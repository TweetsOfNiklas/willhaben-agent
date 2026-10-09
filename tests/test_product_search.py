"""Produktsuche: Claude plans willhaben searches from a description, the app runs them anonymously, Claude ranks.

Fakes only: a mocked willhaben search endpoint and a fake Claude backend.
"""
import asyncio
import copy
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db as dbmod
from app.llm import base as llm_base
from app.llm.base import LLMError, LLMResult
from app.llm.prompts import SEARCH_PLAN_SCHEMA, SEARCH_RANK_SCHEMA
from app.services import product_search as ps
from app.willhaben import public as public_mod
from app.willhaben.parse import AdSummary
from app.willhaben.public import PublicClient, RateLimiter
from tests.conftest import load
from tests.test_search_agents import _set_attr

FAKE_CLI = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]


def _ad_json(ad_id, heading, price, body="Gut erhalten, Abholung in Wien.", private=True):
    tmpl = load("search_fahrrad_rows3.json")["advertSummaryList"]["advertSummary"][0]
    ad = copy.deepcopy(tmpl)
    ad["id"] = ad_id
    for name, value in (("ADID", ad_id), ("HEADING", heading), ("PRICE", str(price)), ("PRICE/AMOUNT", str(price)),
                        ("PRICE_FOR_DISPLAY", f"€ {price}"), ("BODY_DYN", body), ("ISPRIVATE", "1" if private else "0"),
                        ("SEO_URL", f"iad/kaufen-und-verkaufen/d/x-{ad_id}/")):
        _set_attr(ad, name, value)
    return ad


def _page(ads):
    base = load("search_fahrrad_rows3.json")
    base["advertSummaryList"]["advertSummary"] = ads
    base["rowsFound"] = len(ads)
    return base


# keyword -> ads; "geschirrspüler" and "spülmaschine" share ad 2000000002
PAGES = {
    "geschirrspüler": [_ad_json("2000000001", "Bosch Geschirrspüler 60cm leise", 250),
                       _ad_json("2000000002", "Siemens Geschirrspüler", 180),
                       _ad_json("2000000003", "Suche Geschirrspüler", 1),
                       _ad_json("2000000004", "Miele Geschirrspüler G7000 neu", 900),
                       *[_ad_json(f"20000001{n:02d}", f"Geschirrspüler Nr {n}", p) for n, p in
                         enumerate([150, 200, 220, 260, 300, 320])]],
    "spülmaschine": [_ad_json("2000000002", "Siemens Geschirrspüler", 180),
                     _ad_json("2000000005", "AEG Spülmaschine defekt", 40),
                     _ad_json("2000000006", "Spülmaschine Beko, Zahlung nur per Western Union", 90)],
    "geschirspüler": [_ad_json("2000000007", "Geschirspüler Bauknecht", 120)],
}


@pytest.fixture(autouse=True)
def _clean():
    ps._active.clear()
    yield
    ps._active.clear()


@pytest.fixture
def wh():
    """Mocked willhaben search: records keywords in order and the highest number of parallel requests."""
    state = {"keywords": [], "inflight": 0, "max_inflight": 0, "status": {}}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["inflight"] += 1
        state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        try:
            await asyncio.sleep(0.01)
            kw = parse_qs(urlsplit(str(request.url)).query).get("keyword", [""])[0]
            state["keywords"].append(kw)
            status = state["status"].get(kw, 200)
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, json=_page(PAGES.get(kw.lower(), [])))
        finally:
            state["inflight"] -= 1

    client = PublicClient(transport=httpx.MockTransport(handler))
    client.limiter = RateLimiter(0)                         # no waiting; own limiter so blocks don't leak
    public_mod.set_public(client)
    yield state
    public_mod.set_public(None)


