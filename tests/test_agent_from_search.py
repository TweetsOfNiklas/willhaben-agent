"""Search agent created from a finished product search: several keyword searches per check plus the plan's filters.

Fakes only: a mocked willhaben search endpoint (one page per keyword); no Claude calls (screening off in the runs).
"""
import copy
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db as dbmod
from app.services.search_agents import create_agent_from_search, run_agent
from app.willhaben import public as public_mod
from app.willhaben.public import PublicClient, RateLimiter
from tests.conftest import load
from tests.test_search_agents import _set_attr

CAT = "kaufen-und-verkaufen/marktplatz/haushaltsgeraete-2678"


def _ad_json(ad_id, heading, price):
    tmpl = load("search_fahrrad_rows3.json")["advertSummaryList"]["advertSummary"][0]
    ad = copy.deepcopy(tmpl)
    ad["id"] = ad_id
    for name, value in (("ADID", ad_id), ("HEADING", heading), ("PRICE", str(price)), ("PRICE/AMOUNT", str(price)),
                        ("PRICE_FOR_DISPLAY", f"€ {price}"), ("BODY_DYN", "Gut erhalten, Abholung in Wien."),
                        ("SEO_URL", f"iad/kaufen-und-verkaufen/d/x-{ad_id}/")):
        _set_attr(ad, name, value)
    return ad


def _page(ads):
    base = load("search_fahrrad_rows3.json")
    base["advertSummaryList"]["advertSummary"] = ads
    base["rowsFound"] = len(ads)
    return base


PLAN = {"summary": "Leiser Geschirrspüler, 60 cm, bis 300 €, gerne Bosch oder Siemens, Abholung in Wien möglich.",
        "max_price": 300, "must_have": ["60 cm"], "nice_to_have": ["leise"], "exclude": ["defekt"],
        "queries": [{"keyword": "Geschirrspüler", "price_from": None, "price_to": None, "category_hint": None,
                     "category_path": None, "why": "direkt"},
                    {"keyword": "Spülmaschine", "price_from": 20, "price_to": None, "category_hint": "Haushalt",
                     "category_path": CAT, "why": "Synonym"},
                    {"keyword": "Geschirspüler", "price_from": None, "price_to": None, "category_hint": None,
                     "category_path": None, "why": "Tippfehler"}]}


def _pages():
    # ad 2000000002 is found by two searches; 3 = "Suche", 4 = far over budget (> 1.5 × 300), 5 = excluded word
    return {"geschirrspüler": [_ad_json("2000000001", "Bosch Geschirrspüler 60cm leise", 250),
                               _ad_json("2000000002", "Siemens Geschirrspüler", 180),
                               _ad_json("2000000003", "Suche Geschirrspüler", 1),
                               _ad_json("2000000004", "Miele Geschirrspüler G7000 neu", 900)],
            "spülmaschine": [_ad_json("2000000002", "Siemens Geschirrspüler", 180),
                             _ad_json("2000000005", "AEG Spülmaschine defekt", 40),
                             _ad_json("2000000006", "Spülmaschine Beko", 90)],
            "geschirspüler": [_ad_json("2000000007", "Geschirspüler Bauknecht", 120)]}


def _search_row(plan=PLAN, status="done") -> int:
    with dbmod.session() as db:
        row = dbmod.ProductSearch(description="Geschirrspüler 60 cm, leise, bis 300 €", status=status, plan=plan,
                                  settings={"depth": "normal"}, results=[], requests=3, candidates=4)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


@pytest.fixture
def wh():
    """Mocked willhaben search: one page per keyword, per-keyword status, counts PublicClient.search calls."""
    state = {"pages": _pages(), "status": {}, "keywords": [], "search_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        kw = parse_qs(urlsplit(str(request.url)).query).get("keyword", [""])[0].lower()
        state["keywords"].append(kw)
        status = state["status"].get(kw, 200)
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json=_page(state["pages"].get(kw, [])))

    client = PublicClient(transport=httpx.MockTransport(handler))
    client.limiter = RateLimiter(0)          # own limiter: a 429 cool-down must not leak into other tests
    real_search = client.search

    async def counting_search(url, cache_s=0):
        state["search_calls"] += 1
        return await real_search(url, cache_s=cache_s)

    client.search = counting_search
    public_mod.set_public(client)
    yield state
    public_mod.set_public(None)


