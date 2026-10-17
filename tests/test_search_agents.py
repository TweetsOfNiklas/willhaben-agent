import copy
import json

import httpx
import pytest
from sqlmodel import select

from app import db as dbmod
from app.services import notify as notify_mod
from app.services.search_agents import create_agent, run_agent
from app.willhaben import public as public_mod
from app.willhaben.public import PublicClient
from tests.conftest import load

URL = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad"


def _set_attr(ad, name, value):
    for a in ad["attributes"]["attribute"]:
        if a["name"] == name:
            a["values"] = [value]
            return
    ad["attributes"]["attribute"].append({"name": name, "values": [value]})


@pytest.fixture
def responses():
    """Queue of JSON payloads served by the mocked search endpoint."""
    queue: list = []
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = queue.pop(0)
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, json=body)

    public_mod.set_public(PublicClient(transport=httpx.MockTransport(handler)))
    yield queue, calls
    public_mod.set_public(None)


@pytest.fixture
def ha(monkeypatch):
    from app.config import get_settings

    sent: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"message": "ok"})

    s = get_settings()
    monkeypatch.setattr(s, "ha_url", "http://ha.local:8123")
    monkeypatch.setattr(s, "ha_token", "t")
    monkeypatch.setattr(notify_mod, "_transport", httpx.MockTransport(handler))
    return sent


async def test_baseline_then_new_and_price_drop(responses, ha):
    queue, calls = responses
    base = load("search_fahrrad_rows3.json")
    agent = create_agent(URL)
    assert agent.webapi_url.startswith("https://www.willhaben.at/webapi/ad-search/search/atz/seo/")

    # Run 1: baseline -> everything stored as seen, nothing pushed
    queue.append(base)
    r1 = await run_agent(agent.id)
    assert r1.error is None and r1.baseline and not r1.new
    assert calls[0].headers["x-wh-client"].startswith("api@willhaben.at;responsive_web")
    assert ha == []

    # Run 2: one brand-new ad + a price drop on an existing one
    second = copy.deepcopy(base)
    new_ad = copy.deepcopy(second["advertSummaryList"]["advertSummary"][0])
    new_ad["id"] = "999000111"
    _set_attr(new_ad, "ADID", "999000111")
    _set_attr(new_ad, "HEADING", "Brandneues Rennrad")
    second["advertSummaryList"]["advertSummary"].insert(0, new_ad)
    dropped = second["advertSummaryList"]["advertSummary"][2]
    _set_attr(dropped, "PRICE", "1")
    _set_attr(dropped, "PRICE/AMOUNT", "1.0")
    queue.append(second)
    # make sure the dropped ad had a higher price before
    with dbmod.session() as db:
        h = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == dropped["id"])).one()
        h.price = 500
        db.add(h)
        db.commit()

    r2 = await run_agent(agent.id)
    assert [h.ad_id for h in r2.new] == ["999000111"]
    assert [h.ad_id for h in r2.price_drops] == [dropped["id"]]
    paths = [p for p, _ in ha]
    assert paths.count("/api/events/willhaben_hit") == 1
    assert paths.count("/api/events/willhaben_price_drop") == 1
    hit_payload = next(b for p, b in ha if p.endswith("willhaben_hit"))
    assert hit_payload["ad_id"] == "999000111" and hit_payload["url"].startswith("https://www.willhaben.at/iad/")

    # Run 3: same data again -> nothing new
    queue.append(second)
    r3 = await run_agent(agent.id)
    assert not r3.new and not r3.price_drops
    with dbmod.session() as db:
        assert len(db.exec(select(dbmod.Hit)).all()) == 4


async def test_hits_keep_the_condition(responses):
    """The list's condition goes to the hit; a list without one keeps what is stored (Shpock: from the ad page)."""
    queue, _ = responses
    base = load("search_fahrrad_rows3.json")
    agent = create_agent(URL)
    queue.append(base)
    await run_agent(agent.id)
    with dbmod.session() as db:
        stored = {h.ad_id: (h.item_condition, h.item_condition_label) for h in db.exec(select(dbmod.Hit))}
    ids = [a["id"] for a in base["advertSummaryList"]["advertSummary"]]
    assert [stored[i] for i in ids] == [("used", "Gebraucht"), ("like_new", "Neuwertig"), ("new", "Neu")]
    second = copy.deepcopy(base)
    first, other = second["advertSummaryList"]["advertSummary"][:2]
    first["attributes"]["attribute"] = [a for a in first["attributes"]["attribute"] if a["name"] != "categorytreeattributeids"]
    _set_attr(other, "categorytreeattributeids", "[21;24, 2535;2536]")
    queue.append(second)
    await run_agent(agent.id)
    with dbmod.session() as db:
        stored = {h.ad_id: (h.item_condition, h.item_condition_label) for h in db.exec(select(dbmod.Hit))}
    assert stored[ids[0]] == ("used", "Gebraucht") and stored[ids[1]] == ("defect", "Defekt")