PLAN = {"summary": "Leiser Geschirrspüler, 60 cm, bis 300 €.", "max_price": 300,
        "must_have": ["60 cm"], "nice_to_have": ["leise"], "exclude": ["defekt", "nur Versand"],
        "queries": [{"keyword": "Geschirrspüler", "price_from": None, "price_to": None, "category_hint": None,
                     "category_path": None, "why": "direkt"},
                    {"keyword": "Spülmaschine", "price_from": 20, "price_to": 400, "category_hint": "Haushaltsgeräte",
                     "category_path": None, "why": "Synonym"},
                    {"keyword": "Geschirspüler", "why": "Tippfehler"}]}


class FakeClaude:
    model = "fake-sonnet"

    def __init__(self, plan=None, rank=None, fail_rank=False, delay=0.0):
        self.calls, self.purposes = [], []
        self.plan, self.rank, self.fail_rank, self.delay = plan or PLAN, rank, fail_rank, delay

    def backend(self, purpose=None):
        self.purposes.append(purpose)
        return self

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.delay:
            await asyncio.sleep(self.delay)
        if schema is SEARCH_PLAN_SCHEMA:
            return LLMResult(data=copy.deepcopy(self.plan), model=self.model)
        assert schema is SEARCH_RANK_SCHEMA
        if self.fail_rank:
            raise LLMError("overloaded")
        data = self.rank or {"results": [{"nr": 1, "fit": 92, "reason": "Leise, 60 cm, gut im Budget.", "red_flags": []},
                                         {"nr": 2, "fit": 70, "reason": "Günstig, Lautstärke unklar.",
                                          "red_flags": ["Zustand unklar"]}],
                             "note": "Gute Auswahl unter 300 €."}
        return LLMResult(data=data, model=self.model)


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(ps, "get_backend", fake.backend)
    return fake


def _summary(ad_id, heading, price, body=None, private=True):
    return AdSummary(ad_id=ad_id, heading=heading, body=body, price=price, price_display=None, old_price=None,
                     location="Wien", postcode=None, state=None, published_at=None, changed_at=None, end_date=None,
                     url=f"https://www.willhaben.at/iad/object?adId={ad_id}", image_url=None, is_private=private,
                     is_bumped=False, status="active")


# ------------------------------------------------------------------ plan
def test_depth_bounds_queries_with_hard_cap():
    assert [ps.max_queries(ps.make_settings(d)) for d in ("normal", "thorough", "deep")] == [3, 6, 12]
    assert ps.max_queries(ps.make_settings("bogus")) == 3
    many = {"queries": [{"keyword": f"Wort{i}", "why": ""} for i in range(30)]}
    assert len(ps.normalize_plan(many, 6)["queries"]) == 6
    assert len(ps.normalize_plan(many, 50)["queries"]) == ps.MAX_QUERIES == 12


def test_normalize_plan_cleans_claude_output():
    plan = ps.normalize_plan({
        "summary": "  x ", "max_price": -5, "must_have": ["a", 3, "  "], "exclude": "defekt",
        "queries": [{"keyword": "  iPhone   13 ", "price_from": 500, "price_to": 100, "why": "w",
                     "category_path": "kaufen-und-verkaufen/marktplatz/handys-telefone-2691"},
                    {"keyword": "iphone 13", "why": "dup"},
                    {"keyword": "", "why": "empty"},
                    {"keyword": "Handy", "price_to": True, "category_path": "../../etc/passwd", "category_hint": "Handys"},
                    "junk"]}, 6)
    assert plan["summary"] == "x" and plan["max_price"] is None and plan["must_have"] == ["a"] and plan["exclude"] == []
    q1, q2 = plan["queries"]
    assert q1["keyword"] == "iPhone 13" and q1["price_from"] is None and q1["price_to"] == 100
    assert q1["category_path"] == "kaufen-und-verkaufen/marktplatz/handys-telefone-2691"
    assert q2["keyword"] == "Handy" and q2["price_to"] is None and q2["category_path"] is None
    assert q2["category_hint"] == "Handys"


