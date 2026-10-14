import copy
import json
import re
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


def test_query_from_title_glues_sizes():
    assert deals.query_from_title("Raspberry Pi 4 4 GB") == "Raspberry Pi 4"        # the second "4" is no duplicate
    assert deals.query_from_title("Raspberry Pi 4 4 GB", words=4) == "Raspberry Pi 4 4GB"
    assert deals.query_from_title("iPhone 13 128 GB blau") == "iPhone 13 128GB"     # in the 3 words: one word
    assert deals.query_from_title("MacBook Air M1 8GB 256GB") == "MacBook Air M1"   # no size pulled in from further on


def test_geizhals_query_only_for_products():
    assert deals.geizhals_query("iPhone 13 128GB Display kaputt") == "iPhone 13 128GB"
    assert deals.geizhals_query("Bosch GSR 18V-55 Akkuschrauber") == "Bosch GSR 18V-55"
    assert deals.geizhals_query("Cannondale Kinderfahrrad 20 Zoll") is None       # size, not a model
    assert deals.geizhals_query("Schöner alter Holzschrank") is None
    assert deals.geizhals_query("Jacke Größe 38", ["Mode & Accessoires"]) is None


@pytest.mark.parametrize("title, query", [
    ("Raspberry Pi 4 4GB", "Raspberry Pi 4 4GB"),
    ("Raspberry Pi 4 Model B 4GB", "Raspberry Pi 4 4GB"),                       # capacity further on is added
    ("Raspberry Pi 4 Modell B 4GB RAM mit Gehäuse", "Raspberry Pi 4 4GB"),
    ("Raspberry Pi 4 Model B 4 GB", "Raspberry Pi 4 4GB"),                      # "4 GB" is one word, not a 2nd "4"
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
    i18n_labels = {deals.level_label(x) for x in deals.LEVEL_NAMES}
    assert i18n_labels == {"bargain", "cheap", "fair", "expensive", "very expensive"}
    assert deals.position(10, deals.stats(PRICES), None)["suspicious"] is True
    assert deals.position(100, deals.stats(PRICES), None)["suspicious"] is False


USER_CASE = [120, 135, 150, 170]          # the user's report: 4 comparables, p25 131.25, p75 155


def test_position_by_zones_the_users_case():
    """139 € for a product new from 202,30 €: 68 % of new is "fair" under the default used zones (was "teuer")."""
    p = deals.position(139, deals.stats(USER_CASE), 202.30)
    assert p["basis"] == "geizhals" and p["level"] == "fair" and p["pct"] == 68 and p["axis"] == 110
    assert p["group"] == "used" and p["zones"] == [30, 50, 80, 90] and p["new_price"] == 202.30
    assert p["band"] == {"low": 131.25, "high": 155, "count": 4} and p["pos"] == pytest.approx(0.625, abs=0.001)
    assert p["cross"] is None and p["mismatch"] is None and p["suspicious"] is False


def test_zones_win_over_enough_comparables():
    st = deals.stats(PRICES)                                  # comparables alone: 105 is fair
    p = deals.position(105, st, 120.0)                         # 87 % of new: expensive by the zones
    assert p["basis"] == "geizhals" and p["level"] == "expensive" and p["pct"] == 87


def test_new_items_use_the_new_zones():
    assert deals.position(195.02, None, 199.0, "new")["level"] == "cheap"      # 98 %: below the lowest shop price
    p = deals.position(205, None, 199.0, "new")
    assert p["level"] == "fair" and p["group"] == "new" and p["axis"] == 140
    for cond in ("like_new", "refurbished", "used", None):                      # everything else: the used zones
        q = deals.position(195.02, None, 199.0, cond)
        assert q["group"] == "used" and q["level"] == "very_expensive"


def test_defect_gets_no_label():
    p = deals.position(50, deals.stats(PRICES), 100.0, "defect")
    assert p == {"pos": None, "reason": "defect"}
    assert "Defective according to the ad" in deals.explain({"position": p, "comparables": {"count": 10}})


def test_suspicious_by_zones():
    assert deals.position(14, None, 100.0)["suspicious"] is True             # below half the first used limit (15 %)
    assert deals.position(15, None, 100.0)["suspicious"] is False
    assert deals.position(37, None, 100.0, "new")["suspicious"] is True      # 37.5 % for new items
    st = deals.stats([100, 110, 120, 130, 140, 150])                         # p25 112.5: below 56.25 is suspicious
    p = deals.position(56, st, 150.0)
    assert p["basis"] == "geizhals" and p["pct"] == 37 and p["suspicious"] is True


def test_hard_mismatch_ignores_the_new_price():
    st = deals.stats([380, 390, 400, 410, 420, 430])                         # median 405 = 4 × new
    p = deals.position(400, st, 100.0)
    assert p["basis"] == "comparables" and p["new_price_ignored"] == 405 and p["level"] == "fair"
    low = deals.position(10, deals.stats([10, 11, 12, 13, 14]), 100.0)      # median 12 % of new
    assert low["basis"] == "comparables" and low["new_price_ignored"] == 12


def test_soft_mismatch_hint_and_cross_note():
    st = deals.stats([130, 140, 150, 160, 170])                              # median 150 = 1.5 × new
    p = deals.position(140, st, 100.0)
    assert p["basis"] == "geizhals" and p["level"] == "very_expensive" and p["cross"] == "fair"
    assert p["mismatch"] == 150
    p = deals.position(120, st, 100.0, "new")                                # new items: no cross note
    assert p["cross"] is None and p["mismatch"] == 150 and p["level"] == "expensive"
    near = deals.position(90, deals.stats([80, 85, 90, 95, 100]), 100.0)     # very expensive vs fair: 2 steps
    assert near["cross"] == "fair" and near["mismatch"] is None
    one = deals.position(85, deals.stats([80, 85, 90, 95, 100]), 100.0)      # expensive vs fair: 1 step, no note
    assert one["level"] == "expensive" and one["cross"] is None
    few = deals.position(140, deals.stats([130, 150, 170]), 100.0)            # under 5 comparables: neither
    assert few["cross"] is None and few["mismatch"] is None and few["band"]["count"] == 3


def test_tied_quantiles_keep_the_ring_in_its_zone():
    p = deals.position(100, deals.stats([100] * 5 + [200]), None)
    assert p["level"] == "fair" and p["pos"] == pytest.approx(0.32) and 0.25 <= p["pos"] <= 0.75
    assert p["ticks"] == [[0.5, 100]]                                          # p25 == p75: one mark


def test_comparables_rule_by_price():
    st = deals.stats(PRICES)                                                   # p10 58, p25 82.5, p75 127.5, p90 155
    assert [deals.comparables_level(x, st) for x in (57.99, 58, 82.49, 82.5, 127.5, 127.51, 155, 155.01)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]
    for price in range(30, 260):
        p = deals.position(price, st, None)
        lo, hi = deals.QUANTILE_SEGMENTS[p["level"]]
        assert lo <= p["pos"] <= hi


def test_level_at_bounds():
    assert [deals.level_at(x) for x in (0.09, 0.1, 0.24, 0.25, 0.75, 0.76, 0.9, 0.91)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]


def test_zone_span_and_hit_level():
    p = deals.position(139, None, 202.30)
    assert deals.zone_span(p) == {"from_pct": 50, "to_pct": 80, "from": 101.15, "to": 161.84}
    assert deals.zone_span(deals.position(20, None, 202.30)) == {"from_pct": None, "to_pct": 30, "from": None,
                                                               "to": 60.69}
    assert deals.zone_span(deals.position(200, None, 202.30))["to"] is None

    class H:
        price, geizhals, item_condition = 161.84, {"price_from": 202.30}, None
    assert deals.hit_level(H()) == "expensive"
    H.price = 161.83
    assert deals.hit_level(H()) == "fair"
    H.item_condition = "defect"
    assert deals.hit_level(H()) is None
    H.item_condition, H.geizhals = None, {}
    assert deals.hit_level(H()) is None


def test_card_position_reads_the_stats_or_the_stored_position():
    st = deals.stats(PRICES)
    assert deals.card_position({"price": 140, "stats": st})["level"] == "expensive"
    assert deals.card_position({"price": 140, "stats": st, "item_condition": "defect"})["reason"] == "defect"
    legacy = {"price": 140, "position": {"pos": 0.8, "level": "expensive", "basis": "comparables"}}
    assert deals.card_position(legacy)["level"] == "expensive"
    assert deals.card_position({"price": 140, "position": {"pos": 0.5, "level": "expensive"}})["level"] == "fair"
    assert deals.card_position({"price": 140}) == {}


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
    assert deals.parse_ad_ref(text) == (("willhaben", ad_id) if ad_id else None)
    assert deals.parse_ad_ref("kleinanzeigen:2712345678") == ("kleinanzeigen", "2712345678")


# ------------------------------------------------------------------ analysis with fakes
async def test_comparables_exclude_ad_itself_and_wanted_ads(wh, monkeypatch):
    calls, _ = wh
    async def no_gh(q, **kw):
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
    assert calls["search"] == 1 and len(gh) == 1               # one Geizhals search, no product page (card price)
    again = await deals.analyze(_ad(100))
    assert again == first and calls["search"] == 1 and len(gh) == 1     # re-rated on read: an equal copy
    other = await deals.analyze(_ad(90, ad_id="1000000002"))    # other ad, same query: search + Geizhals cached
    assert calls["search"] == 1 and len(gh) == 1 and other["ad"]["ad_id"] == "1000000002"
    await deals.analyze(_ad(100), query="Akkuschrauber Bosch")   # new query -> exactly one new search
    assert calls["search"] == 2


async def test_geizhals_match_and_optional(wh, gh):
    res = await deals.analyze(_ad(100))
    assert res["geizhals"] == {"name": "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo",
                               "url": "https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html",
                               "price_from": 139.0}
    plain = await deals.analyze(_ad(100, heading="Alter Holzschrank Eiche", ad_id="1000000003"))
    assert plain["geizhals"] is None and len(gh) == 1           # generic title: Geizhals not asked


async def test_geizhals_error_and_mismatch_give_none(wh, monkeypatch):
    async def boom(q, **kw):
        raise geizhals.GeizhalsError("HTTP 503")
    monkeypatch.setattr(deals.geizhals, "lookup", boom)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None

    async def other(q, **kw):
        return {"match": {"name": "Makita DDF484Z", "url": "https://geizhals.at/x-a1.html", "price_from": 99.0}}
    monkeypatch.setattr(deals.geizhals, "lookup", other)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None


async def test_few_comparables_use_geizhals(wh, gh):
    _, state = wh
    state["search"] = _search_json([100, 120])
    res = await deals.analyze(_ad(70))
    assert res["comparables"]["count"] == 2 and res["position"]["basis"] == "geizhals"
    assert res["position"]["new_price"] == 139.0


async def test_claude_only_when_asked(wh, monkeypatch):
    async def no_gh(q, **kw):
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
    async def no_gh(q, **kw):
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
    assert "deal-zones" in frag and "10 comparable ads" in frag and "fair" in frag
    assert "71 % of the new price · 10 comparable ads" in frag                # € 100 of € 139, by the used zones
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

    async def lookup(q, **kw):
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

    async def guess(q, **kw):                                                      # Geizhals' first hit, not confirmed
        return {**await lookup(q), "verified": False}
    monkeypatch.setattr(deals.geizhals, "lookup", guess)
    assert await deals.geizhals_match("Raspberry Pi 4 Modell 4GB") is None


async def test_geizhals_error_is_negatively_cached(wh, monkeypatch):
    n = []

    async def boom(q, **kw):
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
    async def no_gh(q, **kw):
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
    async def no_gh(q, **kw):
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


async def test_results_before_the_size_filter_are_recomputed(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    res = await deals.analyze(_ad(100))
    assert res["comparables"]["filters"] == deals.FILTERS
    old = {**res, "comparables": {k: v for k, v in res["comparables"].items() if k != "filters"}}
    deals.remember(old)                                          # stored by a version without the size filter
    deals.clear_cache()
    assert deals.cached(AD_ID) is None
    deals.remember(res)
    deals.clear_cache()
    assert deals.cached(AD_ID) is not None


def test_deals_page_rejects_non_ad_links(app_client):
    page = app_client.get("/deals", params={"q": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552"}).text
    assert "This link is not an ad on a supported site." in page and app_client.calls["search"] == 0



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
    assert out["filtered"] == {"category": 6, "kind": 2, "condition": 0, "outliers": 1}
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


@pytest.mark.parametrize("other, ad, ok", [
    ("Raspberry Pi 4 8GB", "Raspberry Pi 4 4GB", False),
    ("Raspberry Pi 4 Model B", "Raspberry Pi 4 4GB", True),                   # no size: may be any, kept
    ("Raspberry Pi 4 4 GB + 32GB SD-Karte", "Raspberry Pi 4 4GB", True),      # names the ad's size too
    ("Raspberry Pi 4 4GB", "Raspberry Pi 4 4 GB", True),                      # "4 GB" = "4GB"
    ("MacBook Air M1 16GB 256GB", "MacBook Air M1 8GB 256GB", False),
    ("MacBook Air M1 8GB 512GB", "MacBook Air M1 8GB 256GB", False),
    ("MacBook Air M1 256GB", "MacBook Air M1 8GB 256GB", True),
    ("MacBook Air M1 8 GB / 256 GB", "MacBook Air M1 8GB 256GB", True),
    ("Samsung T7 1,5 TB", "Samsung T7 1.5TB", True),
    ("Raspberry Pi 4 8GB", "Raspberry Pi 4", True),                           # ad without a size: no change
    ("Anker Powerbank 20000mAh", "Anker Powerbank 20.000 mAh", True),         # thousands separator
    ("Anker Powerbank 10.000 mAh", "Anker Powerbank 20000mAh", False),
    ("Samsung T7 2TB", "Samsung T7 2.0TB", True),
    ("MacBook Air M1 16/256GB", "MacBook Air M1 8GB 256GB", False),           # RAM/storage
    ("MacBook Air M1 8/256 GB", "MacBook Air M1 8GB 256GB", True),
    ("Raspberry Pi 4 / 8GB", "Raspberry Pi 4 4GB", False),                    # model / size, not RAM/storage
])
def test_same_size(other, ad, ok):
    assert deals._same_size(other, ad) is ok


def _fake_search(monkeypatch, ads):
    async def search_ads(q):
        return {"rows_found": len(ads), "ads": ads}
    monkeypatch.setattr(deals, "search_ads", search_ads)


async def test_comparables_leave_out_other_sizes(monkeypatch):
    pi = "2691;5013000"
    ads = [_summ(n, "Raspberry Pi 4 4 GB", p, pi) for n, p in enumerate([45, 50, 55, 60])]
    ads += [_summ(10, "Raspberry Pi 4 Model B", 52, pi), _summ(11, "Raspberry Pi 4 4 GB + 32GB SD-Karte", 58, pi)]
    ads += [_summ(20 + n, "Raspberry Pi 4 8GB", p, pi) for n, p in enumerate([80, 85, 90])]
    ads += [_summ(30, "Raspberry Pi 4 Gehäuse", 10, pi)]
    _fake_search(monkeypatch, ads)
    out = await deals.comparables("Raspberry Pi 4", AD_ID, 50, category_id="5013000", title="Raspberry Pi 4 4GB")
    assert out["count"] == 6 and out["stats"]["max"] == 60
    assert out["filtered"] == {"category": 0, "kind": 4, "condition": 0, "outliers": 0}   # 3 x 8GB + the case

    mac = [_summ(n, "MacBook Air M1 8GB 256GB", p, pi) for n, p in enumerate([600, 650, 700, 720])]
    mac += [_summ(10, "MacBook Air M1 8 GB / 256 GB", 680, pi), _summ(11, "MacBook Air M1 256GB", 640, pi)]
    mac += [_summ(20, "MacBook Air M1 16GB 256GB", 900, pi), _summ(21, "MacBook Air M1 8GB 512GB", 820, pi)]
    _fake_search(monkeypatch, mac)
    out = await deals.comparables("MacBook Air M1", AD_ID, 650, category_id="5013000", title="MacBook Air M1 8GB 256GB")
    assert out["count"] == 6 and out["stats"]["max"] == 720 and out["filtered"]["kind"] == 2


async def test_comparables_too_few_of_the_size_keep_the_accessory_filter(monkeypatch):
    pi = "2691;5013000"
    ads = [_summ(n, "Raspberry Pi 4 4GB", p, pi) for n, p in enumerate([50, 55])] + [_summ(5, "Raspberry Pi 4", 52, pi)]
    ads += [_summ(20 + n, "Raspberry Pi 4 8GB", p, pi) for n, p in enumerate([80, 85, 90, 95, 100, 70])]
    ads += [_summ(30, "Raspberry Pi 4 Gehäuse", 10, pi)]
    _fake_search(monkeypatch, ads)
    out = await deals.comparables("Raspberry Pi 4", AD_ID, 50, category_id="5013000", title="Raspberry Pi 4 4GB")
    assert out["count"] == 9 and out["filtered"]["kind"] == 1               # all sizes, but not the case
    assert out["stats"]["min"] == 50


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


# ------------------------------------------------------------------ Claude looks for the product on Geizhals
class ScriptedClaude:
    """Answers the Geizhals search: first the queries, then the pick (or an error)."""
    model = "fake-haiku"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return LLMResult(data=a, model=self.model)


@pytest.fixture
def no_auto_gh(monkeypatch):
    """The automatic lookup finds nothing; Geizhals' search pages (gh fixture) still answer Claude's searches."""
    async def nothing(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", nothing)


QUERIES = {"product": "Bosch GSR 18V-55 solo", "queries": ["Bosch GSR 18V-55 solo", "Bosch GSR 18V-55"]}


async def test_claude_finds_the_geizhals_product(wh, gh, no_auto_gh, monkeypatch):
    _, state = wh
    state["search"] = _search_json([100, 120])                  # few comparables: the new price places the ad
    fake = ScriptedClaude(QUERIES, {"pick": 1, "reason": "Genau dieses Modell, ohne Akku."})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(70))
    assert res["geizhals"] is None and res["position"]["pos"] is None and fake.calls == []   # never automatic

    res = await deals.analyze(_ad(70), find_geizhals=True)
    assert len(fake.calls) == 2 and "Bosch GSR 18V-55 Akkuschrauber" in fake.calls[0]["prompt"]
    assert "1. Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo · ab 139 €" in fake.calls[1]["prompt"]
    assert "2. Bosch GSR 18V-55 Set · ab 199.9 €" in fake.calls[1]["prompt"]
    assert gh == ["https://geizhals.at/?fs=Bosch+GSR+18V-55+solo&hloc=at",      # one request per search, no page
                  "https://geizhals.at/?fs=Bosch+GSR+18V-55&hloc=at"]
    assert res["geizhals"] == {"name": "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo",
                               "url": "https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html",
                               "price_from": 139.0, "by": "claude", "reason": "Genau dieses Modell, ohne Akku."}
    assert res["position"]["basis"] == "geizhals"

    again = await deals.analyze(_ad(70), find_geizhals=True)     # found already: no further Claude call
    assert len(fake.calls) == 2 and again["geizhals"]["by"] == "claude"
    deals.remember(again)
    deals.clear_cache()
    with dbmod.session() as db:                                 # the analysis gets old and is recomputed …
        row = db.get(dbmod.DealCheck, AD_ID)
        row.result = {**row.result, "checked_at": (dbmod.utcnow() - timedelta(hours=2)).isoformat()}
        db.add(row)
        db.commit()
    later = await deals.analyze(_ad(70))
    assert later["geizhals"]["by"] == "claude" and len(fake.calls) == 2         # … and keeps Claude's product


async def test_claude_finds_nothing_or_fails(wh, gh, no_auto_gh, monkeypatch):
    fake = ScriptedClaude(LLMError("rate limited"))
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100), find_geizhals=True)
    assert res["geizhals_error"] == "rate limited" and res["geizhals"] is None
    assert "geizhals_error" not in deals.cached(AD_ID)          # a failure is not kept: the next click tries again

    fake.answers = [QUERIES, {"pick": 0, "reason": "Nur Sets mit Akku gefunden."}]
    res = await deals.analyze(_ad(100), find_geizhals=True)
    assert res["geizhals"] is None and res["geizhals_none"] == "Nur Sets mit Akku gefunden."
    await deals.analyze(_ad(100), find_geizhals=True)          # Claude's "none" is kept with the analysis
    assert len(fake.calls) == 3

    fake.answers = [{"product": "", "queries": []}]
    res = await deals.analyze(_ad(100, ad_id="1000000005", heading="Alter Holzschrank Eiche"), find_geizhals=True)
    assert "no specific product" in res["geizhals_none"] and len(fake.calls) == 4 and len(gh) == 2


async def test_claude_geizhals_search_blocked(wh, no_auto_gh, monkeypatch):
    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(lambda r: httpx.Response(429)))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0))
    fake = ScriptedClaude(QUERIES)
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100), find_geizhals=True)
    assert "429" in res["geizhals_error"] and len(fake.calls) == 1          # no pick without results


