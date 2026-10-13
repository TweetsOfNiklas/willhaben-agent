"""A search agent's Geizhals rule: push only ads at least X % below the lowest new price on Geizhals."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.db import Hit, SearchAgent, session
from app.services import deals, search_agents
from app.services.search_agents import create_agent, geizhals_below_pct, geizhals_passes, run_agent
from tests.fake_platform import FakePlatform, listing

GH = {"name": "Apple iPhone 13 128GB", "url": "https://geizhals.at/apple-iphone-13-a2610000.html", "price_from": 500.0}


@pytest.fixture
def fake():
    p = FakePlatform()
    platforms.set_platform("fake", p)
    yield p
    platforms.reset()


@pytest.fixture
def world(monkeypatch):
    """Geizhals knows iPhones only; every lookup and every notification is recorded."""
    lookups, sent = [], []

    async def match(query):
        lookups.append(query)
        return dict(GH) if query and "iphone" in query.lower() else None

    async def notify(kind, title, text="", **kw):
        sent.append({"kind": kind, "ad": (kw.get("data") or {}).get("ad_id"), "push": kw.get("push", True), "text": text,
                     "data": kw.get("data") or {}})

    monkeypatch.setattr(deals, "geizhals_match", match)
    monkeypatch.setattr(search_agents, "notify", notify)
    return lookups, sent


def _hits(agent_id):
    with session() as db:
        return {h.ad_id: h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}


def _set(agent_id, **fields):
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        for k, v in fields.items():
            setattr(a, k, v)
        db.add(a)
        db.commit()


def test_the_rule():
    a = SearchAgent(name="x", source_url="x", webapi_url="x", geizhals_pct=30)
    h = Hit(agent_id=1, ad_id="1", heading="iPhone", url="x", price=350.0, geizhals=dict(GH))
    assert geizhals_passes(a, h) and geizhals_below_pct(h) == 30
    h.price = 351.0
    assert not geizhals_passes(a, h)
    for gh, price in ((None, 351.0), ({}, 351.0), (dict(GH), None)):      # nothing to compare: it passes
        assert geizhals_passes(a, Hit(agent_id=1, ad_id="1", heading="x", url="x", price=price, geizhals=gh))
    a.geizhals_pct = None
    assert geizhals_passes(a, h)
    assert geizhals_below_pct(Hit(agent_id=1, ad_id="1", heading="x", url="x", price=600.0, geizhals=dict(GH))) == -20


async def test_dear_hits_are_filtered_out_until_they_get_cheap(fake, world):
    lookups, sent = world
    a = create_agent("https://fake.example/s/iphone")
    fake.pages = [[]]
    await run_agent(a.id)                                                     # first check: nothing there yet
    _set(a.id, geizhals_pct=30)
    fake.pages = [[listing("1", "iPhone 13 128GB", 340.0), listing("2", "iPhone 13 128GB Akku neu", 400.0),
                   listing("3", "Handyhülle", 10.0)]]
    await run_agent(a.id)
    hits = _hits(a.id)
    assert {k: h.state for k, h in hits.items()} == {"1": "new", "2": "filtered", "3": "new"}
    assert hits["1"].geizhals == GH and hits["3"].geizhals == {}               # Geizhals does not know the case
    assert {m["ad"]: m["push"] for m in sent if m["kind"] == "hit"} == {"1": True, "2": False, "3": True}
    first = next(m for m in sent if m["ad"] == "1")
    assert "Geizhals: new from 500 €, 32 % less" in first["text"] and first["data"]["geizhals_below_pct"] == 32
    asked = len(lookups)

    sent.clear()
    fake.pages = [[listing("1", "iPhone 13 128GB", 340.0), listing("2", "iPhone 13 128GB Akku neu", 345.0),
                   listing("3", "Handyhülle", 10.0)]]
    await run_agent(a.id)                                                     # 400 -> 345: 31 % below now
    assert _hits(a.id)["2"].state == "new" and len(lookups) == asked          # no second lookup
    assert [(m["kind"], m["ad"], m["push"]) for m in sent] == [("price_drop", "2", True)]


async def test_held_back_hits_cost_no_claude_call(fake, world, monkeypatch):
    screened = []

    async def screen(agent_id, hits):
        screened.extend(h.ad_id for h in hits)

    monkeypatch.setattr(search_agents.screening, "screen", screen)
    a = create_agent("https://fake.example/s/iphone")
    fake.pages = [[]]
    await run_agent(a.id)
    _set(a.id, geizhals_pct=30, screen=True)
    fake.pages = [[listing("1", "iPhone 13 128GB", 340.0), listing("2", "iPhone 13 128GB", 450.0)]]
    await run_agent(a.id)
    assert screened == ["1"]


def test_settings_drawer_and_pages(fake):
    from app.main import app

    a = create_agent("https://fake.example/s/iphone")
    with TestClient(app) as c:
        assert 'name="gh_on"' in c.get("/agents").text
        form = {"name": a.name, "interval_min": "10"}
        c.post(f"/agents/{a.id}/edit", data={**form, "gh_on": "on", "gh_pct": "25"})
        with session() as db:
            assert db.get(SearchAgent, a.id).geizhals_pct == 25
        assert "25 % below Geizhals" in c.get("/agents").text
        c.post(f"/agents/{a.id}/edit", data={**form, "gh_on": "on", "gh_pct": "250"})
        with session() as db:
            assert db.get(SearchAgent, a.id).geizhals_pct == 95
        c.post(f"/agents/{a.id}/edit", data={**form, "gh_pct": "25"})                 # switch off
        with session() as db:
            assert db.get(SearchAgent, a.id).geizhals_pct is None
            db.add(Hit(agent_id=a.id, platform="fake", ad_id="9", heading="iPhone 13", url="https://fake.example/ad/9",
                       price=400.0, geizhals=dict(GH), state="filtered"))
            db.commit()
        page = c.get("/hits?state=filtered").text
        assert "Geizhals new from" in page and "20 % less" in page and GH["url"] in page
