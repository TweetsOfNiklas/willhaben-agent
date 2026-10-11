import copy
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.llm.base import LLMError, LLMResult
from app.services import deals, geizhals
from app.willhaben import public as public_mod
from app.willhaben.parse import AdDetail
from app.willhaben.public import PublicClient
from tests.conftest import load
from tests.test_geizhals import PRODUCT, SEARCH
from tests.test_search_agents import _set_attr

AD_ID = "1000000001"
PRICES = [40, 60, 80, 90, 100, 110, 120, 130, 150, 200]


def _ad(price=100.0, heading="Bosch GSR 18V-55 Akkuschrauber neuwertig OVP", ad_id=AD_ID):
    return AdDetail(ad_id=ad_id, heading=heading, description="Kaum benutzt.", price=price, price_display=None,
                    status="active", published_at=None, first_published_at=None, end_date=None,
                    url=f"https://www.willhaben.at/iad/object?adId={ad_id}", images=[], location="1010 Wien",
                    is_private=True, category_path=["Haus & Garten", "Werkzeug"])


def _search_json(prices, include_self_at=None):
    base = load("search_fahrrad_rows3.json")
    tmpl = base["advertSummaryList"]["advertSummary"][0]
    items = []
    for n, p in enumerate(prices):
        ad = copy.deepcopy(tmpl)
        ad["id"] = f"10000001{n:02d}"
        _set_attr(ad, "ADID", ad["id"])
        _set_attr(ad, "HEADING", f"Bosch GSR 18V-55 Nr {n}")
        _set_attr(ad, "PRICE", str(p))
        _set_attr(ad, "PRICE/AMOUNT", str(p))
        items.append(ad)
    if include_self_at is not None:              # the checked ad itself shows up in the search
        me = copy.deepcopy(tmpl)
        me["id"] = AD_ID
        _set_attr(me, "ADID", AD_ID)
        _set_attr(me, "HEADING", "Bosch GSR 18V-55 Akkuschrauber")
        _set_attr(me, "PRICE", str(include_self_at))
        _set_attr(me, "PRICE/AMOUNT", str(include_self_at))
        items.append(me)
    wanted = copy.deepcopy(tmpl)                 # "Suche ..." ads are not comparables
    wanted["id"] = "1000000199"
    _set_attr(wanted, "ADID", wanted["id"])
    _set_attr(wanted, "HEADING", "Suche Bosch GSR 18V-55")
    _set_attr(wanted, "PRICE", "1")
    _set_attr(wanted, "PRICE/AMOUNT", "1")
    items.append(wanted)
    base["advertSummaryList"]["advertSummary"] = items
    base["rowsFound"] = len(items)
    return base