async def test_the_ad_photo_counts_for_the_ads_own_site(monkeypatch):
    """Kleinanzeigen and eBay share i.ebayimg.com: an eBay ad's photo for Claude is an eBay download (statistics,
    eBay's VPN), not Kleinanzeigen's, both in the price check and in the Geizhals search."""
    from app.services import screening
    from app.willhaben import access_log

    sites = []
    monkeypatch.setattr(access_log, "record", lambda *a, platform="willhaben", **k: sites.append(platform))
    monkeypatch.setattr(screening, "_transport", httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})))
    fake = ScriptedClaude({"deal": "good", "reason": "Günstig."}, {"product": "", "queries": []})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    ad = _ad()
    ad.platform, ad.images = "ebay", ["https://i.ebayimg.com/images/g/qvYAAeSwmhhqy9Wh/s-l1600.jpg"]
    assert (await deals.ask_claude(ad, {}))["deal"] == "good"
    assert "none" in await deals.claude_geizhals(ad)
    assert all("Foto: ./foto1.jpg" in c["prompt"] for c in fake.calls) and sites == ["ebay", "ebay"]


def test_viewer_offers_claude_and_updates_the_hit(app_client, no_auto_gh, monkeypatch):
    from app.services import screening

    async def no_photo(*a, **k):
        return None
    monkeypatch.setattr(screening, "_download", no_photo)
    fake = ScriptedClaude(QUERIES, {"pick": 1, "reason": "Passt."})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    with dbmod.session() as db:
        a = dbmod.SearchAgent(name="Bohrer", source_url="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=bosch",
                              webapi_url="x")
        db.add(a)
        db.commit()
        db.refresh(a)
        h = dbmod.Hit(agent_id=a.id, ad_id=AD_ID, heading="Bosch GSR 18V-55", url="u", geizhals={})
        db.add(h)
        db.commit()
        db.refresh(h)
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal?hit={h.id}").text
    assert "No matching product found on Geizhals." in frag and "Let Claude search Geizhals" in frag
    assert f'hx-post="/deals/ad/willhaben/{AD_ID}/geizhals"' in frag and 'hx-target="closest section"' in frag
    assert fake.calls == []

    section = app_client.post(f"/deals/ad/willhaben/{AD_ID}/geizhals", data={"hit": str(h.id), "compact": "1"}).text
    assert section.lstrip().startswith("<section") and "New from € 139 on Geizhals" in section
    assert "found by Claude" in section and "Let Claude search Geizhals" not in section
    from app.services import jobs
    shown = jobs.snapshot()["recent"][0]                         # shown in the jobs indicator while Claude searched
    assert shown["label"] == "Claude searches Geizhals: „Bosch GSR 18V-55“" and shown["quiet"] is True
    assert shown["result"] == "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo"
    with dbmod.session() as db:
        assert db.get(dbmod.Hit, h.id).geizhals["price_from"] == 139.0
        assert db.get(dbmod.DealCheck, AD_ID).result["geizhals"]["by"] == "claude"


