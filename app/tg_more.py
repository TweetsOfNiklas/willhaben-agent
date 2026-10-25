"""The Telegram Mini App's notifications (/tg/inbox), background jobs (/tg/jobs) and its "more" page (/tg/more): the
dashboard's admin pages (settings, statistics, timeline, simulator, the remote browser, bug report) open as they are
inside the Mini App; Telegram's back button returns (templates/base.html)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.tg_bridge import tg_path
from app.tg_web import _render

router = APIRouter()

# the dashboard's own pages, opened as they are: (path, label, icon)
ADMIN = (("/settings", "Settings", "settings"), ("/stats/willhaben", "Statistics", "database"),
         ("/timeline", "Timeline", "clock"), ("/simulator", "Chat simulator", "bot"), ("/agent-sim", "Search agent simulator", "detective"),
         ("/session", "Browser", "monitor"), ("/credits", "Credits", "star"), ("/bugreport", "Report a bug", "bug"))


@router.get("/tg/inbox", response_class=HTMLResponse)
async def tg_inbox(request: Request):
    """The dashboard's inbox: hits, drafts, expiring ads, connection problems; each opens its /tg page."""
    from app import main
    items = []
    for n in main._inbox_items(limit=50):
        link = n.get("link") or {}
        href = f"/tg/ad/{link['ad']}" if link.get("ad") else tg_path(link.get("href") or "/") or link.get("href") or "/tg"
        items.append({**n, "href": href, "external": bool(link.get("external")) or not href.startswith("/tg")})
    return _render(request, "inbox.html", items=items, unread=main._nav_counts()["inbox"])


@router.get("/tg/jobs", response_class=HTMLResponse)
async def tg_jobs(request: Request):
    """What runs in the background (willhaben steps, Claude, sending), queued ones can be taken back."""
    from app.services import jobs
    snap = jobs.snapshot()
    return _render(request, "jobs.html", active=snap["active"], recent=snap["recent"])


@router.get("/tg/more", response_class=HTMLResponse)
async def tg_more(request: Request):
    return _render(request, "more.html", admin=ADMIN)