def _detail_html(price=100, heading="Bosch GSR 18V-55 Akkuschrauber neuwertig"):
    props = load("detail_1662146981_nextdata.json")
    ad = props["advertDetails"]
    ad["id"], ad["description"] = int(AD_ID), heading
    ad["breadcrumbs"] = ad["breadcrumbs"][:2] + [{"displayName": "Haus & Garten"}, {"displayName": "Werkzeug"}]
    _set_attr(ad, "PRICE", str(price))
    _set_attr(ad, "PRICE/AMOUNT", str(price))
    _set_attr(ad, "PRICE_FOR_DISPLAY", f"€ {price}")
    nd = {"page": "/iad/kaufen-und-verkaufen/d/[[...seopath]]", "buildId": "x", "props": {"pageProps": props}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'


@pytest.fixture(autouse=True)
def _clean():
    deals.clear_cache()
    yield
    deals.clear_cache()


@pytest.fixture
def wh():
    """Mocked willhaben: counts search and detail requests."""
    calls = {"search": 0, "detail": 0}
    state = {"search": _search_json(PRICES, include_self_at=100), "detail": _detail_html()}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/webapi/ad-search/search"):
            calls["search"] += 1
            return httpx.Response(200, json=state["search"])
        if request.url.path == "/iad/object":
            calls["detail"] += 1
            return httpx.Response(200, text=state["detail"])
        return httpx.Response(404)

    public_mod.set_public(PublicClient(transport=httpx.MockTransport(handler)))
    yield calls, state
    public_mod.set_public(None)


@pytest.fixture
def gh(monkeypatch):
    """Mocked Geizhals pages (same structure as the real ones), counting requests."""
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


class FakeClaude:
    model = "fake-haiku"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.fail:
            raise LLMError("rate limited")
        return LLMResult(data={"deal": "good", "reason": "Günstig: rund 30 % unter dem üblichen Gebrauchtpreis."},
                         model=self.model)


# ------------------------------------------------------------------ pure functions
def test_query_from_title_drops_filler():
    assert deals.query_from_title("Verkaufe Bosch GSR 18V-55 Akkuschrauber neuwertig OVP!") == "Bosch GSR 18V-55"
    assert deals.query_from_title("Top Zustand: iPhone 13 128GB") == "iPhone 13 128GB"
    assert deals.query_from_title("neu und OVP") == ""


def test_geizhals_query_only_for_products():
    assert deals.geizhals_query("iPhone 13 128GB Display kaputt") == "iPhone 13 128GB"
    assert deals.geizhals_query("Bosch GSR 18V-55 Akkuschrauber") == "Bosch GSR 18V-55"
    assert deals.geizhals_query("Cannondale Kinderfahrrad 20 Zoll") is None       # size, not a model
    assert deals.geizhals_query("Schöner alter Holzschrank") is None
    assert deals.geizhals_query("Jacke Größe 38", ["Mode & Accessoires"]) is None


@pytest.mark.parametrize("title, query", [
    ("Raspberry Pi 4 4GB", "Raspberry Pi 4 4GB"),
    ("Raspberry Pi 4 Model B 4GB", "Raspberry Pi 4 Model 4GB"),                 # capacity past the 4-word cap
    ("Raspberry Pi 4 Modell B 4GB RAM mit Gehäuse", "Raspberry Pi 4 Modell 4GB"),
    ("Raspberry Pi 4 Model B 4 GB", "Raspberry Pi 4 Model 4GB"),                # "4 GB" is one word, not a 2nd "4"
    ("Neuwertiger Raspberry Pi 4 (4 GB)", "Raspberry Pi 4 4GB"),
    ("MacBook Air M1 8GB 256GB", "MacBook Air M1 8GB 256GB"),
])
def test_geizhals_query_keeps_the_variant(title, query):
    assert deals.geizhals_query(title) == query


def test_stats_percentiles():
    st = deals.stats(PRICES + [0, None])
    assert st["count"] == 10 and st["min"] == 40 and st["max"] == 200 and st["median"] == 105
    assert st["p10"] < st["p25"] < st["median"] < st["p75"] < st["p90"]
    assert deals.stats([10, 20]) is None


@pytest.mark.parametrize("price,lvl", [(30, "bargain"), (70, "cheap"), (105, "fair"), (140, "expensive"),
                                       (250, "very_expensive")])
def test_position_from_comparables(price, lvl):
    p = deals.position(price, deals.stats(PRICES), None)
    assert p["basis"] == "comparables" and p["level"] == lvl and 0 <= p["pos"] <= 1
    assert [round(x[0], 2) for x in p["ticks"]] == [0.25, 0.5, 0.75]
    assert deals.position(105, deals.stats(PRICES), None)["pos"] == pytest.approx(0.5)


def test_position_labels_and_suspicious():
    i18n_labels = {deals.level_label(x) for _, x in deals.LEVELS}
    assert i18n_labels == {"bargain", "cheap", "fair", "expensive", "very expensive"}
    assert deals.position(10, deals.stats(PRICES), None)["suspicious"] is True
    assert deals.position(100, deals.stats(PRICES), None)["suspicious"] is False


def test_position_falls_back_to_geizhals_new_price():
    few = deals.stats([90, 100, 110])                       # 3 < MIN_COMPARABLES
    p = deals.position(55, few, 100.0)
    assert p["basis"] == "geizhals" and p["pos"] == pytest.approx(0.5) and p["level"] == "fair"
    assert [x[1] for x in p["ticks"]] == [40, 55, 70]
    assert deals.position(30, None, 100.0)["level"] == "bargain"
    assert deals.position(120, None, 100.0)["level"] == "very_expensive"


def test_position_not_enough_data():
    assert deals.position(100, deals.stats([90, 100, 110]), None) == {"pos": None, "reason": "few_data"}
    assert deals.position(None, deals.stats(PRICES), 100.0)["reason"] == "no_price"
    assert deals.position(0, deals.stats(PRICES), 100.0)["reason"] == "free"
    res = {"position": {"pos": None, "reason": "few_data"}, "comparables": {"count": 2}}
    assert "only 2 comparable ads" in deals.explain(res)


@pytest.mark.parametrize("text,ad_id", [
    ("1000000001", "1000000001"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/bosch-gsr-18v-55-1000000001/", "1000000001"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/bosch-gsr-18v-55-1000000001", "1000000001"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/iphone-13-1000000002/?utm_source=x", "1000000002"),
    ("https://www.willhaben.at/iad/object?adId=1000000003", "1000000003"),
    ("willhaben.at/iad/object?foo=1&adId=1000000004#top", "1000000004"),
    ("https://m.willhaben.at/iad/gebrauchtwagen/d/auto/vw-golf-1000000005/", "1000000005"),
    ("iPhone 13 128GB", None),
    ("rtx 3080", None),
    ("", None),
])
def test_parse_ad_ref(text, ad_id):
    assert deals.parse_ad_ref(text) == ad_id


# ------------------------------------------------------------------ analysis with fakes
async def test_comparables_exclude_ad_itself_and_wanted_ads(wh, monkeypatch):
    calls, _ = wh
    async def no_gh(q):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    res = await deals.analyze(_ad(100))
    comp = res["comparables"]
    assert calls["search"] == 1 and comp["query"] == "Bosch GSR 18V-55"
    assert comp["count"] == len(PRICES)                     # without the ad itself and the "Suche" ad
    assert AD_ID not in {a["ad_id"] for a in comp["closest"]} and len(comp["closest"]) == 10
    assert [a["price"] for a in comp["closest"]] == sorted(a["price"] for a in comp["closest"])
    assert res["position"]["basis"] == "comparables" and res["geizhals"] is None


async def test_caching_avoids_repeat_requests(wh, gh):
    calls, _ = wh
    first = await deals.analyze(_ad(100))
    assert calls["search"] == 1 and len(gh) == 2               # one Geizhals search + one product page
    again = await deals.analyze(_ad(100))
    assert again is first and calls["search"] == 1 and len(gh) == 2
    other = await deals.analyze(_ad(90, ad_id="1000000002"))    # other ad, same query: search + Geizhals cached
    assert calls["search"] == 1 and len(gh) == 2 and other["ad"]["ad_id"] == "1000000002"
    await deals.analyze(_ad(100), query="Akkuschrauber Bosch")   # new query -> exactly one new search
    assert calls["search"] == 2


async def test_geizhals_match_and_optional(wh, gh):
    res = await deals.analyze(_ad(100))
    assert res["geizhals"] == {"name": "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo",
                               "url": "https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html",
                               "price_from": 139.0}
    plain = await deals.analyze(_ad(100, heading="Alter Holzschrank Eiche", ad_id="1000000003"))
    assert plain["geizhals"] is None and len(gh) == 2           # generic title: Geizhals not asked


async def test_geizhals_error_and_mismatch_give_none(wh, monkeypatch):
    async def boom(q):
        raise geizhals.GeizhalsError("HTTP 503")
    monkeypatch.setattr(deals.geizhals, "lookup", boom)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None

    async def other(q):
        return {"match": {"name": "Makita DDF484Z", "url": "https://geizhals.at/x-a1.html", "price_from": 99.0}}
    monkeypatch.setattr(deals.geizhals, "lookup", other)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None


async def test_few_comparables_use_geizhals(wh, gh):
    _, state = wh
    state["search"] = _search_json([100, 120])
    res = await deals.analyze(_ad(70))
    assert res["comparables"]["count"] == 2 and res["position"]["basis"] == "geizhals"
    assert res["position"]["ref"]["new_price"] == 139.0


async def test_claude_only_when_asked(wh, monkeypatch):
    async def no_gh(q):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    fake = FakeClaude()
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100))
    assert fake.calls == [] and res["claude"] is None
    res = await deals.analyze(_ad(100), use_claude=True)
    assert len(fake.calls) == 1 and res["claude"]["deal"] == "good"
    assert "Median 105 €" in fake.calls[0]["prompt"] and "Bosch GSR 18V-55" in fake.calls[0]["prompt"]
    res = await deals.analyze(_ad(100), use_claude=True)        # cached verdict, no second call
    assert len(fake.calls) == 1 and res["claude"]["deal"] == "good"


