"""Produktsuche on Facebook Marketplace: price bands inside the budget (round-robin, capped by depth, no comparables)
and the descriptions of the shortlisted Facebook offers loaded before the ranking. facebook.com is an httpx
MockTransport over the recorded search and listing pages."""
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import set_setting
from app.llm.prompts import SEARCH_RANK_SCHEMA
from app.platforms import facebook as fb
from app.platforms.ratelimit import RateLimiter
from app.services import product_search as ps
from tests.conftest import FIXTURES
from tests.test_product_search import _clean, _finish, claude, wh  # noqa: F401  (pytest fixtures)

PAGE = (FIXTURES / "facebook_search.html").read_text(encoding="utf-8")
ITEM = (FIXTURES / "facebook_item.html").read_text(encoding="utf-8")
ITEM_ID = "1438848741764802"
IDS = [a.ad_id for a in fb.parse_search(PAGE).ads]          # 99 €, 50 €, 100 €
FB_PLAN = {"summary": "Ein Fahrrad bis 300 €.", "max_price": 300, "must_have": [], "nice_to_have": [], "exclude": [],
           "queries": [{"keyword": "Fahrrad", "why": "direkt"},
                       {"keyword": "Mountainbike", "price_from": 20, "price_to": 200, "why": "Synonym"},
                       {"keyword": "MTB", "why": "kurz"}]}