def _hits(agent_id):
    with dbmod.session() as db:
        return {h.ad_id: h for h in db.exec(select(dbmod.Hit).where(dbmod.Hit.agent_id == agent_id))}


def _agent(agent_id):
    with dbmod.session() as db:
        return db.get(dbmod.SearchAgent, agent_id)


# ------------------------------------------------------------------ create
def test_create_agent_from_search():
    sid = _search_row()
    a = create_agent_from_search(sid)
    assert a.search_id == sid and len(a.queries) == 3
    assert [q["keyword"] for q in a.queries] == ["Geschirrspüler", "Spülmaschine", "Geschirspüler"]
    q1, q2 = a.queries[0], a.queries[1]
    assert q1["webapi_url"].startswith("https://www.willhaben.at/webapi/ad-search/search/atz/seo/"
                                       "kaufen-und-verkaufen/marktplatz?")
    assert "sort=1" in q1["webapi_url"] and "rows=30" in q1["webapi_url"] and "PRICE_FROM" not in q1["webapi_url"]
    assert q1["source_url"] == "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=Geschirrsp%C3%BCler"
    assert f"/seo/{CAT}?" in q2["webapi_url"] and "PRICE_FROM=20" in q2["webapi_url"]
    assert q2["source_url"].startswith(f"https://www.willhaben.at/iad/{CAT}?") and "sort" not in q2["source_url"]
    assert q2["price_from"] == 20 and q2["category_path"] == CAT and q2["why"] == "Synonym"
    # the first search mirrored into the single-URL fields (agents page link, /api/agents, MCP)
    assert a.source_url == q1["source_url"] and a.webapi_url == q1["webapi_url"]
    assert a.name.startswith("Leiser Geschirrspüler") and len(a.name) <= 61 and a.name.endswith("…")
    assert a.interval_s == 3 * 300                       # three requests per check -> three times less often
    assert a.screen is True and a.screen_min == "match" and a.screen_hide is None
    assert a.screen_brief.splitlines() == ["Geschirrspüler 60 cm, leise, bis 300 €", "Budget: bis 300 €",
                                           "Muss: 60 cm", "Wünsche: leise", "Ausschluss: defekt"]
    assert a.plan == {"summary": PLAN["summary"], "max_price": 300, "must_have": ["60 cm"],
                      "nice_to_have": ["leise"], "exclude": ["defekt"]}
    assert not a.baseline_done and a.last_run_at is None  # the baseline is left to the scheduler

    b = create_agent_from_search(sid, name="Spüler", interval_s=60, screen=False)
    assert b.name == "Spüler" and b.interval_s == 120 and not b.screen and b.screen_brief is None   # min interval


def test_screen_brief_has_the_answers():
    plan = {**PLAN, "questions": [{"id": "kratzer", "text": "Sind Kratzer okay, wenn der Preis niedriger ist?",
                                   "options": ["Ja", "Nein"]},
                                  {"id": "marke", "text": "Muss es Bosch sein?", "options": ["Ja", "Nein"]}]}
    sid = _search_row(plan=plan)
    with dbmod.session() as db:
        row = db.get(dbmod.ProductSearch, sid)
        row.answers = {"kratzer": "Ja, für weniger Geld"}
        db.add(row)
        db.commit()
    a = create_agent_from_search(sid)
    assert a.screen_brief.splitlines()[-3:] == ["Ausschluss: defekt", "Rückfragen und Antworten:",
                                                "– Sind Kratzer okay, wenn der Preis niedriger ist?: Ja, für weniger Geld"]
    assert "Bosch sein" not in a.screen_brief                     # unanswered question left out


def test_create_needs_a_plan():
    with pytest.raises(KeyError):
        create_agent_from_search(9999)
    with pytest.raises(ValueError):
        create_agent_from_search(_search_row(plan=None, status="error"))
    with pytest.raises(ValueError):
        create_agent_from_search(_search_row(plan={**PLAN, "queries": []}))