async def test_claude_failure_is_not_cached(wh, monkeypatch):
    async def no_gh(q):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    monkeypatch.setattr(deals, "get_backend", lambda *a: FakeClaude(fail=True))
    res = await deals.analyze(_ad(100), use_claude=True)
    assert res["claude"] == {"error": "rate limited"}
    assert deals.cached(AD_ID)["claude"] is None


# ------------------------------------------------------------------ pages
@pytest.fixture
def app_client(wh, gh, monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    from app.main import app

    with TestClient(app) as c:
        c.fake, c.calls = fake, wh[0]
        yield c


def test_viewer_has_lazy_price_section(app_client):
    page = app_client.get(f"/hits/ad/{AD_ID}").text
    assert f'hx-get="/hits/ad/{AD_ID}/deal"' in page and 'hx-trigger="load delay:700ms"' in page
    assert app_client.calls["search"] == 0                       # opening the ad does not search yet
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal").text
    assert "deal-track" in frag and "10 comparable ads" in frag and "fair" in frag
    assert 'href="https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html" target="_blank" rel="noopener"' in frag
    assert "New from € 139 on Geizhals" in frag and "Ask Claude" in frag
    assert "Comparable ads" in frag and "closest by price" in frag   # list in the viewer too
    assert "this ad" in frag
    assert app_client.fake.calls == []
    app_client.get(f"/hits/ad/{AD_ID}/deal")                     # reopening: no new requests
    assert app_client.calls == {"search": 1, "detail": 1}


def test_viewer_keeps_screening_and_uses_agent_keyword(app_client, wh):
    _, state = wh
    state["detail"] = _detail_html(heading="neu OVP")            # title gives no query
    with dbmod.session() as db:
        a = dbmod.SearchAgent(name="Bohrer", source_url="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=bohrschrauber",
                              webapi_url="x")
        db.add(a)
        db.commit()
        db.refresh(a)
        h = dbmod.Hit(agent_id=a.id, ad_id=AD_ID, heading="neu OVP", url="u", screen_relevant=True, screen_deal="good",
                      screen_reason="Passt.")
        db.add(h)
        db.commit()
        db.refresh(h)
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal?hit={h.id}").text
    assert "Ask Claude" not in frag   # screened hit: assessment already shown


def test_deals_pages_and_claude_on_post(app_client):
    page = app_client.get("/deals")
    assert page.status_code == 200 and "Product search" in page.text and "up to 4 willhaben/Geizhals requests" in page.text
    assert 'href="/deals"' in page.text                          # nav tab

    r = app_client.get("/deals", params={"q": f"https://www.willhaben.at/iad/kaufen-und-verkaufen/d/bosch-{AD_ID}/"},
                       follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/deals/ad/{AD_ID}"

    listing = app_client.get("/deals", params={"q": "Bosch GSR 18V-55"}).text
    assert "Bosch GSR 18V-55 Nr 0" in listing and f"/deals/ad/{AD_ID}?q=Bosch%20GSR%2018V-55" in listing
    assert "bg-lvl-" in listing and app_client.calls["search"] == 1

    page = app_client.get(f"/deals/ad/{AD_ID}", params={"q": "Bosch GSR 18V-55"}).text   # answers at once ...
    assert f'hx-get="/deals/ad/{AD_ID}/result?q=Bosch%20GSR%2018V-55' in page
    assert app_client.calls["search"] == 1                                    # ... without willhaben requests
    result = app_client.get(f"/deals/ad/{AD_ID}/result", params={"q": "Bosch GSR 18V-55"}).text   # ... the check loads into it
    assert "Comparable ads" in result and "this ad" in result and "<html" not in result
    assert "Recently checked" in app_client.get(f"/deals/ad/{AD_ID}").text      # the check is remembered
    assert app_client.calls["search"] == 2   # comparables served from cache; +1 for the category navigator (selector)
    assert 'name="cat"' in result and "Automatic (category of this ad)" in result
    assert app_client.fake.calls == []                           # never automatic

    verdict = app_client.post(f"/deals/ad/{AD_ID}/claude", data={"q": "Bosch GSR 18V-55"}).text
    assert len(app_client.fake.calls) == 1 and "Claude&#39;s assessment" in verdict and "30 %" in verdict
    again = app_client.get(f"/deals/ad/{AD_ID}/result", params={"q": "Bosch GSR 18V-55"}).text
    assert "Claude&#39;s assessment" in again and len(app_client.fake.calls) == 1
    with dbmod.session() as db:
        row = db.get(dbmod.DealCheck, AD_ID)
    assert row.claude["deal"] == "good" and row.result["position"]["level"]


def test_deals_page_german(app_client):
    from app.db import set_setting
    set_setting("ui_lang", "de")
    page = app_client.get("/deals").text
    assert "Produktsuche" in page and "Claude wird nur auf Klick gefragt" in page
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal").text
    assert "Neu ab € 139 auf Geizhals" in frag and "vergleichbare Anzeigen" in frag


# ------------------------------------------------------------------ review fixes
def test_matches_model_numbers_as_whole_words():
    assert deals._matches("Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo", "Bosch GSR 18V-55")
    assert not deals._matches("Apple iPhone 113 Pro", "Apple iPhone 13")
    assert not deals._matches("Tisch 230cm Eiche", "Tisch 23 Eiche")


@pytest.mark.parametrize("name, query, ok", [
    ("Raspberry Pi 4 Modell B, 8GB RAM", "Raspberry Pi 4 4GB", False),         # other variant
    ("Raspberry Pi 4 Modell B, 4GB RAM", "Raspberry Pi 4 4GB", True),
    ("Raspberry Pi 4 Modell B 4 GB", "Raspberry Pi 4 4GB", True),               # "4 GB" = "4GB"
    ("Raspberry Pi 5, 8GB RAM", "Raspberry Pi 4", False),                       # single digit counts too
    ("Raspberry Pi 400", "Raspberry Pi 4", False),
    ("Raspberry Pi 4 Modell B, 8GB RAM", "Raspberry Pi 4", True),               # query without capacity
])
def test_matches_variants(name, query, ok):
    assert deals._matches(name, query) is ok


async def test_geizhals_cache_per_variant_and_only_verified(wh, monkeypatch):
    asked = []

    async def lookup(q):
        asked.append(q)
        gb = "8GB" if "8GB" in q else "4GB"
        return {"query": q, "verified": True, "candidates": [],
                "match": {"name": f"Raspberry Pi 4 Modell B, {gb} RAM", "url": f"https://geizhals.at/pi4-{gb}-a1.html",
                          "price_from": 89.9 if gb == "8GB" else 59.9}}
    monkeypatch.setattr(deals.geizhals, "lookup", lookup)
    four = await deals.geizhals_match(deals.geizhals_query("Raspberry Pi 4 Model B 4 GB"))
    eight = await deals.geizhals_match(deals.geizhals_query("Raspberry Pi 4 Model B 8GB"))
    assert four["price_from"] == 59.9 and eight["price_from"] == 89.9 and len(asked) == 2
    assert len(deals._geizhals) == 2                                         # one cache entry per variant

    async def guess(q):                                                      # Geizhals' first hit, not confirmed
        return {**await lookup(q), "verified": False}
    monkeypatch.setattr(deals.geizhals, "lookup", guess)
    assert await deals.geizhals_match("Raspberry Pi 4 Modell 4GB") is None


async def test_geizhals_error_is_negatively_cached(wh, monkeypatch):
    n = []

    async def boom(q):
        n.append(q)
        raise geizhals.GeizhalsError("HTTP 429")
    monkeypatch.setattr(deals.geizhals, "lookup", boom)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None
    assert len(n) == 1
    deals._geizhals_fail["bosch gsr 18v-55"] = (deals.time.monotonic() - deals.GEIZHALS_FAIL_TTL_S - 1, None)
    await deals.geizhals_match("Bosch GSR 18V-55")
    assert len(n) == 2


async def test_viewing_does_not_extend_freshness(wh, monkeypatch):
    async def no_gh(q):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    res = await deals.analyze(_ad(100))
    old = {**res, "checked_at": (dbmod.utcnow() - timedelta(seconds=deals.RESULT_TTL_S + 60)).isoformat()}
    deals.remember(old)                                         # viewed just now, analysed over an hour ago
    deals.clear_cache()
    assert deals.cached(AD_ID) is None
    fresh = {**res, "checked_at": (dbmod.utcnow() - timedelta(seconds=deals.RESULT_TTL_S - 60)).isoformat()}
    deals.remember(fresh)
    deals.clear_cache()
    assert deals.cached(AD_ID) is not None
    assert deals.time.monotonic() - deals._results[(AD_ID, "")][0] > deals.RESULT_TTL_S - 120


async def test_claude_verdict_tied_to_price(wh, monkeypatch):
    async def no_gh(q):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    fake = FakeClaude()
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100), use_claude=True)
    deals.remember(res)
    assert res["claude"]["price"] == 100.0 and len(fake.calls) == 1
    deals.clear_cache()
    cheaper = await deals.analyze(_ad(80))                     # seller lowered the price: old verdict not shown
    assert cheaper["claude"] is None
    await deals.analyze(_ad(80), use_claude=True)
    assert len(fake.calls) == 2


