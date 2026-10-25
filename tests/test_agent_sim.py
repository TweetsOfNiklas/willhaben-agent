"""The search agent simulator: replaying an agent's hits with edited settings and judging test ads Claude invents –
dry (nothing written to the hits, nothing pushed), kept as runs, rated, and a better brief proposed."""
import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import EventLog, Hit, Purchase, SearchAgent, session
from app.llm.base import LLMResult
from app.services import agent_sim, screening


class Claude:
    """Answers by the schema asked for: test ads, a brief, or the screening's verdicts (`verdict(title)`)."""
    model = "fake"

    def __init__(self, verdict=None, ads=None):
        self.calls = []
        self.verdict = verdict or (lambda title: {"relevant": True, "deal": "good", "issues": [], "reason": "Passt."})
        self.ads = ads or []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt})
        props = schema.get("properties", {})
        if "ads" in props:
            return LLMResult(data={"ads": self.ads}, model=self.model)
        if "brief" in props:
            return LLMResult(data={"brief": "Echo Show 8, ab Generation 2, nicht defekt, unter 50 €.",
                                   "summary": "Generation und Defekte ergänzt."}, model=self.model)
        results = []
        for nr, title in re.findall(r"^\[(\d+)\] (.+?) · Preis", prompt, re.M):
            v = dict(self.verdict(title))
            if "geizhals_ok" in str(schema):
                v.update({"geizhals_ok": True, "geizhals_pick": 0, "product": "", "queries": []})
            results.append({"nr": int(nr), **v})
        return LLMResult(data={"results": results}, model=self.model)


@pytest.fixture
def agent():
    with session() as db:
        a = SearchAgent(name="Echo Show", source_url="https://www.willhaben.at/iad/x?keyword=echo+show+8", webapi_url="x",
                        screen=True, screen_brief="Echo Show 8", screen_min="match")
        db.add(a)
        db.commit()
        db.refresh(a)
        return a


def _hit(agent, n, heading, state, price=40.0, **kw):
    with session() as db:
        h = Hit(agent_id=agent.id, ad_id=str(n), heading=heading, url=f"https://x/{n}", price=price, state=state,
                screen_relevant=state != "filtered", screen_deal="good" if state != "filtered" else "unknown", **kw)
        db.add(h)
        db.commit()
        db.refresh(h)
        return h


async def test_replay_judges_again_without_touching_the_hits(agent, monkeypatch):
    """A stricter brief holds back a hit that was pushed; the hits keep their verdicts; the pushed one is known."""
    pushed = _hit(agent, 1, "Echo Show 8 Gen 1", "new")
    _hit(agent, 2, "Echo Show 8 Gen 2", "seen")
    held = _hit(agent, 3, "Echo Show 5", "filtered")
    with session() as db:
        db.add(EventLog(kind="hit", message="x", ad_id="1"))
        db.commit()
    fake = Claude(lambda title: {"relevant": "Gen 2" in title, "deal": "good",
                                 "issues": [] if "Gen 2" in title else ["wrong_variant"], "reason": "geprüft"})
    monkeypatch.setattr(screening, "get_backend", lambda *a, **k: fake)
    settings = {**agent_sim.settings_of(agent), "screen_brief": "Nur Echo Show 8 Gen 2"}
    out = await agent_sim._replay(agent.id, settings, 30, "all")
    by = {r["ad_id"]: r for r in out}
    assert by["1"]["real"]["pushed"] and by["1"]["real"]["push"] and not by["1"]["sim"]["push"] and by["1"]["changed"]
    assert by["2"]["sim"]["push"] and not by["2"]["changed"]
    assert not by["3"]["real"]["push"] and not by["3"]["sim"]["push"]
    assert "Nur Echo Show 8 Gen 2" in fake.calls[0]["prompt"]                   # the edited brief, not the agent's
    with session() as db:
        assert db.get(Hit, pushed.id).screen_relevant is True and db.get(Hit, held.id).state == "filtered"
    s = agent_sim._replay_summary(out)
    assert (s["push"], s["held"], s["now_held"], s["now_push"]) == (1, 2, 1, 0)
    only = await agent_sim._replay(agent.id, settings, 30, "pushed")
    assert [r["ad_id"] for r in only] == ["1"]


async def test_replay_rules_without_claude(agent):
    """Without the screening only the rules judge: a Geizhals rule holds back what is not cheap enough."""
    _hit(agent, 1, "Echo Show 8", "new", price=60.0, geizhals={"name": "Echo Show 8", "price_from": 70.0})
    _hit(agent, 2, "Echo Show 8", "new", price=20.0, geizhals={"name": "Echo Show 8", "price_from": 70.0})
    settings = {**agent_sim.settings_of(agent), "screen": False, "geizhals_pct": 50}
    out = {r["ad_id"]: r for r in await agent_sim._replay(agent.id, settings, 30, "all")}
    assert not out["1"]["sim"]["push"] and out["1"]["sim"]["reasons"] and out["2"]["sim"]["push"]


