"""willhaben access statistics: page /stats/willhaben (linked from the footer counter) and /api/stats/willhaben.

Kept out of main.py (one include there); `render` is imported from app.main at call time.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services import access_stats
from app.willhaben.public import willhaben_limiter

log = logging.getLogger(__name__)
router = APIRouter()


def footer_used() -> int:
    """Footer counter: from the persisted access log (survives restarts); the limiter's own count as a fallback.
    Unpaced live-chat reconnect attempts are left out, the footer is compared against the limiter budget."""
    try:
        return access_stats.last_hour(exclude=access_stats.UNPACED)
    except Exception as e:  # noqa: BLE001 - the footer must always render
        log.debug("access stats unavailable: %s", e)
        return willhaben_limiter().last_hour


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    templates.env.globals.update(wh_used=footer_used)
    app.include_router(router)


@router.get("/stats/willhaben", response_class=HTMLResponse)
async def stats_page(request: Request, range: str = access_stats.DEFAULT_RANGE):  # noqa: A002 - query name
    from app.main import render

    data = access_stats.series(range)
    return render(request, "stats_willhaben.html", data=data, chart=access_stats.chart(data),
                  channel_label=access_stats.channel_label, kind_label=access_stats.kind_label,
                  outcome_label=access_stats.outcome_label)


@router.get("/api/stats/willhaben")
async def api_stats(range: str = access_stats.DEFAULT_RANGE):  # noqa: A002
    return access_stats.public_json(access_stats.series(range))