# ------------------------------------------------------------------ zones on read, condition filter, texts
async def test_zone_change_applies_to_cached_results_without_a_request(wh, gh):
    from app.services import price_zones
    calls, _ = wh
    res = await deals.analyze(_ad(100))                      # € 100 of € 139 = 71 %: fair by the default used zones
    assert res["position"]["level"] == "fair" and calls["search"] == 1
    deals.remember(res)
    price_zones.save({"used": [30, 50, 70, 90], "new": list(price_zones.DEFAULTS["new"])})
    assert deals.cached(AD_ID)["position"]["level"] == "expensive"            # from memory
    deals.clear_cache()
    assert deals.cached(AD_ID)["position"]["level"] == "expensive"            # revived from the DealCheck row
    again = await deals.analyze(_ad(100))
    assert again["position"]["level"] == "expensive" and calls["search"] == 1 and len(gh) == 2


async def test_a_stored_label_is_never_read(wh, monkeypatch):
    calls, _ = wh

    async def no_gh(q, **kw):
        return None

    async def fails(ad):
        return {"error": "rate limited"}
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    monkeypatch.setattr(deals, "claude_geizhals", fails)
    await deals.analyze(_ad(100))
    at, stored = deals._results[(AD_ID, "")]
    deals._results[(AD_ID, "")] = (at, {**stored, "position": {"pos": 0.99, "level": "very_expensive"}})
    assert deals.cached(AD_ID)["position"]["level"] == "fair"
    out = await deals.analyze(_ad(100), find_geizhals=True)                     # the geizhals_error answer too
    assert out["geizhals_error"] == "rate limited" and out["position"]["level"] == "fair" and calls["search"] == 1
    assert deals.rerate(None) is None and deals.rerate({"query": "x"}) == {"query": "x"}


