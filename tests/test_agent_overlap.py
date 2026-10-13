"""Never two checks of one search agent at once: "Run now" during a scheduled check sent every search twice and eBay
answered the second with 403 (2026-10-13 23:04)."""
import asyncio

from app import scheduler
from app.db import SearchAgent, session
from app.services import search_agents
from app.services.search_agents import RunResult


def _agent() -> int:
    with session() as db:
        a = SearchAgent(name="Unifi", source_url="https://www.willhaben.at/iad/x", webapi_url="https://www.willhaben.at/webapi/x")
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id


def _fake_checks(monkeypatch):
    seen = {"active": 0, "most": 0, "runs": 0}

    async def check(agent_id):
        seen["active"] += 1
        seen["most"] = max(seen["most"], seen["active"])
        await asyncio.sleep(0.05)
        seen["active"] -= 1
        seen["runs"] += 1
        return RunResult(agent_id=agent_id)

    monkeypatch.setattr(search_agents, "_run_agent", check)
    return seen


async def test_run_now_waits_for_the_running_check(monkeypatch):
    seen = _fake_checks(monkeypatch)
    aid = _agent()
    first = asyncio.create_task(search_agents.run_agent(aid))
    await asyncio.sleep(0.01)
    assert search_agents.is_checking(aid)
    await asyncio.gather(first, search_agents.run_agent(aid), search_agents.run_agent(aid))
    assert seen["runs"] == 3 and seen["most"] == 1                 # all ran, one after another
    assert not search_agents.is_checking(aid)


async def test_scheduler_skips_while_a_check_runs(monkeypatch):
    seen = _fake_checks(monkeypatch)
    monkeypatch.setattr(scheduler.quiet_hours, "should_skip", lambda a: False)
    monkeypatch.setattr(scheduler.human_pace, "take_break", lambda: False)
    aid = _agent()
    manual = asyncio.create_task(search_agents.run_agent(aid))
    await asyncio.sleep(0.01)
    await scheduler._run_agent_job(aid)                            # its turn comes while "Run now" runs: skipped
    await manual
    assert seen["runs"] == 1
    await scheduler._run_agent_job(aid)                            # next turn: runs
    assert seen["runs"] == 2


async def test_a_failing_check_frees_the_agent(monkeypatch):
    async def boom(agent_id):
        raise RuntimeError("db gone")

    monkeypatch.setattr(search_agents, "_run_agent", boom)
    aid = _agent()
    try:
        await search_agents.run_agent(aid)
    except RuntimeError:
        pass
    assert not search_agents.is_checking(aid)
