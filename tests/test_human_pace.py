import random

from app.services import human_pace


def test_trigger_spreads_around_the_interval():
    human_pace.save(30, True)
    base, jitter = human_pace.trigger(300, 120)
    assert (base, jitter) == (255, 90)                    # runs land in 255..345 s, centred on 300
    human_pace.save(0, False)
    assert human_pace.trigger(300, 120) == (300, 60)      # off: the old small jitter
    human_pace.save(80, True)
    base, jitter = human_pace.trigger(120, 120)
    assert base >= 102 and jitter == 96                   # never far below the minimum interval
    human_pace.save(500, True)
    assert human_pace.config()["variance_pct"] == 80


def test_breaks_about_one_in_twelve():
    human_pace.save(30, True)
    rng = random.Random(1)
    skipped = sum(human_pace.take_break(rng) for _ in range(12000))
    assert 800 < skipped < 1200
    human_pace.save(30, False)
    assert not any(human_pace.take_break(rng) for _ in range(500))


async def test_scheduled_run_takes_a_break(monkeypatch):
    from app import scheduler
    from app.services.search_agents import create_agent
    from tests.test_search_agents import URL

    agent = create_agent(URL)
    ran = []

    async def fake_run(agent_id):
        ran.append(agent_id)

    monkeypatch.setattr(scheduler, "run_agent", fake_run)
    monkeypatch.setattr(scheduler.quiet_hours, "should_skip", lambda a, now=None: False)   # any time of day
    monkeypatch.setattr(human_pace, "take_break", lambda rng=None: True)
    await scheduler._run_agent_job(agent.id)
    assert ran == []
    monkeypatch.setattr(human_pace, "take_break", lambda rng=None: False)
    await scheduler._run_agent_job(agent.id)
    assert ran == [agent.id]


def test_settings_form_saves_and_reschedules(monkeypatch):
    from fastapi.testclient import TestClient

    from app import scheduler
    from app.main import app
    calls = []
    monkeypatch.setattr(scheduler, "reschedule_agents", lambda: calls.append(1))
    with TestClient(app) as c:
        c.post("/settings/human-pace", data={"variance_pct": "40"})
        assert human_pace.config() == {"variance_pct": 40, "breaks": False} and calls
        assert 'name="variance_pct" value="40"' in c.get("/settings").text
