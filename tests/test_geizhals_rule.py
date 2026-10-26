"""A search agent's Geizhals rule: push only ads at least X % below the lowest new price on Geizhals."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.db import Hit, SearchAgent, session
from app.services import deals, geizhals, screening, search_agents
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

    async def match(query, **kw):
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


async def test_the_screening_gets_the_new_price_without_a_rule(fake, world, monkeypatch):
    """Screening alone (no Geizhals rule) looks the hits up too: Claude rates against the new price."""
    screened = []

    async def screen(agent_id, hits):
        screened.extend((h.ad_id, (h.geizhals or {}).get("price_from")) for h in hits)

    monkeypatch.setattr(search_agents.screening, "screen", screen)
    a = create_agent("https://fake.example/s/iphone")
    fake.pages = [[]]
    await run_agent(a.id)
    _set(a.id, screen=True)
    fake.pages = [[listing("1", "iPhone 13 128GB", 340.0)]]
    await run_agent(a.id)
    assert screened == [("1", 500.0)]


def test_exactly_x_percent_below_passes():
    """Exactly X % below keeps the rule (700 € · 0.7 is 489.99999… in floats); the % shown never rounds up to X."""
    a = SearchAgent(name="x", source_url="x", webapi_url="x")
    for new, price, pct in ((700.0, 490.0, 30), (500.0, 465.0, 7), (300.0, 207.0, 31), (699.9, 489.93, 30)):
        a.geizhals_pct = pct
        h = Hit(agent_id=1, ad_id="1", heading="iPhone", url="x", price=price, geizhals={**GH, "price_from": new})
        assert geizhals_passes(a, h) and geizhals_below_pct(h) == pct
        h.price = round(price + 0.01, 2)                                      # a cent dearer: held back
        assert not geizhals_passes(a, h) and geizhals_below_pct(h) == pct - 1
    a.geizhals_pct = 30
    held = Hit(agent_id=1, ad_id="1", heading="iPhone", url="x", price=352.0, geizhals=dict(GH))   # 29.6 % below
    assert not geizhals_passes(a, held) and geizhals_below_pct(held) == 29
    for price, pct in ((503.0, -1), (500.01, -1), (500.0, 0), (600.0, -20), (601.0, -21)):   # dearer: never "0 % less"
        h = Hit(agent_id=1, ad_id="1", heading="x", url="x", price=price, geizhals=dict(GH))
        assert geizhals_below_pct(h) == pct


async def test_a_held_back_hit_returns_only_screened(fake, world, monkeypatch):
    """Screening on, price drops not reported: a hit the rule held back was never screened, so a drop under the line
    leaves it filtered (no way into New past Claude); reported drops are screened first and then it returns."""
    screened = []

    async def screen(agent_id, hits):
        screened.extend(h.ad_id for h in hits)

    monkeypatch.setattr(search_agents.screening, "screen", screen)
    a = create_agent("https://fake.example/s/iphone")
    fake.pages = [[]]
    await run_agent(a.id)
    _set(a.id, geizhals_pct=30, screen=True, notify_price_drops=False)
    fake.pages = [[listing("1", "iPhone 13 128GB defekt", 400.0)]]
    await run_agent(a.id)
    assert _hits(a.id)["1"].state == "filtered" and screened == []
    fake.pages = [[listing("1", "iPhone 13 128GB defekt", 300.0), listing("2", "Handyhülle", 10.0)]]
    await run_agent(a.id)                                                     # under the line, but not screened
    hits = _hits(a.id)
    assert hits["1"].state == "filtered" and hits["2"].state == "new" and screened == ["2"]
    _set(a.id, notify_price_drops=True)
    fake.pages = [[listing("1", "iPhone 13 128GB defekt", 290.0), listing("2", "Handyhülle", 10.0)]]
    await run_agent(a.id)
    assert _hits(a.id)["1"].state == "new" and screened == ["2", "1"]



async def test_a_held_back_hit_claude_was_not_sent_stays_filtered(fake, world, monkeypatch):
    """Claude sees MAX_PER_RUN hits per check, held-back hits first: one that did not fit stays filtered and gets no
    push, though its drop is under the line now (it has no verdict, and Claude was not asked); a new hit that did not
    fit stays pending (the next check judges it) instead of being pushed unjudged."""
    lookups, sent = world
    screened = []

    async def screen(agent_id, hits):
        screened.extend(h.ad_id for h in hits[:screening.MAX_PER_CALL])
        return len(hits)

    monkeypatch.setattr(search_agents.screening, "screen", screen)
    monkeypatch.setattr(search_agents.screening, "MAX_PER_CALL", 1)
    monkeypatch.setattr(search_agents.screening, "MAX_PER_RUN", 1)
    a = create_agent("https://fake.example/s/iphone")
    fake.pages = [[]]
    await run_agent(a.id)
    _set(a.id, geizhals_pct=30, screen=True)
    fake.pages = [[listing("1", "iPhone 13 128GB defekt", 400.0), listing("2", "iPhone 13 128GB", 420.0)]]
    await run_agent(a.id)
    assert {k: h.state for k, h in _hits(a.id).items()} == {"1": "filtered", "2": "filtered"} and screened == []
    sent.clear()
    fake.pages = [[listing("1", "iPhone 13 128GB defekt", 300.0), listing("2", "iPhone 13 128GB", 310.0),
                   listing("3", "Handyhülle", 10.0)]]
    await run_agent(a.id)                                                     # both under the line now
    assert screened == ["1"]                                                  # before the new hit
    assert {k: h.state for k, h in _hits(a.id).items()} == {"1": "new", "2": "filtered", "3": "pending"}
    assert {(m["kind"], m["ad"]): m["push"] for m in sent} == {("price_drop", "1"): True, ("price_drop", "2"): False,
                                                               ("hit", "3"): False}

async def test_a_failed_lookup_is_asked_again(fake, monkeypatch):
    """Geizhals blocked or down: the hit passes as usual and is looked up again later, not kept as "no product"."""
    sent, asked = [], []

    async def notify(kind, title, text="", **kw):
        sent.append((kind, (kw.get("data") or {}).get("ad_id"), kw.get("push", True)))

    async def down(query, **kw):
        asked.append(query)
        raise geizhals.GeizhalsError("HTTP 429")

    async def up(query, **kw):
        asked.append(query)
        return {"query": query, "verified": True, "candidates": [], "match": dict(GH)}

    monkeypatch.setattr(search_agents, "notify", notify)
    monkeypatch.setattr(deals.geizhals, "lookup", down)
    deals.clear_cache()
    try:
        a = create_agent("https://fake.example/s/iphone")
        fake.pages = [[]]
        await run_agent(a.id)
        _set(a.id, geizhals_pct=30)
        fake.pages = [[listing("1", "iPhone 13 128GB", 480.0)]]
        await run_agent(a.id)
        hit = _hits(a.id)["1"]
        assert hit.state == "new" and hit.geizhals is None and sent == [("hit", "1", True)] and len(asked) == 1
        deals.clear_cache()                                                   # 15 min later Geizhals answers again
        monkeypatch.setattr(deals.geizhals, "lookup", up)
        sent.clear()
        fake.pages = [[listing("1", "iPhone 13 128GB", 470.0)]]
        await run_agent(a.id)                                                 # 470 € is far above 350 €
        assert _hits(a.id)["1"].geizhals == GH and len(asked) == 2
        assert sent == [("price_drop", "1", False)]
    finally:
        deals.clear_cache()



async def test_a_lookup_that_fails_while_another_finds_it(fake, monkeypatch):
    """Two agents ask for one product at once: this lookup fails (429), the other one finds it. The failed one leaves
    its hit not asked (a later run takes the cached product), not "no product" for good."""
    other_found = asyncio.Event()
    calls = []

    async def lookup(query, **kw):
        calls.append(query)
        if len(calls) == 1:
            await other_found.wait()
            raise geizhals.GeizhalsError("HTTP 429")
        other_found.set()
        return {"query": query, "verified": True, "candidates": [], "match": dict(GH)}

    monkeypatch.setattr(deals.geizhals, "lookup", lookup)
    deals.clear_cache()
    try:
        a = create_agent("https://fake.example/s/iphone")
        with session() as db:
            pair = [Hit(agent_id=a.id, platform="fake", ad_id=i, heading="iPhone 13 128GB",
                        url=f"https://fake.example/ad/{i}", price=400.0) for i in ("1", "2")]
            db.add_all(pair)
            db.commit()
            for h in pair:
                db.refresh(h)
        await asyncio.gather(search_agents._geizhals_prices([pair[0]]), search_agents._geizhals_prices([pair[1]]))
        hits = _hits(a.id)
        assert len(calls) == 2 and hits["1"].geizhals is None and hits["2"].geizhals == GH
        await search_agents._geizhals_prices([hits["1"]])                     # the next run: from the cache
        assert _hits(a.id)["1"].geizhals == GH and len(calls) == 2
    finally:
        deals.clear_cache()

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
            db.add(Hit(agent_id=a.id, platform="fake", ad_id="10", heading="iPhone 13 defekt", price=100.0,
                       url="https://fake.example/ad/10", geizhals=dict(GH), state="filtered", item_condition="defect",
                       item_condition_label="Defekt"))
            db.commit()
        page = c.get("/hits?state=filtered").text
        assert 'data-rating="geizhals"' in page and GH["url"] in page              # each rating names its source
        assert 'data-rating="geizhals">' in page              # 80 % of new: the user's used zones
        # next to a zone badge the share it comes from (a floored "20 % less" next to it read as 80 % or 81 %)
        assert "80\xa0% of €\xa0500 new" in page and "20 % less" not in page
        assert 'By the Geizhals new price alone; the price check of the ad also weighs comparable ads."' in page
        assert "new from €\xa0500 · 80\xa0% less" in page              # no badge for a defective item: the saving
        assert '>Defekt</span>' in page and "bg-bad-soft text-error" in page     # its condition as a pill


def test_the_price_zone_rule():
    """A price zone instead of a %: the zones of Settings → Prices (used 35/55/75/90, new 70/85/100/115 % of new)."""
    a = SearchAgent(name="x", source_url="x", webapi_url="x", geizhals_level="cheap")
    hit = lambda price, cond=None: Hit(agent_id=1, ad_id="1", heading="iPhone", url="x", price=price,  # noqa: E731
                                       geizhals=dict(GH), item_condition=cond)
    assert search_agents.geizhals_rule(a)
    assert geizhals_passes(a, hit(150.0)) and geizhals_passes(a, hit(274.99))       # bargain, cheap: used below 55 %
    assert not geizhals_passes(a, hit(275.0)) and not geizhals_passes(a, hit(400.0))  # fair, expensive
    assert geizhals_passes(a, hit(400.0, "new")) and not geizhals_passes(a, hit(425.0, "new"))   # new: below 85 %
    assert geizhals_passes(a, hit(450.0, "defect"))                                  # nothing to compare
    assert geizhals_passes(a, Hit(agent_id=1, ad_id="1", heading="x", url="x", price=450.0, geizhals={}))
    a.geizhals_level = "bargain"
    assert geizhals_passes(a, hit(174.99)) and not geizhals_passes(a, hit(175.0))
    a.geizhals_level = "fair"
    assert geizhals_passes(a, hit(374.99)) and not geizhals_passes(a, hit(375.0))


def test_the_drawer_saves_a_price_zone(fake):
    from app.main import app

    a = create_agent("https://fake.example/s/iphone")
    with TestClient(app) as c:
        form = {"name": a.name, "interval_min": "10", "gh_pct": "25"}
        page = c.get("/agents").text
        assert 'name="gh_level"' in page and "Bargain (used below 35 %, new below 70 % of the new price)" in page
        c.post(f"/agents/{a.id}/edit", data={**form, "gh_on": "on", "gh_level": "bargain"})
        with session() as db:
            row = db.get(SearchAgent, a.id)
            assert (row.geizhals_level, row.geizhals_pct) == ("bargain", None)
        assert "Geizhals: bargain or cheaper" in c.get("/agents").text
        c.post(f"/agents/{a.id}/edit", data={**form, "gh_on": "on", "gh_level": "pct"})      # own percentage
        with session() as db:
            row = db.get(SearchAgent, a.id)
            assert (row.geizhals_level, row.geizhals_pct) == (None, 25)
        c.post(f"/agents/{a.id}/edit", data={**form, "gh_level": "cheap"})                  # switch off
        with session() as db:
            row = db.get(SearchAgent, a.id)
            assert (row.geizhals_level, row.geizhals_pct) == (None, None)


async def test_a_new_hit_is_pending_until_it_passed(fake, world, monkeypatch):
    """New shows only hits that passed the rule and the screening: meanwhile they are pending (their own tab); one
    Claude was not sent stays pending and is judged first in the next check."""
    states, sent_to_claude = [], []

    async def screen_all(agent_id, hits):
        states.extend(h.state for h in _hits(agent_id).values())      # what the hits page shows meanwhile
        hits = hits[:1]                                               # MAX_PER_RUN: one
        sent_to_claude.append([h.ad_id for h in hits])
        with session() as db:
            for h in hits:
                row = db.get(Hit, h.id)
                row.screen_relevant, row.screen_deal = True, "great"
                h.screen_relevant, h.screen_deal = True, "great"
                db.add(row)
            db.commit()
        return {h.id for h in hits}

    monkeypatch.setattr(screening, "screen_all", screen_all)
    from app import scheduler
    follow_ups = []
    monkeypatch.setattr(scheduler, "follow_up", follow_ups.append)
    a = create_agent("https://fake.example/s/iphone")
    fake.pages = [[]]
    await run_agent(a.id)
    _set(a.id, geizhals_level="bargain", screen=True)
    fake.pages = [[listing("1", "iPhone 13 128GB", 100.0), listing("2", "iPhone 13 128GB", 400.0),
                   listing("3", "iPhone 13 128GB", 110.0)]]
    await run_agent(a.id)
    assert states == ["pending"] * 3
    assert {k: h.state for k, h in _hits(a.id).items()} == {"1": "new", "2": "filtered", "3": "pending"}
    assert follow_ups == [a.id]                                       # the one left gets a check soon
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert "Being checked" in c.get("/hits").text
    fake.pages = [[listing("1", "iPhone 13 128GB", 100.0), listing("2", "iPhone 13 128GB", 400.0),
                   listing("3", "iPhone 13 128GB", 110.0), listing("4", "iPhone 13 128GB", 90.0)]]
    await run_agent(a.id)
    assert sent_to_claude[-1] == ["3"]                                # the one left over first
    assert {k: h.state for k, h in _hits(a.id).items()} == {"1": "new", "2": "filtered", "3": "new", "4": "pending"}
    _set(a.id, geizhals_level=None, screen=False)                     # switched off: what waits is simply new
    await run_agent(a.id)
    assert _hits(a.id)["4"].state == "new" and follow_ups == [a.id, a.id]


def test_without_a_geizhals_price_the_zone_asks_claudes_verdict():
    a = SearchAgent(name="x", source_url="x", webapi_url="x", geizhals_level="bargain")
    h = Hit(agent_id=1, ad_id="1", heading="UniFi Dream Router", url="x", price=200.0, geizhals={})
    assert geizhals_passes(a, h)                                         # not screened yet: nothing to compare
    for deal, ok in (("overpriced", False), ("fair", False), ("good", False), ("great", True), ("unknown", True)):
        h.screen_deal = deal
        assert geizhals_passes(a, h) is ok, deal
    a.geizhals_level = "cheap"
    h.screen_deal = "good"
    assert geizhals_passes(a, h)
    a.geizhals_level, a.geizhals_pct = None, 30                         # an own % keeps passing unknown products
    h.screen_deal = "overpriced"
    assert geizhals_passes(a, h)


async def test_the_first_checks_hits_are_judged_too_without_a_push(fake, world, monkeypatch):
    """What an agent finds on its first check counts as seen (no push), but is judged like a new hit: what fails the
    screening goes to "filtered", not unjudged into "seen"; one Claude was not sent waits for the next check."""
    _lookups, sent = world

    async def screen_all(agent_id, hits):
        hits = hits[:2]                                               # MAX_PER_RUN: two
        with session() as db:
            for h in hits:
                row = db.get(Hit, h.id)
                row.screen_relevant = h.screen_relevant = "hülle" not in h.heading.lower()
                row.screen_deal = h.screen_deal = "fair"
                db.add(row)
            db.commit()
        return {h.id for h in hits}

    monkeypatch.setattr(screening, "screen_all", screen_all)
    from app import scheduler
    follow_ups = []
    monkeypatch.setattr(scheduler, "follow_up", follow_ups.append)
    a = create_agent("https://fake.example/s/iphone")
    _set(a.id, screen=True)
    fake.pages = [[listing("1", "iPhone 13 128GB", 300.0), listing("2", "Hülle für iPhone 13", 10.0),
                   listing("3", "iPhone 13 128GB", 310.0)]]
    res = await run_agent(a.id)
    assert res.baseline and not res.new
    assert {k: h.state for k, h in _hits(a.id).items()} == {"1": "seen", "2": "filtered", "3": "pending"}
    assert all(h.quiet for h in _hits(a.id).values()) and follow_ups == [a.id]
    await run_agent(a.id)                                             # the one left over, still without a push
    assert _hits(a.id)["3"].state == "seen" and not [s for s in sent if s["kind"] != "health"]
    _set(a.id, screen=False)                                          # an agent that does not judge: seen as before
    fake.pages = [[listing("1", "iPhone 13 128GB", 300.0)]]
    b = create_agent("https://fake.example/s/iphone2")
    fake.pages = [[listing("9", "Hülle", 5.0)]]
    await run_agent(b.id)
    assert _hits(b.id)["9"].state == "seen" and _hits(b.id)["9"].quiet is None