@pytest.mark.parametrize("unusual,categories", [(False, False), (True, False), (False, True), (True, True)])
async def test_toggles_change_the_plan_prompt(claude, unusual, categories):
    plan, model = await ps.make_plan("Geschirrspüler bis 300 €", ps.make_settings("thorough", unusual, categories))
    prompt = claude.calls[0]["prompt"]
    assert claude.purposes == ["search"] and model == "fake-sonnet"
    assert "Höchstzahl an Suchen: 6" in prompt and "Geschirrspüler bis 300 €" in prompt
    assert ("ungewöhnliche Bezeichnungen" in prompt) is unusual and ("Schreibfehler" in prompt) is unusual
    assert ("andere Kategorien" in prompt) is categories and ("Konvolut" in prompt) is categories
    assert [q["keyword"] for q in plan["queries"]] == ["Geschirrspüler", "Spülmaschine", "Geschirspüler"]


# ------------------------------------------------------------------ searching
def test_candidate_filter():
    plan = {"max_price": 300, "exclude": ["defekt", "nur Versand"]}
    assert ps.drop_reason(_summary("1", "Suche Geschirrspüler", 1), plan) == "wanted"
    assert ps.drop_reason(_summary("2", "Tausche Spüler", 100), plan) == "wanted"
    assert ps.drop_reason(_summary("3", "Miele G7000", 900), plan) == "over_budget"
    assert ps.drop_reason(_summary("4", "Miele G7000", 440), plan) is None           # within 1.5 × budget
    assert ps.drop_reason(_summary("5", "AEG Spülmaschine defekt", 40), plan) == "excluded"
    assert ps.drop_reason(_summary("6", "Bosch leise", None), plan) is None
    assert ps.drop_reason(_summary("7", "Bosch", 9999), {"max_price": None}) is None


async def test_queries_run_one_after_another_and_deduplicate(wh):
    plan = ps.normalize_plan(PLAN, 3)
    progress = []
    found = await ps.run_queries(plan, ps.make_settings(), on_progress=lambda d, n: progress.append((d, n)))
    assert wh["keywords"] == ["Geschirrspüler", "Spülmaschine", "Geschirspüler"] and wh["max_inflight"] == 1
    assert found["requests"] == 3 and progress == [(1, 3), (2, 3), (3, 3)] and found["warning"] is None
    ids = [a.ad_id for pool in found["pools"] for a in pool]
    assert len(ids) == len(set(ids))                                   # 2000000002 only once
    assert "2000000002" in [a.ad_id for a in found["pools"][0]] and "2000000002" not in [a.ad_id for a in found["pools"][1]]
    assert "2000000003" not in ids and "2000000004" not in ids          # wanted ad, far above budget
    assert "2000000005" not in ids                                      # "defekt" in the title
    assert 900 in found["prices"][0]                                    # still a comparable for the price position
    assert found["per_query"][0]["found"] == 10 and found["per_query"][1]["kept"] == 1


def test_pick_candidates_round_robin_and_cap():
    pools = [[_summary(f"a{i}", "A", 1) for i in range(5)], [_summary("b0", "B", 1)], [_summary(f"c{i}", "C", 1) for i in range(3)]]
    picked = [a.ad_id for _, a in ps.pick_candidates(pools, cap=6)]
    assert picked == ["a0", "b0", "c0", "a1", "c1", "a2"]
    assert len(ps.pick_candidates(pools)) == 9


async def test_block_stops_gracefully(wh):
    wh["status"]["Spülmaschine"] = 429
    found = await ps.run_queries(ps.normalize_plan(PLAN, 3), ps.make_settings())
    assert wh["keywords"] == ["Geschirrspüler", "Spülmaschine"]          # third query not sent
    assert found["per_query"][2]["skipped"] and found["per_query"][1]["error"]
    assert "stopped after 1 searches" in found["warning"] and found["pools"][0]


async def test_hourly_budget_limits_the_queries(wh):
    public_mod.get_public().limiter = RateLimiter(0, per_hour=2)
    found = await ps.run_queries(ps.normalize_plan(PLAN, 3), ps.make_settings())
    assert len(wh["keywords"]) == 2 and found["per_query"][2]["skipped"]
    assert "only 2 of 3 searches" in found["warning"]


