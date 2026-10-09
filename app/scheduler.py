"""Background jobs (APScheduler, asyncio)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import select

from app.config import get_settings
from app.db import SearchAgent, session, utcnow
from app.services import chats, my_ads, quiet_hours
from app.services.search_agents import run_agent
from app.willhaben.session import get_session

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Europe/Vienna")


def _agent_job(agent_id: int) -> str:
    return f"agent-{agent_id}"


async def _run_agent_job(agent_id: int) -> None:
    """Scheduled agent run; quiet hours may skip it (manual runs call run_agent directly)."""
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
    if agent is None:
        return
    if quiet_hours.should_skip(agent):
        log.debug("agent %s: quiet hours, skipped", agent_id)
        return
    await run_agent(agent_id)


def schedule_agent(agent: SearchAgent, run_soon: bool = True) -> None:
    job_id = _agent_job(agent.id)
    if not agent.enabled:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        return
    extra = {"next_run_time": datetime.now().astimezone() + timedelta(seconds=5)} if run_soon else {}
    # Note: passing next_run_time=None would add the job *paused* in APScheduler 3.
    scheduler.add_job(
        _run_agent_job, IntervalTrigger(seconds=agent.interval_s, jitter=min(60, agent.interval_s // 5)),
        args=[agent.id], id=job_id, replace_existing=True, max_instances=1, coalesce=True, **extra,
    )


def unschedule_agent(agent_id: int) -> None:
    if scheduler.get_job(_agent_job(agent_id)):
        scheduler.remove_job(_agent_job(agent_id))


async def _session_check() -> None:
    sess = get_session()
    if sess.started:
        try:
            await sess.check_login()
        except Exception as e:  # noqa: BLE001
            log.warning("session check failed: %s", e)


LIVE_FALLBACK = timedelta(minutes=15)


async def _chat_sync() -> None:
    # The live WebSocket triggers syncs as messages arrive; polling is only the safety net then.
    last = chats.last_sync_at[0]
    if get_session().chat_live_ok() and last and utcnow() - last < LIVE_FALLBACK:
        return
    try:
        await chats.sync()
    except Exception as e:  # noqa: BLE001 - keep the job alive
        log.warning("chat sync failed: %s", e)


def start() -> None:
    s = get_settings()
    with session() as db:
        agents = list(db.exec(select(SearchAgent)))
    for a in agents:
        schedule_agent(a, run_soon=False)
    scheduler.add_job(_session_check, IntervalTrigger(seconds=s.session_check_interval_s), id="session-check",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(_chat_sync, IntervalTrigger(seconds=s.chat_poll_interval_s, jitter=30), id="chat-sync",
                      replace_existing=True, max_instances=1, coalesce=True,
                      next_run_time=datetime.now().astimezone() + timedelta(seconds=30))
    scheduler.add_job(my_ads.check_all, CronTrigger(hour=s.myads_check_hour, minute=7), id="myads-check",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()
    # Stagger the first agent runs so a restart does not burst requests.
    for i, a in enumerate(x for x in agents if x.enabled):
        job = scheduler.get_job(_agent_job(a.id))
        if job:
            job.modify(next_run_time=datetime.now().astimezone() + timedelta(seconds=15 + i * 20))


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def next_run(agent_id: int):
    job = scheduler.get_job(_agent_job(agent_id))
    return job.next_run_time if job else None
