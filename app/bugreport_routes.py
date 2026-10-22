"""Footer → "Report a bug": an anonymised report the owner can copy or download (app/services/bugreport.py).

Kept out of main.py (one `install` call there). The report is never sent anywhere: it is generated locally from
in-app state, shown on the page and offered as .txt/.json download. Also installs the log ring buffer and a handler
that logs unhandled request errors (route template + status, no bodies or query values) so they appear in the report.
"""
from __future__ import annotations

import json
import logging
import secrets
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.db import utcnow
from app.services import bugreport, logbuffer

router = APIRouter()
errlog = logging.getLogger("secondhandler.errors")
KEEP = 5
_reports: OrderedDict[str, dict[str, Any]] = OrderedDict()   # token -> {"text", "json", "name"}; memory only


async def _unhandled(request: Request, exc: Exception) -> Response:
    route = getattr(request.scope.get("route"), "path", None) or "<unmatched route>"
    errlog.error("unhandled error: %s %s -> 500 (%s)", request.method, route, type(exc).__name__, exc_info=exc)
    return PlainTextResponse("Internal Server Error", status_code=500)


def install(app: FastAPI) -> None:
    logbuffer.install()
    app.add_exception_handler(Exception, _unhandled)
    app.include_router(router)


def _render(request: Request, **ctx: Any) -> HTMLResponse:
    from app.main import render
    return render(request, "bugreport.html", removed=bugreport.REMOVED, **ctx)


@router.get("/bugreport", response_class=HTMLResponse)
async def bugreport_page(request: Request):
    return _render(request, report=None, description="", include_info=False, include_settings=True)


@router.post("/bugreport", response_class=HTMLResponse)
async def bugreport_create(request: Request, description: str = Form(""), include_info: str = Form(""),
                           include_settings: str = Form("")):
    report = bugreport.build(description, include_info=bool(include_info), include_settings=bool(include_settings))
    text = bugreport.render_text(report)
    token = secrets.token_urlsafe(12)
    _reports[token] = {"text": text, "json": json.dumps(report, ensure_ascii=False, indent=1, default=str),
                       "name": f"secondhandler-bugreport-{utcnow():%Y%m%d-%H%M%S}"}
    while len(_reports) > KEEP:
        _reports.popitem(last=False)
    return _render(request, report=text, token=token, description=description, include_info=bool(include_info),
                   include_settings=bool(include_settings))


@router.get("/bugreport/{token}.{ext}")
async def bugreport_download(token: str, ext: str):
    r = _reports.get(token)
    if not r or ext not in ("txt", "json"):
        raise HTTPException(404)
    media = "text/plain; charset=utf-8" if ext == "txt" else "application/json"
    return Response(r["text"] if ext == "txt" else r["json"], media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{r["name"]}.{ext}"'})
