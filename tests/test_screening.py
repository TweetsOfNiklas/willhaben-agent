import copy

import httpx
import pytest
from sqlmodel import select

from app import db as dbmod
from app.db import set_setting
from app.llm.base import LLMError, LLMResult
from app.services import screening
from app.services.search_agents import create_agent, run_agent
from tests.conftest import load
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.test_search_agents import URL, _set_attr, ha, responses  # noqa: F401  (pytest fixtures)


class FakeScreen:
    model = "fake-haiku"

    def __init__(self, results=None, fail=False):
        self.calls, self.results, self.fail = [], results, fail

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"prompt": prompt, "images": list(images or []), "schema": schema})
        if self.fail:
            raise LLMError("rate limited")
        return LLMResult(data={"results": self.results}, model=self.model)


@pytest.fixture
def images(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(screening, "_transport", httpx.MockTransport(handler))


async def _agent_with_two_new(queue, minimum="good"):
    base = load("search_fahrrad_rows3.json")
    agent = create_agent(URL)
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a.screen, a.screen_brief, a.screen_min = True, "Rennrad RH 56, kein E-Bike", minimum
        db.add(a)
        db.commit()
    queue.append(base)
    await run_agent(agent.id)                         # baseline: silent, nothing screened
    second = copy.deepcopy(base)
    for n, heading in ((1, "Rennrad Shimano 105 RH 56"), (2, "Fahrradschlauch 28 Zoll")):
        ad = copy.deepcopy(second["advertSummaryList"]["advertSummary"][0])
        ad["id"] = f"99900011{n}"
        _set_attr(ad, "ADID", ad["id"])
        _set_attr(ad, "HEADING", heading)
        second["advertSummaryList"]["advertSummary"].insert(0, ad)
    queue.append(second)
    return agent


def _pushes(sent):
    return [b for p, b in sent if p.startswith("/api/services/notify/")]


async def test_screening_limits_pushes_but_keeps_events(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    # newest first in the search -> nr 1 = the tube (inserted last), nr 2 = the road bike
    fake = FakeScreen([{"nr": 1, "relevant": False, "deal": "unknown", "reason": "Nur ein Schlauch"},
                       {"nr": 2, "relevant": True, "deal": "good", "reason": "30 % unter üblichem Preis"}])
    monkeypatch.setattr(screening, "get_backend", lambda *a: fake)
    r = await run_agent(agent.id)

    assert len(r.new) == 2 and len(fake.calls) == 1
    prompt = fake.calls[0]["prompt"]
    assert "Rennrad RH 56, kein E-Bike" in prompt and "[1] Fahrradschlauch" in prompt and "Foto: ./foto1.jpg" in prompt
    assert len(fake.calls[0]["images"]) == 2
    events = [b for p, b in ha if p == "/api/events/willhaben_hit"]
    assert len(events) == 2                                           # HA automations still see both
    rejected = next(e for e in events if e["ad_id"] == "999000112")
    assert rejected["screen_relevant"] is False and rejected["screen_reason"] == "Nur ein Schlauch"
    pushes = _pushes(ha)
    assert len(pushes) == 1 and "Rennrad Shimano 105" in pushes[0]["message"]
    assert "🤖 good price: 30 % unter üblichem Preis" in pushes[0]["message"]
    with dbmod.session() as db:
        tube = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == "999000112")).one()
    assert (tube.screen_relevant, tube.screen_deal) == (False, "unknown")


async def test_screening_fails_open(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    monkeypatch.setattr(screening, "get_backend", lambda *a: FakeScreen(fail=True))
    await run_agent(agent.id)
    assert len(_pushes(ha)) == 2                                     # Claude down: every hit pushes as before


def test_passes_thresholds():
    agent = dbmod.SearchAgent(name="x", source_url="u", webapi_url="u", screen=True, screen_min="good")
    hit = dbmod.Hit(agent_id=1, ad_id="1", heading="x", url="u")
    assert screening.passes(agent, hit)                               # not screened yet
    hit.screen_relevant, hit.screen_deal = True, "fair"
    assert not screening.passes(agent, hit)
    hit.screen_deal = "great"
    assert screening.passes(agent, hit)
    hit.screen_relevant = False
    assert not screening.passes(agent, hit)
    agent.screen_min, hit.screen_relevant, hit.screen_deal = "fair", True, "unknown"
    assert screening.passes(agent, hit)                               # unclear price passes "fair", not "good"
    agent.screen = False
    hit.screen_relevant = False
    assert screening.passes(agent, hit)                               # screening off: everything notifies


def test_agent_form_settings_and_hit_badge(client):  # noqa: F811
    agent = create_agent(URL)
    client.post(f"/agents/{agent.id}/edit", data={"name": "Rennrad", "interval_min": "5", "notify_on": "on",
                                                  "screen_on": "on", "screen_brief": "RH 56\r\nkein E-Bike",
                                                  "screen_min": "fair"})
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a_screen = (a.screen, a.screen_brief, a.screen_min)
        db.add(dbmod.Hit(agent_id=agent.id, ad_id="5", heading="Schlauch", url="https://x", state="new",
                         screen_relevant=False, screen_deal="unknown", screen_reason="Nur Zubehör"))
        db.commit()
    assert a_screen == (True, "RH 56\nkein E-Bike", "fair")
    page = client.get("/agents").text
    assert "🤖" in page and 'name="screen_brief"' in page and "only fair or better prices" in page
    assert 'name="purpose" value="screen"' in client.get("/settings").text
    assert "wrong item" in client.get(f"/hits?agent_id={agent.id}").text
    client.post(f"/agents/{agent.id}/edit", data={"name": "Rennrad", "interval_min": "5", "screen_min": "bogus"})
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
    assert a.screen is False and a.screen_min == "match"
