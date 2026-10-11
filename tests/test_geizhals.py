import httpx
import pytest

from app.services import geizhals
from tests.test_dashboard import client  # noqa: F401,F811  (pytest fixture)
from tests.conftest import wait_jobs  # noqa: E402

# Trimmed copies of the real page structure (2026-10-09)
SEARCH = """
<article class="galleryview__item card"><a href="https://geizhals.at/bosch-gsr-18v-55-v9251.html#offerlist">
 <span class="price">€ 139,00</span></a><h3 class="galleryview__name">
 <a href="https://geizhals.at/bosch-gsr-18v-55-v9251.html" class="galleryview__name-link">Bosch GSR 18V-55 (Familie)</a></h3></article>
<article class="galleryview__item card"><div class="galleryview__image-wrapper">
 <a href="https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html" class="galleryview__image-link">
 <img src="https://gzhls.at/pix/91/89/x-m.webp" alt=""></a></div><h3 class="galleryview__name">
 <a href="https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html" class="galleryview__name-link"
    title="x"> Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo </a></h3>
 <a href="https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html#offerlist"><span class="price">€ 1.139,00</span></a></article>
<article class="galleryview__item card"><h3 class="galleryview__name">
 <a href="https://geizhals.at/bosch-gsr-18v-55-set-a2221733.html" class="galleryview__name-link">Bosch GSR 18V-55 Set</a></h3>
 <span class="price">€ 199,90</span></article>
"""
PRODUCT = """<h1 class="variant__header__headline">Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo</h1>
<script>window.x={lowPrice: 139, highPrice: 200}</script>
<dl
class="specs-grid no-limit "
onclick="x">
<div class="specs-grid__item">
<dt>Typ</dt>
<dd>Bohrschrauber </dd>
</div>
<div class="specs-grid__item">
<dt>Leerlaufdrehzahl</dt>
<dd>0-460/&#8203;0-1800/&#8203;min </dd>
</div>
</dl>"""


@pytest.fixture
def gh(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=SEARCH)
        if request.url.path.endswith(".html"):
            return httpx.Response(200, text=PRODUCT)
        return httpx.Response(404)

    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0))
    return seen


def test_parse_search_skips_families():
    hits = geizhals.parse_search(SEARCH)
    assert [h.name for h in hits] == ["Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo", "Bosch GSR 18V-55 Set"]
    assert hits[0].price == 1139.0 and hits[0].image.startswith("https://gzhls.at/")


def test_parse_product():
    p = geizhals.parse_product(PRODUCT, "https://geizhals.at/x-a1.html")
    assert p.name == "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo"
    assert p.price_from == 139.0
    assert p.specs == [("Typ", "Bohrschrauber"), ("Leerlaufdrehzahl", "0-460/0-1800/min")]


async def test_lookup_and_block(gh):
    res = await geizhals.lookup("Bosch GSR 18V-55")
    assert "fs=Bosch+GSR+18V-55" in gh[0] and gh[1].endswith("-a2221732.html")
    assert res["match"]["price_from"] == 139.0 and len(res["candidates"]) == 2
    block = geizhals.spec_block(res["match"])
    assert block.splitlines() == ["Technische Daten (Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo, laut Hersteller/Geizhals):",
                                  "- Typ: Bohrschrauber", "- Leerlaufdrehzahl: 0-460/0-1800/min"]
    with pytest.raises(geizhals.GeizhalsError):
        await geizhals.product("https://evil.example/x-a1.html")


def test_draft_with_geizhals_refine_and_undo(client, gh, monkeypatch):  # noqa: F811
    fake = client.fake
    orig = fake.complete

    async def complete(**kw):
        r = await orig(**kw)
        if "title" in r.data:
            r.data = {**r.data, "product_query": "Bosch GSR 18V-55"}
            if "Änderungswünsche: kürzer" in kw["prompt"]:
                r.data = {**r.data, "title": "Bosch GSR 18V-55 kürzer", "description": "Kurz."}
        return r

    monkeypatch.setattr(fake, "complete", complete)
    r = client.post("/new-ad", data={"notes": "Bosch Akkuschrauber"}, follow_redirects=False)
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/new-ad/{draft_id}").text
    assert "New from" in page and "Geizhals spec sheet" in page and "Bosch GSR 18V-55 Set" in page
    assert "Check text against the spec sheet" in page
    assert "Checked against the Geizhals spec sheet" in page          # automatic pass after drafting, undoable
    assert "Herstellerdaten laut Geizhals" in fake.calls[-1]["prompt"]
    client.post(f"/new-ad/{draft_id}/undo")                          # back to Claude's first text

    # user ticks the spec sheet (JS appends it) and saves
    from app.db import AdDraft, session
    with session() as db:
        d = db.get(AdDraft, draft_id)
    block = geizhals.spec_block(d.geizhals["match"])
    client.post(f"/new-ad/{draft_id}", data={"title": d.title, "price": "120",
                                             "description": (d.description + "\n\n" + block).replace("\n", "\r\n")})

    client.post(f"/new-ad/{draft_id}/refine", data={"instruction": "kürzer"})

    wait_jobs()
    refine_prompt = fake.calls[-1]["prompt"]
    assert "Herstellerdaten laut Geizhals" in refine_prompt and "- Typ: Bohrschrauber" in refine_prompt
    assert block not in refine_prompt                     # Claude gets the prose only
    with session() as db:
        d = db.get(AdDraft, draft_id)
    assert d.title == "Bosch GSR 18V-55 kürzer" and d.description == "Kurz.\n\n" + block   # sheet re-attached
    assert d.revisions[-1]["instruction"] == "kürzer"

    client.post(f"/new-ad/{draft_id}/undo")
    with session() as db:
        d = db.get(AdDraft, draft_id)
    assert d.title == "KTM Kinderrad 20 Zoll blau" and not d.revisions

    client.post(f"/new-ad/{draft_id}/geizhals",
                data={"url": "https://geizhals.at/bosch-gsr-18v-55-set-a2221733.html"})
    with session() as db:
        assert db.get(AdDraft, draft_id).geizhals["match"]["url"].endswith("-a2221733.html")
