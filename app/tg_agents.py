"""The Telegram Mini App's search agents (/tg/agents): the list with their switches, creating, editing, running and
deleting one, several at once (the dashboard's agent pages in the Mini App's look; actions are the dashboard's
endpoints via app/tg_bridge.py)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import func, select

from app.db import Hit, SearchAgent, session
from app.tg_web import _back, _page, _render

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tg/agents", response_class=HTMLResponse)
async def tg_agents(request: Request, error: str | None = None):
    """The dashboard's agents page (main.agents_page): each agent with its switch, new hits, last and next check."""
    from app import main
    return await _page(request, "agents.html", main.agents_page, error=error, msg=None)


@router.get("/tg/agents/new", response_class=HTMLResponse)
async def tg_agent_new(request: Request):
    """The dashboard's builder (main.agents_new, which reads this page's query: url, keyword, sites, r_<filter>)."""
    from app import main
    return await _page(request, "agent_new.html", main.agents_new)


@router.get("/tg/agents/{agent_id}", response_class=HTMLResponse)
async def tg_agent(request: Request, agent_id: int):
    """One agent with everything its settings drawer offers."""
    from app import scheduler
    from app.config import get_settings
    from app.services import jobs, screening
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        if a is None:
            raise HTTPException(404)
        new = db.exec(select(func.count()).select_from(Hit).where(Hit.agent_id == agent_id, Hit.state == "new")).one()
    return _render(request, "agent.html", a=a, new=new, settings=get_settings(), thresholds=screening.thresholds(),
                   busy=f"agent:{agent_id}" in set(jobs.current_keys("agent:")), next_run=scheduler.next_run)


@router.post("/tg/agents/{agent_id}/toggle")
async def tg_agent_toggle(agent_id: int, back: str = Form("")):
    """The switch on the Mini App's home and list: paused or running; back to the page it came from (`back`)."""
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        if a is None:
            raise HTTPException(404)
        a.enabled = not a.enabled
        db.add(a)
        db.commit()
        db.refresh(a)
    from app import scheduler
    try:
        scheduler.schedule_agent(a)
    except Exception as e:  # noqa: BLE001 - the switch is stored; the next start schedules it
        log.info("reschedule after toggle failed: %s", e)
    return _back(back if back.startswith("/tg") else "/tg/agents")