@pytest.fixture
def site():
    """facebook.com: a search without maxPrice answers the recorded page; each band (minPrice, maxPrice) its own
    copy with new ids (the same band of another term: the same ids again); a listing page the recorded one under the
    asked id, "<html>nothing</html>" for ids in `gone`. `queue`: answers to send first (an int is a status)."""
    state = {"queue": [], "searches": [], "items": [], "gone": set(), "bands": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/marketplace/item/"):
            ad_id = path.split("/")[3]
            state["items"].append(ad_id)
        else:
            q = parse_qs(urlsplit(str(request.url)).query)
            state["searches"].append((q["query"][0], q.get("minPrice", [None])[0], q.get("maxPrice", [None])[0]))
        if state["queue"]:
            item = state["queue"].pop(0)
            return httpx.Response(item) if isinstance(item, int) else httpx.Response(200, text=item)
        if path.startswith("/marketplace/item/"):
            if ad_id in state["gone"]:
                return httpx.Response(200, text="<html>nothing</html>")
            return httpx.Response(200, text=ITEM.replace(ITEM_ID, ad_id))
        band = state["searches"][-1][1:]
        if band[1] is None:                             # no maxPrice: a whole-range search
            return httpx.Response(200, text=PAGE)
        tag = state["bands"].setdefault(band, str(len(state["bands"]) + 1))
        page = PAGE
        for i in IDS:
            page = page.replace(i, tag + i)
        return httpx.Response(200, text=page)

    c = fb.FacebookClient(httpx_transport=httpx.MockTransport(handler))
    c.limiter = fb._limiter = RateLimiter(0, name="Facebook")      # the platform's limiter is the client's
    fb.set_client(c)
    platforms.reset()
    set_setting("platform_facebook_enabled", "1")
    set_setting(fb.LOCATION_KEY, json.dumps({"loc": "vienna", "radius": "40", "place": "Wien"}))
    yield state
    fb.set_client(None)
    fb._limiter = None
    platforms.reset()


def test_price_bands_split_the_budget(site):
    plan = ps.normalize_plan(FB_PLAN, 3)
    assert [ps.price_bands(q, plan) for q in plan["queries"]] == [
        [(None, 150.0), (150.0, 300.0)], [(20, 110.0), (110.0, 200.0)], [(None, 150.0), (150.0, 300.0)]]
    assert ps.price_bands({"keyword": "x"}, {"max_price": None}) == []        # no budget: nothing to split
    assert ps.price_bands({"keyword": "x", "price_from": 299}, {"max_price": 300}) == []
    settings = ps.make_settings("normal", sites=["willhaben", "facebook"])
    # runs are query-major (willhaben, facebook); facebook's are 1, 3, 5: every lower half, then the upper ones
    assert ps.band_runs(plan, settings) == [(1, None, 150.0), (3, 20, 110.0), (5, None, 150.0), (1, 150.0, 300.0)]
    assert len(ps.band_runs(plan, ps.make_settings("deep", sites=["facebook"]))) == 6
    assert ps.band_runs(plan, ps.make_settings(sites=["willhaben"])) == []


async def test_facebook_runs_in_price_bands(site):
    plan = ps.normalize_plan(FB_PLAN, 3)
    calls = []
    found = await ps.run_queries(plan, ps.make_settings("normal", sites=["facebook"]),
                                 on_progress=lambda d, total, sent: calls.append((d, total, sent)))
    # the whole-range searches first (no price sent: drop_reason applies price_to), then the bands round-robin
    assert site["searches"] == [("Fahrrad", None, None), ("Mountainbike", "20", None), ("MTB", None, None),
                                ("Fahrrad", None, "150"), ("Mountainbike", "20", "110"), ("MTB", None, "150"),
                                ("Fahrrad", "150", "300")]
    assert found["requests"] == 7 and calls[-1] == (7, 7, 7) and found["warning"] is None
    rows = found["per_query"]
    assert len(rows) == 3 and [len(r.get("bands") or []) for r in rows] == [2, 1, 1]
    low, high = rows[0]["bands"]
    assert (low["price_from"], low["price_to"], low["found"], low["kept"]) == (None, 150.0, 3, 3)
    assert (high["price_from"], high["price_to"], high["kept"]) == (150.0, 300.0, 3)
    assert "maxPrice=150" in low["url"] and "spec" not in low
    assert rows[0]["found"] == 9 and rows[0]["kept"] == len(found["pools"][0]) == 9
    assert rows[1]["kept"] == 3 and rows[1]["bands"][0]["kept"] == 3      # its base page: the same ads again
    assert rows[2]["bands"][0]["kept"] == 0                                 # the same band as Fahrrad's: dupes
    assert sorted(found["prices"][0]) == [50, 99, 100] and found["prices"][1] == [99, 50, 100]   # bands: no comparables
    terms = ps.by_term(rows)                                                # still one row per search term
    assert [(t["keyword"], t["found"], t["kept"]) for t in terms] == [("Fahrrad", 9, 9), ("Mountainbike", 6, 3),
                                                                       ("MTB", 6, 0)]
    site["searches"].clear()
    found = await ps.run_queries(plan, ps.make_settings("deep", sites=["facebook"]))
    assert len(site["searches"]) == 9 and [len(r["bands"]) for r in found["per_query"]] == [2, 2, 2]


async def test_bands_follow_their_search_and_a_block(site):
    plan = ps.normalize_plan(FB_PLAN, 3)
    site["queue"] = [500]                               # Fahrrad's whole-range search fails: no bands for it
    found = await ps.run_queries(plan, ps.make_settings("normal", sites=["facebook"]))
    rows = found["per_query"]
    assert rows[0]["error"] and all(b["skipped"] for b in rows[0]["bands"])
    assert ("Fahrrad", None, "150") not in site["searches"] and found["requests"] == 5
    site["searches"].clear()
    site["queue"] = [PAGE, PAGE, PAGE, 429]              # the first band is refused: the rest are not sent
    found = await ps.run_queries(plan, ps.make_settings("normal", sites=["facebook"]))
    rows = found["per_query"]
    assert "HTTP 429" in rows[0]["bands"][0]["error"] and rows[0]["error"] is None
    assert rows[1]["bands"][0]["skipped"] and rows[0]["bands"][1]["skipped"] and len(site["searches"]) == 4
    assert "stopped after 3 searches there" in found["warning"]


async def test_descriptions_of_the_shortlist_are_loaded(site):
    plan = ps.normalize_plan(FB_PLAN, 3)
    found = await ps.run_queries(plan, ps.make_settings("normal", sites=["facebook"]))
    picked = ps.pick_candidates(found["pools"], cap=ps.MAX_CANDIDATES + ps.MAX_POOL)
    assert len(picked) == 12 and all(ad.body is None for _, ad in picked)
    site["gone"] = {IDS[0]}
    progress = []
    picked2, sent, warning = await ps.load_details(picked, found["per_query"],
                                                   on_progress=lambda d, total: progress.append((d, total)))
    assert sent == 8 and len(site["items"]) == 8 and site["items"][0] == IDS[0] and warning is None
    assert progress[-1] == (8, 8) and len(picked2) == 11 and IDS[0] not in {ad.ad_id for _, ad in picked2}
    texts = [ad for _, ad in picked2 if ad.body]
    assert len(texts) == 7 and texts[0].body.startswith("1,70m x 2,20m") and texts[0].location == "Oberwart, BGLD."
    assert texts[0].item_condition == "used"
    rows = found["per_query"]
    assert sum(r.get("details", 0) for r in rows) == 7 and rows[0]["gone"] == 1
    card = ps.card(0, texts[0], plan, found["prices"][0])
    assert card["text"].startswith("1,70m x 2,20m")
    # again: the pages loaded before come from the client's cache and cost nothing; a refusal stops with a warning
    for _, ad in picked2:
        ad.body = None
    site["items"].clear()
    site["queue"] = [429]
    picked3, sent, warning = await ps.load_details(picked2, found["per_query"])
    assert sent == 1 and len(site["items"]) == 1 and len(picked3) == 11
    assert "Facebook Marketplace paused loading the descriptions" in warning


async def test_search_with_bands_and_descriptions_end_to_end(site, wh, claude):  # noqa: F811
    claude.plan = FB_PLAN
    sid = ps.start("Ein Fahrrad bis 300 €", ps.make_settings("normal", photos=False, ask=False,
                                                             sites=["willhaben", "facebook"]))
    await _finish()
    row = ps.get(sid)
    assert row.status == "done" and row.error is None
    assert row.requests == 3 + 7                        # searches: willhaben, Facebook with 4 bands
    assert row.progress == {"done": 8, "total": 8, "step": "details"}
    rank_prompt = next(c["prompt"] for c in claude.calls if c["schema"] is SEARCH_RANK_SCHEMA)
    assert "1,70m x 2,20m" in rank_prompt
    fb_rows = [q for q in row.queries if q["platform"] == "facebook"]
    assert [len(q.get("bands") or []) for q in fb_rows] == [2, 1, 1] and sum(q.get("details", 0) for q in fb_rows) == 8
    assert all("bands" not in q for q in row.queries if q["platform"] == "willhaben")

    from app.main import app
    with TestClient(app) as c:
        frag = c.get(f"/deals/search/{sid}").text
    tbl = frag[frag.index('data-keep="queries"'):frag.index("</table>")]
    assert tbl.count("price bands") == 3 and tbl.count("Facebook Marketplace:</span>") == 3
    assert "descriptions loaded" in tbl and "maxPrice=150" in tbl and ">up to € 150 (3, <span" in tbl


def test_progress_shows_bands_and_descriptions(site, wh, claude):  # noqa: F811
    from app import db as dbmod
    from app.main import app

    plan = ps.normalize_plan(FB_PLAN, 3)
    with TestClient(app) as c:                          # started first: its startup ends running searches
        with dbmod.session() as db:
            row = dbmod.ProductSearch(description="Fahrrad", status="searching", plan=plan,
                                      settings=ps.make_settings(sites=["facebook"]),
                                      progress={"done": 4, "total": 7, "bands": 4})
            db.add(row)
            db.commit()
            db.refresh(row)
        ps._active.add(row.id)
        frag = c.get(f"/deals/search/{row.id}").text
        assert "4 of 7 searches" in frag and "incl. 4 Facebook price bands" in frag
        ps._update(row.id, progress={"done": 2, "total": 8, "step": "details"})
        frag = c.get(f"/deals/search/{row.id}").text
        assert "Descriptions of the shortlisted Facebook offers: 2 of 8" in frag
    ps._active.discard(row.id)