def test_deals_page_rejects_non_ad_links(app_client):
    page = app_client.get("/deals", params={"q": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552"}).text
    assert "This link is not a willhaben ad." in page and app_client.calls["search"] == 0



def _summ(n, title, price, cats):
    return {"ad_id": f"20000000{n:02d}", "title": title, "price": price, "category_ids": cats.split(";")}


async def test_comparables_same_category_kind_and_trimmed(monkeypatch):
    """iPhone 15 Pro at 490: cases (other category), a 15 Pro Max and a €1 swap must not shape the bar."""
    phone, case_cat = "2691;2722;2724;5013241", "2691;2722;2752"
    ads = [_summ(n, "iPhone 15 Pro 128GB", p, phone) for n, p in enumerate([520, 560, 600, 640, 580, 610, 590])]
    ads += [_summ(20, "iPhone 15 Pro Max 256GB", 900, phone), _summ(21, "iPhone 15 Pro Hülle MagSafe", 12, phone),
            _summ(22, "iPhone 15 Pro", 1, phone)]
    ads += [_summ(30 + n, "Panzerglas iPhone 15 Pro", 8, case_cat) for n in range(6)]

    async def search_ads(q):
        return {"rows_found": len(ads), "ads": ads}

    monkeypatch.setattr(deals, "search_ads", search_ads)
    out = await deals.comparables("iPhone 15 pro", AD_ID, 490, category_id="5013241", title="iPhone 15 pro")
    assert out["scope"] == "leaf" and out["count"] == 7
    assert out["filtered"] == {"category": 6, "kind": 2, "outliers": 1}
    assert out["stats"]["min"] == 520 and out["stats"]["max"] == 640
    assert deals.position(490, out["stats"], None)["level"] == "bargain"      # was "teuer" against cases at €12


async def test_comparables_fall_back_to_parent_category(monkeypatch):
    ads = [_summ(n, "Rennrad Shimano 105", p, "1;2;3") for n, p in enumerate([400, 500, 600])]
    ads += [_summ(10 + n, "Rennrad Ultegra", p, "1;2;4") for n, p in enumerate([450, 550, 650])]

    async def search_ads(q):
        return {"rows_found": len(ads), "ads": ads}

    monkeypatch.setattr(deals, "search_ads", search_ads)
    out = await deals.comparables("rennrad", AD_ID, 500, category_id="3", title="Rennrad")
    assert out["scope"] == "parent" and out["count"] == 6
    out = await deals.comparables("rennrad", AD_ID, 500, category_id=None, title="Rennrad")
    assert out["scope"] is None and out["count"] == 6


def test_same_kind_rules():
    assert deals._same_kind("iPhone 15 Pro 256GB blau", "iPhone 15 pro")
    assert not deals._same_kind("iPhone 15 Pro Max", "iPhone 15 pro")
    assert not deals._same_kind("Hülle für iPhone 15 Pro", "iPhone 15 pro")
    assert deals._same_kind("iPhone 15 Pro Display defekt", "iPhone 15 Pro defekt")    # the ad itself is defective
    assert not deals._same_kind("Nur OVP iPhone 15 Pro", "iPhone 15 pro")


async def test_category_choices_and_chosen_category(monkeypatch):
    from app.willhaben.parse import FilterValue, SearchFilter, SearchFilters

    seen = []

    async def search_filters(url, rows=6):
        seen.append(url)
        nav = SearchFilter(id="category", label="Kategorie", kind="HIERARCHICAL", multi=False,
                           values=[FilterValue("Smartphones / Telefonie", "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/smartphones-telefonie-2691?keyword=iphone", 36)],
                           selected=[])
        return SearchFilters(url=url, rows_found=50, groups=[("Kategorie", [nav])], ads=[])

    monkeypatch.setattr(deals.get_public(), "search_filters", search_filters)
    ch = await deals.category_choices("iphone 15 pro")
    assert seen[0].endswith("keyword=iphone+15+pro") and ch["values"][0]["hits"] == 36

    cat = ch["values"][0]["url"]
    got = []

    async def search_category(url):
        got.append(url)
        return {"rows_found": 2, "ads": [_summ(n, "Handy", p, "9") for n, p in enumerate([100, 120, 140, 160, 180])]}

    monkeypatch.setattr(deals, "search_category", search_category)
    out = await deals.comparables("iphone", AD_ID, 150, category_id="5013241", title="Handy", category_url=cat)
    assert got == [cat] and out["scope"] == "chosen" and out["count"] == 5   # no leaf filter on a chosen category