async def test_a_new_hit_takes_the_condition_another_agent_learned(responses):
    """Agent A's hit got its condition from the ad page (as Shpock states it only there); agent B finds the same ad in
    a list without one: its new hit starts with what A's knows, so both cards say the same."""
    queue, _ = responses
    base = load("search_fahrrad_rows3.json")
    for ad in base["advertSummaryList"]["advertSummary"]:
        ad["attributes"]["attribute"] = [a for a in ad["attributes"]["attribute"] if a["name"] != "categorytreeattributeids"]
    ids = [a["id"] for a in base["advertSummaryList"]["advertSummary"]]
    first = create_agent(URL)
    queue.append(copy.deepcopy(base))
    await run_agent(first.id)
    with dbmod.session() as db:
        hit = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == ids[0])).one()
        assert hit.item_condition is None
        hit.item_condition, hit.item_condition_label = "used", "Gut"           # the viewer wrote the ad page's
        db.add(hit)
        db.commit()
    second = create_agent(URL)
    queue.append(copy.deepcopy(base))
    await run_agent(second.id)
    with dbmod.session() as db:
        got = {h.ad_id: (h.item_condition, h.item_condition_label)
               for h in db.exec(select(dbmod.Hit).where(dbmod.Hit.agent_id == second.id))}
    assert got[ids[0]] == ("used", "Gut") and got[ids[1]] == (None, None)


async def test_blocked_sets_error_and_backoff(responses):
    queue, _ = responses
    agent = create_agent(URL)
    queue.append(429)
    r = await run_agent(agent.id)
    assert r.error and "429" in r.error
    assert public_mod.get_public().limiter.cooling_down_s > 0
    with dbmod.session() as db:
        assert db.get(dbmod.SearchAgent, agent.id).last_error


async def test_willhaben_agents_take_the_largest_page_once_quietly(responses, monkeypatch):
    """An agent asks willhaben for its largest page (one request covers more); the first check with it files the ads
    beyond the rows the agent asked for before as seen: they are older, the smaller page just never showed them."""
    from app.db import get_setting
    from app.platforms.willhaben import MAX_ROWS
    from app.services import search_agents as sa

    queue, calls = responses
    base = load("search_fahrrad_rows3.json")
    agent = create_agent(URL)
    queue.append(base)
    await run_agent(agent.id)                                         # baseline: all seen
    assert f"rows={MAX_ROWS}" in str(calls[-1].url) and get_setting(sa._wh_rows_key(agent.id)) == "1"

    dbmod.set_setting(sa._wh_rows_key(agent.id), "")                  # an agent from before the larger page
    monkeypatch.setattr(sa, "_old_rows", lambda spec: 1)              # ... that asked for one row
    page = copy.deepcopy(base)
    ads = page["advertSummaryList"]["advertSummary"]
    for i, ad in enumerate(ads):
        ad["id"] = f"88800{i}"
        _set_attr(ad, "ADID", f"88800{i}")
    queue.append(page)
    r = await run_agent(agent.id)
    assert [h.ad_id for h in r.new] == ["888000"]                     # within the old page: new as always
    with dbmod.session() as db:
        states = {h.ad_id: h.state for h in db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id.like("888%")))}
    assert states == {"888000": "new", "888001": "seen", "888002": "seen"}
    assert get_setting(sa._wh_rows_key(agent.id)) == "1"


async def test_a_running_check_shows_in_the_background_work(responses):
    from app.services import jobs

    queue, _calls = responses
    agent = create_agent(URL)
    seen = []
    real = public_mod.get_public().search

    async def search(url, cache_s=0):
        seen.append([j["label"] for j in jobs.snapshot()["active"]])
        return await real(url, cache_s=cache_s)
    public_mod.get_public().search = search
    queue.append(load("search_fahrrad_rows3.json"))
    await run_agent(agent.id)
    assert seen and any(label.startswith("Search agent") for label in seen[0])
    done = [j for j in jobs.snapshot()["recent"] if j["label"].startswith("Search agent")]
    assert done and done[0]["status"] == "done" and "first check" in done[0]["result"]