# ------------------------------------------------------------------ ranking
async def test_ranking_merges_price_position_and_scam_flags(wh, claude):
    plan = ps.normalize_plan(PLAN, 3)
    found = await ps.run_queries(plan, ps.make_settings())
    cards = [ps.card(qi, ad, plan, found["prices"][qi]) for qi, ad in ps.pick_candidates(found["pools"])]
    claude.rank = {"results": [{"nr": 99, "fit": 100, "reason": "unknown nr", "red_flags": []},
                               {"nr": 2, "fit": 60, "reason": "ok", "red_flags": ["Händler"]},
                               {"nr": 1, "fit": 140, "reason": "top", "red_flags": []},
                               {"nr": 1, "fit": 10, "reason": "dup", "red_flags": []}], "note": "n"}
    results, note, _ = await ps.rank("Geschirrspüler", plan, cards)
    assert [r["reason"] for r in results] == ["top", "ok"] and results[0]["fit"] == 100 and note == "n"
    first = results[0]
    assert first["ad_id"] == cards[0]["ad_id"] and first["position"]["basis"] == "comparables"
    assert first["position"]["level"] in {"bargain", "cheap", "fair", "expensive", "very_expensive"}
    assert first["comparables"] >= 5 and first["query"] == "Geschirrspüler"
    scam_card = next(c for c in cards if c["ad_id"] == "2000000006")
    assert any(f["code"] == "money_transfer" for f in scam_card["scam_flags"])
    prompt = claude.calls[-1]["prompt"]
    assert "Western Union" in prompt or "Auffälligkeiten" in prompt
    assert f"[{len(cards)}]" in prompt and "Budget: bis 300 €" in prompt and "Ausschlüsse: defekt; nur Versand" in prompt


# ------------------------------------------------------------------ background task + storage
async def _finish():
    for _ in range(200):
        if not ps._tasks:
            return
        await asyncio.gather(*list(ps._tasks))


async def test_background_search_is_stored_and_refuses_a_second(wh, claude):
    claude.delay = 0.05
    sid = ps.start("Leiser Geschirrspüler, 60 cm, bis 300 €", ps.make_settings("normal", True, False))
    assert ps.running().id == sid
    with pytest.raises(ps.Busy):
        ps.start("noch was", ps.make_settings())
    await _finish()
    row = ps.get(sid)
    assert row.status == "done" and row.requests == 3 and row.finished_at and row.started_at
    assert row.settings == {"depth": "normal", "unusual": True, "categories": False, "sort": "relevance"}
    assert row.plan["summary"].startswith("Leiser") and len(row.queries) == 3
    assert [r["fit"] for r in row.results] == [92, 70] and row.note == "Gute Auswahl unter 300 €."
    assert row.progress == {"done": 3, "total": 3} and row.candidates >= 5
    assert len(claude.calls) == 2 and claude.purposes == ["search", "search"]
    assert ps.running() is None and [r.id for r in ps.recent()] == [sid]

    ps.rerun(sid)                                           # same plan, only the ranking asks Claude
    await _finish()
    assert len(claude.calls) == 3 and claude.calls[-1]["schema"] is SEARCH_RANK_SCHEMA
    assert ps.get(sid).status == "done" and len(wh["keywords"]) == 6


async def test_rank_failure_falls_back_to_cheapest(wh, claude):
    claude.fail_rank = True
    sid = ps.start("Geschirrspüler", ps.make_settings())
    await _finish()
    row = ps.get(sid)
    assert row.status == "done" and row.results and all(r["fit"] is None for r in row.results)
    assert "overloaded" in row.warning
    pos = [r["position"].get("pos") for r in row.results if r["position"].get("pos") is not None]
    assert pos == sorted(pos)


async def test_plan_failure_and_restart_recovery(wh, monkeypatch):
    class Broken:
        async def complete(self, **kw):
            raise LLMError("no token")
    monkeypatch.setattr(ps, "get_backend", lambda purpose=None: Broken())
    sid = ps.start("x", ps.make_settings())
    await _finish()
    assert ps.get(sid).status == "error" and "no token" in ps.get(sid).error and wh["keywords"] == []
    with dbmod.session() as db:
        db.add(dbmod.ProductSearch(description="y", status="searching"))
        db.commit()
    ps.recover()
    with dbmod.session() as db:
        assert all(r.status in ("error", "done") for r in db.exec(select(dbmod.ProductSearch)))