async def test_replay_says_which_hits_auto_buy_would_buy(agent):
    """With auto-buy in the simulated settings each hit that would be a hit says whether it would be bought (the
    estimated total incl. shipping and buyer protection within the maximum) or why not; nothing is bought."""
    _hit(agent, 1, "Echo Show 8", "new", price=30.0, shipping_price=3.49)
    _hit(agent, 2, "Echo Show 8", "new", price=80.0, shipping_price=3.49)
    settings = {**agent_sim.settings_of(agent), "screen": False, "auto_buy": True, "auto_buy_max": 50.0}
    out = {r["ad_id"]: r for r in await agent_sim._replay(agent.id, settings, 30, "all")}
    assert out["1"]["sim"]["buy"] and 30 < out["1"]["sim"]["buy_total"] <= 50
    assert not out["2"]["sim"]["buy"] and "50" in out["2"]["sim"]["buy_why"]
    assert agent_sim._replay_summary(list(out.values()))["buy"] == 1
    with session() as db:
        assert not db.exec(select(Purchase)).all()                              # simulated: nothing bought


async def test_invented_ads_are_judged_and_compared(agent, monkeypatch):
    """Claude's test ads with their expected verdict; the screening's verdict next to it; a mismatch is marked."""
    ads = [{"kind": "bargain", "title": "Echo Show 8 wie neu", "price": 25, "description": "Top", "condition": "wie neu",
            "location": "Wien", "private": True, "shipping": True, "expected_relevant": True, "expected_deal": "great",
            "expected_issues": [], "why": "Günstig"},
           {"kind": "accessory_only", "title": "Ständer für Echo Show 8", "price": 15, "description": "Nur Ständer",
            "condition": "neu", "location": "Graz", "private": True, "shipping": True, "expected_relevant": False,
            "expected_deal": "unknown", "expected_issues": ["accessory_only"], "why": "Zubehör"}]
    fake = Claude(lambda title: {"relevant": True, "deal": "great", "issues": [], "reason": "Passt"}, ads=ads)
    monkeypatch.setattr(agent_sim, "get_backend", lambda *a, **k: fake)
    monkeypatch.setattr(screening, "get_backend", lambda *a, **k: fake)
    out = await agent_sim._tests(agent.id, agent_sim.settings_of(agent), 2)
    assert [r["ok"] for r in out] == [True, False]                              # the stand counted as the item: wrong
    assert out[1]["expected"]["push"] is False and out[1]["actual"]["push"] is True
    assert "Echo Show 8" in fake.calls[0]["prompt"] and "echo show 8" in fake.calls[0]["prompt"]   # brief, search terms
    with session() as db:
        assert not db.exec(select(Hit)).all()                                   # invented ads are never stored


def test_the_page_runs_rates_proposes_and_applies(agent, monkeypatch):
    """Start a replay from the page, wait for it, rate a row, let Claude propose a brief and take it, take the
    settings over."""
    from app.main import app
    _hit(agent, 1, "Echo Show 8 Gen 1", "new")
    fake = Claude()
    monkeypatch.setattr(screening, "get_backend", lambda *a, **k: fake)
    monkeypatch.setattr(agent_sim, "get_backend", lambda *a, **k: fake)
    with TestClient(app) as c:
        page = c.get(f"/agent-sim?agent={agent.id}").text
        assert "data-sim-settings" in page and "Echo Show 8" in page and "echo show 8" in page
        form = {"agent_id": agent.id, "kind": "replay", "screen": "on", "screen_brief": "Echo Show 8, nur Gen 2",
                "screen_min": "good", "screen_hide": ["wrong_product"], "gh_rule": "", "max_km": "40", "n": "30",
                "only": "all"}
        r = c.post("/agent-sim/run", data=form, follow_redirects=False)
        sim_id = int(r.headers["location"].split("id=")[1])
        for _ in range(50):
            if agent_sim.get(sim_id).status != "running":
                break
            time.sleep(0.05)
        sim = agent_sim.get(sim_id)
        assert sim.status == "done" and sim.results and sim.settings["screen_min"] == "good"
        page = c.get(f"/agent-sim?id={sim_id}").text
        assert 'data-sim-run="done"' in page and "data-apply" in page and "data-propose" in page
        c.post(f"/agent-sim/{sim_id}/rate/0", data={"up": "0", "note": "Gen 1 will ich nicht"})
        assert agent_sim.get(sim_id).ratings["0"] == {"up": False, "note": "Gen 1 will ich nicht"}
        c.post(f"/agent-sim/{sim_id}/propose")
        assert "Gen 1 will ich nicht" in fake.calls[-1]["prompt"]
        assert "data-diff" in c.get(f"/agent-sim?id={sim_id}").text
        c.post(f"/agent-sim/{sim_id}/proposal/apply")
        c.post(f"/agent-sim/{sim_id}/apply")
        assert 'data-run-link' in c.get("/agent-sim").text
    with session() as db:
        a = db.get(SearchAgent, agent.id)
    assert a.screen_min == "good" and a.max_km == 40 and a.screen_hide == "wrong_product"
    assert a.screen_brief == "Echo Show 8, nur Gen 2"                         # the settings (applied last) win
    assert agent_sim.get(sim_id).proposal["status"] == "applied"


def test_test_ads_need_the_screening(agent):
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/agent-sim/run", data={"agent_id": agent.id, "kind": "tests"}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and not agent_sim.runs()