def _cond(n, price, condition):
    return {**_summ(n, "iPhone 15 Pro 128GB", price, "2691;5013241"), "item_condition": condition}


async def test_comparables_in_another_condition_are_left_out(monkeypatch):
    ads = [_cond(n, p, "used") for n, p in enumerate([500, 520, 540, 560, 580])] + [_cond(5, 600, None)]
    ads += [_cond(10 + n, p, "new") for n, p in enumerate([900, 950])] + [_cond(20, 200, "defect")]
    _fake_search(monkeypatch, ads)
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 500, category_id="5013241", title="iPhone 15 Pro")
    assert out["count"] == 6 and out["filtered"]["condition"] == 3 and out["stats"]["max"] == 600   # used ad
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 500, category_id="5013241", title="iPhone 15 Pro",
                                  condition="defect")
    assert out["count"] == 9 and out["filtered"]["condition"] == 0          # a defective ad: nothing to pick
    _fake_search(monkeypatch, ads[2:])                                        # 3 used + 1 unknown left: kept all
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 500, category_id="5013241", title="iPhone 15 Pro")
    assert out["filtered"]["condition"] == 0
    new = [_cond(n, p, "new") for n, p in enumerate([900, 920, 940, 960])] + [_cond(9, 950, None)]
    _fake_search(monkeypatch, new + ads[:5])
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 950, category_id="5013241", title="iPhone 15 Pro",
                                  condition="new")
    assert out["count"] == 5 and out["filtered"]["condition"] == 5 and out["stats"]["min"] == 900