# ------------------------------------------------------------------ run
async def test_run_merges_filters_and_reports_new(wh):
    a = create_agent_from_search(_search_row(), screen=False)
    r1 = await run_agent(a.id)
    assert r1.error is None and r1.baseline and not r1.new
    assert wh["keywords"] == ["geschirrspüler", "spülmaschine", "geschirspüler"] and wh["search_calls"] == 3
    assert r1.rows_found == 4 + 3 + 1
    # merged and de-duplicated; "Suche", far over budget and "defekt" are never stored
    assert set(_hits(a.id)) == {"2000000001", "2000000002", "2000000006", "2000000007"}
    stored = _agent(a.id)
    assert stored.baseline_done and stored.last_error is None and stored.last_rows_found == 8

    wh["pages"]["geschirspüler"] += [_ad_json("2000000008", "Neuer Geschirrspüler Bosch", 200),
                                     _ad_json("2000000009", "Suche Spülmaschine", 50),
                                     _ad_json("2000000010", "Geschirrspüler defekt", 20)]
    r2 = await run_agent(a.id)
    assert r2.error is None and not r2.baseline
    assert [h.ad_id for h in r2.new] == ["2000000008"]
    assert "2000000009" not in _hits(a.id) and "2000000010" not in _hits(a.id)

    r3 = await run_agent(a.id)
    assert not r3.new


async def test_one_failing_search_does_not_stop_the_others(wh):
    a = create_agent_from_search(_search_row(), screen=False)
    wh["status"]["spülmaschine"] = 500
    r = await run_agent(a.id)
    assert r.error is None and wh["search_calls"] == 3
    assert set(_hits(a.id)) == {"2000000001", "2000000002", "2000000007"}
    assert r.rows_found == 4 + 1
    err = _agent(a.id).last_error
    assert "search 2/3 failed" in err and "500" in err


async def test_blocked_stops_the_remaining_searches(wh):
    a = create_agent_from_search(_search_row(), screen=False)
    wh["status"]["geschirrspüler"] = 429
    r = await run_agent(a.id)
    assert wh["search_calls"] == 1                      # searches 2 and 3 never ran
    assert r.error and "429" in r.error                 # nothing succeeded: the single-URL error path
    stored = _agent(a.id)
    assert "429" in stored.last_error and not stored.baseline_done and not _hits(a.id)


async def test_blocked_later_keeps_what_was_found(wh):
    a = create_agent_from_search(_search_row(), screen=False)
    wh["status"]["spülmaschine"] = 403
    r = await run_agent(a.id)
    assert wh["search_calls"] == 2 and r.error is None
    assert set(_hits(a.id)) == {"2000000001", "2000000002"}
    err = _agent(a.id).last_error
    assert "search 2/3 failed" in err and "remaining searches skipped: 1" in err


# ------------------------------------------------------------------ pages
@pytest.fixture
def app_client(wh, monkeypatch):
    from app import scheduler
    from app.main import app

    scheduled = []
    with TestClient(app) as c:
        monkeypatch.setattr(scheduler, "schedule_agent", lambda agent, run_soon=True: scheduled.append(agent.id))
        c.scheduled = scheduled
        yield c


