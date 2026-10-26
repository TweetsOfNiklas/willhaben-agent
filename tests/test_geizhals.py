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
 <a href="https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html#offerlist"><span class="price">€ 139,00</span></a></article>
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
    assert hits[0].price == 139.0 and hits[0].image.startswith("https://gzhls.at/")
    big = SEARCH.replace("€ 139,00", "€ 1.139,00")                     # thousands separator
    assert geizhals.parse_search(big)[0].price == 1139.0


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
    wait_jobs()
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
        gh = db.get(AdDraft, draft_id).geizhals
    assert gh["match"]["url"].endswith("-a2221733.html") and gh["verified"] is True     # the user's pick counts


# ------------------------------------------------------------------ variants: the 8GB model is not the 4GB one
PI8 = "https://geizhals.at/raspberry-pi-4-modell-b-8gb-ram-a2297397.html"
PI4 = "https://geizhals.at/raspberry-pi-4-modell-b-4gb-ram-a2115430.html"
SEARCH_PI = f"""
<article class="galleryview__item card"><h3 class="galleryview__name">
 <a href="{PI8}" class="galleryview__name-link">Raspberry Pi 4 Modell B, 8GB RAM</a></h3>
 <span class="price">€ 89,90</span></article>
<article class="galleryview__item card"><h3 class="galleryview__name">
 <a href="{PI4}" class="galleryview__name-link">Raspberry Pi 4 Modell B, 4 GB RAM</a></h3>
 <span class="price">€ 59,90</span></article>
"""


def _pi_page(gb):
    return f"""<h1 class="variant__header__headline">Raspberry Pi 4 Modell B, {gb}GB RAM</h1>
<script>window.x={{lowPrice: {89.9 if gb == 8 else 59.9}}}</script>
<dl class="specs-grid"><div class="specs-grid__item"><dt>RAM</dt><dd>{gb}GB</dd></div></dl>"""


@pytest.fixture
def pi(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=SEARCH_PI)
        if request.url.path.endswith(".html"):
            return httpx.Response(200, text=_pi_page(8 if "-8gb-" in request.url.path else 4))
        return httpx.Response(404)

    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0))
    return seen


async def test_lookup_picks_the_matching_variant(pi):
    res = await geizhals.lookup("Raspberry Pi 4 Model B 4GB")
    assert pi[1] == PI4 and res["match"]["url"] == PI4 and res["verified"] is True
    assert res["match"]["price_from"] == 59.9 and [c["url"] for c in res["candidates"]] == [PI8, PI4]   # picker


async def test_lookup_without_matching_card_is_a_guess(pi):
    res = await geizhals.lookup("Raspberry Pi 5 16GB")
    assert res["match"]["url"] == PI8 and res["verified"] is False             # Geizhals' first hit, unconfirmed
    assert len(res["candidates"]) == 2


def test_unconfirmed_variant_does_not_rewrite_the_draft(client, pi, monkeypatch):  # noqa: F811
    fake = client.fake
    orig = fake.complete

    async def complete(**kw):
        r = await orig(**kw)
        if "title" in r.data:
            r.data = {**r.data, "product_query": "Raspberry Pi 5 16GB"}
        return r

    monkeypatch.setattr(fake, "complete", complete)
    client.post("/settings/listings", data={"geizhals_auto_specs": "on"})
    r = client.post("/new-ad", data={"notes": "Raspberry Pi"}, follow_redirects=False)
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    wait_jobs()
    from app.db import AdDraft, session
    with session() as db:
        d = db.get(AdDraft, draft_id)
    assert d.geizhals["verified"] is False and d.geizhals["match"]["url"] == PI8
    assert len(fake.calls) == 1 and not d.revisions                          # no automatic reconcile pass
    assert "Technische Daten" not in (d.description or "")                    # no automatic spec sheet either
    page = client.get(f"/new-ad/{draft_id}").text
    assert "Variant not confirmed" in page and "matched automatically" not in page

    client.post(f"/new-ad/{draft_id}/refine", data={"instruction": "kürzer"})
    wait_jobs()
    assert "Herstellerdaten laut Geizhals" not in fake.calls[-1]["prompt"]    # the guessed sheet is no "reliable" data

    client.post(f"/new-ad/{draft_id}/geizhals", data={"query": "Raspberry Pi 4 Modell B 4GB"})   # typed search
    with session() as db:
        gh = db.get(AdDraft, draft_id).geizhals
    assert gh["verified"] is True and gh["match"]["url"] == PI4


