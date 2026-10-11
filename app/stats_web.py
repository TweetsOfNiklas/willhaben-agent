"""willhaben access statistics: page /stats/willhaben (linked from the footer counter) and /api/stats/willhaben.

Kept out of main.py (one include there); `render` is imported from app.main at call time.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services import access_stats
from app.willhaben.public import willhaben_limiter

log = logging.getLogger(__name__)
router = APIRouter()


def footer_used() -> int:
    """Footer counter: from the persisted access log (survives restarts); the limiter's own count as a fallback.
    Unpaced live-chat reconnect attempts are left out, the footer is compared against the limiter budget."""
    try:
        return access_stats.last_hour(exclude=access_stats.UNPACED, platform="willhaben")
    except Exception as e:  # noqa: BLE001 - the footer must always render
        log.debug("access stats unavailable: %s", e)
        return willhaben_limiter().last_hour


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    templates.env.globals.update(wh_used=footer_used)
    app.include_router(router)


def _platform(pid: str) -> str:
    from app import platforms

    pid = (pid or "willhaben").strip().lower()
    try:
        platforms.get(pid)
    except platforms.UnknownPlatform as e:
        raise HTTPException(404) from e
    return pid


@router.get("/stats/requests", response_class=HTMLResponse)
async def stats_page(request: Request, range: str = access_stats.DEFAULT_RANGE, platform: str = "willhaben"):  # noqa: A002
    from app import platforms
    from app.main import render

    pid = _platform(platform)
    data = access_stats.series(range, platform=pid)
    return render(request, "stats_willhaben.html", data=data, chart=access_stats.chart(data),
                  channel_label=access_stats.channel_label, kind_label=access_stats.kind_label,
                  outcome_label=access_stats.outcome_label, platform_choices=platforms.all(enabled_only=False))


@router.get("/stats/willhaben")
async def stats_willhaben(range: str = access_stats.DEFAULT_RANGE):  # noqa: A002
    return RedirectResponse(f"/stats/requests?platform=willhaben&range={range}", status_code=307)


@router.get("/api/stats/requests")
async def api_stats_requests(range: str = access_stats.DEFAULT_RANGE, platform: str = "willhaben"):  # noqa: A002
    return access_stats.public_json(access_stats.series(range, platform=_platform(platform)))


@router.get("/api/stats/willhaben")
async def api_stats(range: str = access_stats.DEFAULT_RANGE):  # noqa: A002
    return access_stats.public_json(access_stats.series(range))