def test_create_from_the_product_search_page(app_client):
    sid = _search_row()
    page = app_client.get(f"/deals?search={sid}").text
    assert "Create a search agent from this search" in page and f'action="/deals/search/{sid}/agent"' in page
    assert "3 searches per check" in page and 'name="interval_min" value="15"' in page

    r = app_client.post(f"/deals/search/{sid}/agent", data={"name": "", "interval_min": "", "screen_on": "on"},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/agents"
    with dbmod.session() as db:
        agents = list(db.exec(select(dbmod.SearchAgent)))
    assert len(agents) == 1 and agents[0].search_id == sid and agents[0].screen and agents[0].interval_s == 900
    assert app_client.scheduled == [agents[0].id]

    page = app_client.get("/agents").text
    assert "3 searches: Geschirrspüler · Spülmaschine · Geschirspüler" in page
    assert f"Searches from product search #{sid}" in page

    page = app_client.get(f"/deals?search={sid}").text
    assert f"Search agent #{agents[0].id} exists" in page and "Create a search agent from this search" not in page

    # the normal edit form keeps working for these agents
    r = app_client.post(f"/agents/{agents[0].id}/edit", data={"name": "Spüler", "interval_min": "20",
                                                               "notify_on": "on"}, follow_redirects=False)
    assert r.status_code == 303
    a = _agent(agents[0].id)
    assert a.name == "Spüler" and a.interval_s == 1200 and not a.screen and len(a.queries) == 3


def test_edit_removes_single_searches(app_client):
    a = create_agent_from_search(_search_row(), screen=False)
    form = {"name": a.name, "interval_min": "15"}
    page = app_client.get("/agents").text
    assert 'name="drop_query" value="0"' in page and 'name="drop_query" value="2"' in page

    # removing every search is refused: nothing changes
    r = app_client.post(f"/agents/{a.id}/edit", data={**form, "name": "x", "drop_query": ["0", "1", "2"]},
                        follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    b = _agent(a.id)
    assert b.name == a.name and len(b.queries) == 3

    # removing the first one moves the agent's link to the next
    r = app_client.post(f"/agents/{a.id}/edit", data={**form, "drop_query": ["0", "2"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/agents"
    b = _agent(a.id)
    assert [q["keyword"] for q in b.queries] == ["Spülmaschine"]
    assert b.source_url == b.queries[0]["source_url"] and b.webapi_url == b.queries[0]["webapi_url"]
    assert 'name="drop_query"' not in app_client.get("/agents").text   # the last search has no remove box


def test_create_route_errors(app_client):
    assert app_client.post("/deals/search/9999/agent", data={}, follow_redirects=False).status_code == 404
    sid = _search_row(plan=None, status="error")
    r = app_client.post(f"/deals/search/{sid}/agent", data={"interval_min": "10"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=noplan" in r.headers["location"]
    assert "Create a search agent" not in app_client.get(f"/deals?search={sid}").text
    with dbmod.session() as db:
        assert not list(db.exec(select(dbmod.SearchAgent)))



def test_one_agent_for_every_site_of_a_search(app_client):
    from app import platforms
    from tests.fake_platform import FakePlatform

    platforms.set_platform("fake", FakePlatform())
    try:
        sid = _search_row()
        with dbmod.session() as db:
            row = db.get(dbmod.ProductSearch, sid)
            row.settings = {**(row.settings or {}), "platforms": ["willhaben", "fake"]}
            db.add(row)
            db.commit()
        page = app_client.get(f"/deals?search={sid}").text
        assert "Sites:" in page and 'name="sites" value="fake"' in page
        r = app_client.post(f"/deals/search/{sid}/agent", data={"name": "Spüler", "interval_min": "",
                                                                "sites": ["willhaben", "fake"]}, follow_redirects=False)
        assert r.status_code == 303
        with dbmod.session() as db:
            agents = list(db.exec(select(dbmod.SearchAgent).order_by(dbmod.SearchAgent.id)))
        assert [(a.platform, a.name) for a in agents] == [("willhaben", "Spüler")]        # one agent, both sites
        a = agents[0]
        n = len(a.queries) // 2
        assert [q["platform"] for q in a.queries] == ["willhaben"] * n + ["fake"] * n and a.search_id == sid
        from app.services.search_agents import agent_platforms, default_interval_s
        assert agent_platforms(a) == ["willhaben", "fake"] and a.interval_s == default_interval_s(n)   # per site
        assert platforms.multi() is True
        page = app_client.get(f"/deals?search={sid}").text
        assert "exists" in page and "Create a search agent from this search" not in page
        listing = app_client.get("/agents").text
        assert "willhaben · Fake" in listing

        # removing every willhaben search moves the main search to the other site
        r = app_client.post(f"/agents/{a.id}/edit", data={"name": a.name, "interval_min": "15",
                                                          "drop_query": [str(i) for i in range(n)]},
                            follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/agents"
        b = _agent(a.id)
        first = b.queries[0]
        assert agent_platforms(b) == ["fake"] and b.platform == "fake" and b.spec == first["spec"]
        assert b.source_url == first["source_url"] and b.webapi_url == (first["webapi_url"] or first["source_url"])
    finally:
        platforms.reset()