# ------------------------------------------------------------------ pages
@pytest.fixture
def app_client(wh, claude):
    from app.main import app

    with TestClient(app) as c:
        c.fake = claude
        yield c


def test_page_renamed_and_search_flow(app_client):
    page = app_client.get("/deals").text
    assert '<a href="/deals">Product search</a>' in page and "<h3>Product search</h3>" in page
    assert "Price check" not in page
    assert "What are you looking for?" in page and "Extended: unusual names" in page and "Extended: other categories" in page
    assert "Normal (≈3 searches)" in page and "Thorough (≈6)" in page and "Deep (≈12)" in page
    assert "Claude is called 2 times" in page and "Check a single ad&#39;s price" in page
    assert app_client.get("/search", follow_redirects=False).headers["location"] == "/deals"

    r = app_client.post("/deals/search", data={"description": "Leiser Geschirrspüler bis 300 €", "depth": "deep",
                                               "unusual": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "/deals?search=" in r.headers["location"]
    sid = int(r.headers["location"].split("search=")[1].split("#")[0])
    frag = ""
    for _ in range(100):
        frag = app_client.get(f"/deals/search/{sid}").text
        if 'hx-trigger="every 2s"' not in frag:
            break
        time.sleep(0.05)
    assert "Fit 92/100" in frag and "Leise, 60 cm, gut im Budget." in frag and "Open in viewer" in frag
    assert 'data-ad="2000000001"' in frag and "/deals/ad/2000000001?q=Geschirrsp%C3%BCler" in frag
    assert "Check price closely" in frag and "deal-needle" in frag and "Generated searches (3)" in frag
    assert "3 willhaben searches" in frag and "Zustand unklar" in frag
    assert ps.get(sid).settings["depth"] == "deep"
    assert "Höchstzahl an Suchen: 12" in app_client.fake.calls[0]["prompt"]

    full = app_client.get(f"/deals?search={sid}").text
    assert "Recent searches" in full and f"/deals/search/{sid}/rerun" in full

    empty = app_client.post("/deals/search", data={"description": "  "}, follow_redirects=False)
    assert empty.status_code == 303 and "msg=" in empty.headers["location"]


def test_page_german(app_client):
    dbmod.set_setting("ui_lang", "de")
    page = app_client.get("/deals").text
    assert "Produktsuche" in page and "Preischeck</a>" not in page and "Was suchst du?" in page
    assert "Erweitert: ungewöhnliche Bezeichnungen" in page and "Erweitert: andere Kategorien" in page
    assert "Normal (≈3 Suchen)" in page and "Gründlich (≈6)" in page and "Tief (≈12)" in page


# ------------------------------------------------------------------ purpose + usage
def test_search_purpose_has_own_model_and_usage(monkeypatch):
    assert llm_base.PURPOSES["search"][0] == "llm_model_search" and llm_base.PURPOSES["search"][1] == "Product search"
    dbmod.set_setting("llm_model_search", "claude-haiku-5-5")
    assert llm_base.model_for("search") == "claude-haiku-5-5"

    from app.llm.claude_cli import ClaudeCLI
    asyncio.run(ClaudeCLI(cmd=FAKE_CLI, model="claude-haiku-5-5", purpose="search")
                .complete(system="s", prompt="p", schema={"type": "object"}))
    with dbmod.session() as db:
        rows = list(db.exec(select(dbmod.LLMCall)))
    assert [r.purpose for r in rows] == ["search"]

    from app.main import app
    with TestClient(app) as c:
        page = c.get("/settings").text
    assert "Product search" in page


def test_module_is_checked_for_anonymity():
    from tests.test_anonymous_public import ANONYMOUS_MODULES
    assert "services/product_search.py" in ANONYMOUS_MODULES