async def test_results_of_filter_version_2_are_recomputed(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    assert deals.FILTERS == 3
    res = await deals.analyze(_ad(100))
    deals.remember({**res, "comparables": {**res["comparables"], "filters": 2}})
    deals.clear_cache()
    assert deals.cached(AD_ID) is None


def test_deal_prompt_names_the_zones_and_the_condition():
    res = {"comparables": {"count": 4, "stats": deals.stats(USER_CASE), "query": "Bosch"},
           "geizhals": {"name": "Bosch GSR", "url": "u", "price_from": 202.30},
           "position": deals.position(139, deals.stats(USER_CASE), 202.30)}
    prompt = deals.deal_prompt(_ad(139), res, "Deutsch", None)
    assert "Zustand: unbekannt" in prompt
    assert ("Einordnung der App: fair – 68 % vom günstigsten Geizhals-Neupreis (Zonen des Nutzers für Gebrauchtes: "
            "Schnäppchen unter 30 %, günstig ab 30 %, fair ab 50 %, teuer ab 80 %, sehr teuer ab 90 %)") in prompt
    ad = _ad(139)
    ad.item_condition, ad.item_condition_label = "like_new", "Neuwertig"
    assert "Zustand laut Inserat: Neuwertig" in deals.deal_prompt(ad, res, "Deutsch", None)
    ad.item_condition, ad.item_condition_label = "new", None
    assert "Zustand angenommen: neu" in deals.deal_prompt(ad, res, "Deutsch", None)
    st = deals.stats([380, 390, 400, 410, 420, 430])
    res = {**res, "comparables": {"count": 6, "stats": st}, "position": deals.position(400, st, 100.0)}
    prompt = deals.deal_prompt(_ad(400), res, "Deutsch", None)
    assert "Einordnung der App: fair unter den Vergleichsanzeigen (die mittlere Hälfte gilt als fair)" in prompt
    assert "die Vergleichsanzeigen kosten im Mittel 405 % davon" in prompt
    res = {**res, "position": deals.position(400, st, 100.0, "defect")}
    assert "Einordnung der App: keine (laut Inserat defekt)" in deals.deal_prompt(_ad(400), res, "Deutsch", None)


def _render_deal(res, lang="de"):
    from app import i18n
    from app.main import templates
    i18n.set_request_lang(lang)
    try:
        return templates.env.get_template("_deal.html").render(res=res, error=None, compact=True, h=None)
    finally:
        i18n.set_request_lang("en")


def _res(price, prices, new_price, condition=None, label=None):
    st = deals.stats(prices) if prices else None
    return deals.rerate({"ad": {"ad_id": AD_ID, "platform": "willhaben", "title": "Bosch GSR 18V-55", "price": price,
                                "item_condition": condition, "item_condition_label": label},
                         "comparables": {"count": len(prices or []), "stats": st, "closest": [], "filtered": {},
                                         "scope": None, "query": "Bosch"},
                         "geizhals": {"name": "Bosch GSR", "url": "https://geizhals.at/x-a1.html",
                                      "price_from": new_price} if new_price else None,
                         "category_path": [], "claude": None})


def test_price_section_by_zones_in_german():
    html = _render_deal(_res(139, USER_CASE, 202.30))
    small = re.search(r'<small class="text-muted">([^<]*)</small>', html).group(1)
    assert small == "68 % vom Neupreis · 4 vergleichbare Anzeigen"              # never a leading " · "
    assert "Neu ab € 202,30 auf Geizhals" in html
    assert 'üblich bei Vergleichsanzeigen: <span class="whitespace-nowrap">€ 131–€ 155</span>' in html
    assert "Bei Gebrauchtem gilt 50–79 % vom Neupreis als „fair“ (€ 101,15 bis unter € 161,84)." in html
    assert "Zustand unbekannt – es gelten die Zonen für Gebrauchtes." in html and "Zonen ändern" in html
    assert 'href="/settings/prices"' in html and "deal-zones" in html and "diese Anzeige kostet" not in html
    assert 'aria-label="€ 139: fair, 68 % vom Neupreis"' in html
    html = _render_deal(_res(195.02, None, 199.0, "new", "Neu"))
    assert "Bei Neuware gilt 75–99 % vom Neupreis als „günstig“ (€ 149,25 bis unter € 199)." in html
    assert "Zustand laut Inserat: Neu." in html
    html = _render_deal(_res(400, None, 202.30))
    assert "ab 90 % vom Neupreis als „sehr teuer“ (ab € 182,07)" in html
    html = _render_deal(_res(20, None, 202.30, "new", None))                      # inferred: "angenommen"
    assert "unter 75 % vom Neupreis als „Schnäppchen“ (unter € 151,73)" in html and "Zustand angenommen: Neu." in html


def test_price_section_warnings_and_defect():
    html = _render_deal(_res(140, [130, 140, 150, 160, 170], 100.0), lang="en")
    assert "By the new price it is „very expensive“, among the comparable ads it would be „fair“" in html
    assert "outside the scale" in html and "Comparable ads cost 150 %" not in html
    html = _render_deal(_res(120, [130, 140, 150, 160, 170], 100.0, "new", "New"), lang="en")
    assert "Comparable ads cost 150 % of the new price on average" in html
    html = _render_deal(_res(400, [380, 390, 400, 410, 420, 430], 100.0), lang="en")
    assert "Geizhals new price not used: the comparable ads cost 405 % of it on average" in html
    assert "this ad is 400 % of new" in html and "Among the comparable ads the middle half" in html
    html = _render_deal(_res(50, PRICES, 100.0, "defect", "Defekt"), lang="en")
    assert "Defective according to the ad" in html and "deal-zones" not in html and "this ad is 50 % of new" in html
    assert "below the median" not in html and "10 comparable ads" in html          # not measured against working items
    assert "bg-bad-soft text-error" in html                                        # the red condition pill


def test_price_section_by_comparables_reads_the_stats_median():
    html = _render_deal(_res(100, PRICES, None))
    small = re.search(r'<small class="text-muted">([^<]*)</small>', html).group(1)
    assert small == "5 % unter dem Median · 10 vergleichbare Anzeigen"
    assert "Unter den Vergleichsanzeigen gilt die mittlere Hälfte (€ 82,50–€ 127,50) als fair" in html
    assert ">€ 82<" in html and ">€ 105<" in html and ">€ 128<" in html            # the marks below the bar