def test_search_terms_never_carry_four_digits_in_a_row():
    """Cloudflare challenges every Geizhals search with four digits in a row, through every server."""
    st = geizhals.search_term
    assert st("RTX 4090") == "RTX 4.090" and st("Gira 211100") == "Gira 211.100"
    assert st("CGM S9000DUAL") == "CGM S9.000DUAL" and st("Amazon Echo Show 8 2021") == "Amazon Echo Show 8 2.021"
    assert st("MDT JAL-0810D.02") == "MDT JAL-081*"                 # a leading zero: no thousands, a wildcard
    assert st("Raspberry Pi 5 8GB") == "Raspberry Pi 5 8GB" and st("Samsung 990 Pro") == "Samsung 990 Pro"
    assert st("ABC1234567X") == "ABC1.234.567X"


async def test_the_cache_answers_for_a_week_then_asks_again_and_forgets_what_nobody_looked_up(monkeypatch):
    from app import db

    monkeypatch.setattr(geizhals, "CACHE_DAYS", 7)
    seen = []
    card = ('<article class="galleryview__item x"><a class="galleryview__name-link" href="#">RTX 4090 FE</a>'
            '<a href="https://geizhals.at/nvidia-rtx-4090-a1.html"></a><span class="price">€ 1.799,00</span></article>')

    def handler(request):
        seen.append(request.url.params.get("fs") or request.url.path)
        if request.url.path == "/":
            return httpx.Response(200, text=card)
        return httpx.Response(200, text="<h1>NVIDIA RTX 4090 FE</h1><script>x={lowPrice: 1799.0}</script>")

    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0, name="Geizhals"))
    clock = [1_000_000.0]
    monkeypatch.setattr(geizhals.time, "time", lambda: clock[0])
    hits = await geizhals.search("RTX 4090")
    assert seen == ["RTX 4.090"] and hits[0].price == 1799.0
    p = await geizhals.product(hits[0].url)
    assert p.name == "NVIDIA RTX 4090 FE" and len(seen) == 2

    clock[0] += 6 * 86400                                         # six days later: from the cache, no request
    assert (await geizhals.search("RTX 4090"))[0].url == hits[0].url
    assert (await geizhals.product(hits[0].url)).price_from == 1799.0 and len(seen) == 2

    clock[0] += 2 * 86400                                         # a week after the fetch: asked again
    await geizhals.search("RTX 4090")
    assert seen[-1] == "RTX 4.090" and len(seen) == 3

    monkeypatch.setattr(geizhals, "_get", _fail)                  # a refresh that fails keeps the old answer
    clock[0] += 8 * 86400
    assert (await geizhals.search("RTX 4090"))[0].price == 1799.0

    clock[0] += 6 * 86400                                         # unused for a week: deleted
    assert geizhals.prune_cache() == 1                            # the product page; the search was used 6 days ago
    clock[0] += 2 * 86400
    assert geizhals.prune_cache() == 1
    with db.session() as ses:
        assert ses.exec(db.select(db.GeizhalsCache)).all() == []


async def _fail(url):
    raise geizhals.GeizhalsError("down")


def test_settings_lists_the_cache_and_clears_it(client):  # noqa: F811
    import time

    from app import db

    now = time.time()
    with db.session() as ses:
        ses.add(db.GeizhalsCache(url="https://geizhals.at/?fs=RTX+4.090&hloc=at", fetched_at=now - 3600, used_at=now,
                                 data=[{"name": "NVIDIA RTX 4090 FE", "url": "https://geizhals.at/x-a1.html",
                                        "price": 1799.0, "image": None}]))
        ses.add(db.GeizhalsCache(url="https://geizhals.at/x-a1.html", fetched_at=now - 8 * 86400, used_at=now - 60,
                                 data={"name": "NVIDIA RTX 4090 Founders Edition", "url": "https://geizhals.at/x-a1.html",
                                       "price_from": 1799.0, "specs": [["Chip", "AD102"]]}))
        ses.commit()
    page = client.get("/settings/geizhals").text
    assert "Cached lookups" in page and "RTX 4.090" in page and "NVIDIA RTX 4090 Founders Edition" in page
    assert "1 hit" in page and "1 hits" not in page and "fetched again on the next lookup" in page        # the product page is over a week old
    r = client.post("/settings/geizhals/clear", follow_redirects=False)
    assert r.status_code == 303 and "2%20cached%20answers%20deleted" in r.headers["location"]
    assert "Nothing cached yet." in client.get("/settings/geizhals").text


def test_a_graphics_cards_variant_suffix_counts():
    from app.services.geizhals import matches
    assert matches("PowerColor Hellhound Radeon RX 6600, 8GB GDDR6", "Radeon RX 6600")
    assert not matches("PowerColor Hellhound Radeon RX 6600 XT, 8GB GDDR6", "Radeon RX 6600")
    assert not matches("Gainward GeForce RTX 4070 Ti SUPER Panther OC, 16GB", "GeForce RTX 4070 Ti")
    assert matches("Gainward GeForce RTX 4070 Ti Panther OC, 12GB", "GeForce RTX 4070 Ti")
