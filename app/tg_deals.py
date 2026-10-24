"""The Telegram Mini App's price check and product search (/tg/search, /tg/check): any ad link or search term, the
price position with Claude's verdict, Claude's product search (the dashboard's deals pages in the Mini App's look;
actions via app/tg_bridge.py)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.tg_web import _back, _page, _render

log = logging.getLogger(__name__)
router = APIRouter()


def _search_notice(code: str | None) -> str | None:
    """The dashboard's short codes in its redirects (deals_web._search_ctx) as their text; None for anything else."""
    from app.i18n import t
    return {"busy": t("A product search is still running – wait for it to finish."),
            "empty": t("Describe what you are looking for."),
            "noplan": t("This search has no planned willhaben searches yet.")}.get(code or "")


def _with_msg(path: str, msg: str | None) -> str:
    return f"{path}{'&' if '?' in path else '?'}msg={quote(msg, safe='')}" if msg else path


@router.get("/tg/search", response_class=HTMLResponse)
async def tg_search(request: Request, q: str = "", search: int | None = None, msg: str | None = None,
                    from_: int | None = Query(None, alias="from"), sites: list[str] = Query([])):
    """Search: Claude's product search (AI) or the direct search – an ad link or id checks that ad's price, a text
    lists the ads of the ticked sites. The dashboard's redirects (/deals?search=…) come here and go on to the search."""
    from app import deals_web
    from app.services import deals
    notice = _search_notice(msg)
    if search:
        return _back(_with_msg(f"/tg/search/{search}", notice or msg))
    if notice:                                      # a code from the dashboard's redirect: its text instead
        return _back(_with_msg("/tg/search" + (f"?{urlencode({'from': from_})}" if from_ else ""), notice))
    q = q.strip()
    if ref := deals.parse_ad_ref(q):
        return _back(f"/tg/check/{ref[0]}/{quote(ref[1], safe='')}")
    return await _page(request, "search.html", deals_web.deals_page, q=q, search=None, msg=None, from_=from_,
                       sites=sites)


@router.get("/tg/search/{search_id}", response_class=HTMLResponse)
async def tg_search_result(request: Request, search_id: int):
    """One product search: its progress while it runs (the page asks again), Claude's questions, the offers."""
    from app import deals_web
    from app.services import product_search
    if product_search.get(search_id) is None:
        raise HTTPException(404)
    return _render(request, "search_result.html", **deals_web._search_ctx(search_id))


# ------------------------------------------------------------------ price check of one ad
# Claude's verdict and its Geizhals search take up to half a minute, longer than a phone keeps a request open: they
# run on their own ((platform, ad id, what) → task) and the result section asks again until they are done
_running: dict[tuple[str, str, str], asyncio.Task] = {}
_errors: dict[tuple[str, str, str], str] = {}


def _check_url(platform: str, ad_id: str, suffix: str = "", **params: Any) -> str:
    """/tg/check/<platform>/<ad id>[suffix] with the parameters that are set."""
    keep = {k: v for k, v in params.items() if v not in (None, "")}
    return f"/tg/check/{platform}/{quote(ad_id, safe='')}{suffix}" + (f"?{urlencode(keep)}" if keep else "")


@router.get("/tg/check/{platform}/{ad_id}", response_class=HTMLResponse)
async def tg_check(request: Request, platform: str, ad_id: str, q: str = "", cat: str = "", hit: int | None = None,
                   own: str = "", was: str | None = None):
    """The price check of any ad: the page answers at once, the analysis (site requests) loads into it. q from a
    list only changes the search; "Compare again" sends the term it showed as `was`, a changed one is the user's own
    (as deals_web.deals_ad_platform)."""
    from app import deals_web
    p = deals_web._platform(platform)
    own_q = own == "1" or (was is not None and " ".join(q.split()).lower() != " ".join(was.split()).lower())
    return _render(request, "check.html", platform=p.id, ad_id=ad_id, q=q.strip(),
                   result_url=_check_url(p.id, ad_id, "/result", q=q.strip(), cat=cat, hit=hit,
                                         own="1" if own_q and q.strip() else ""))


@router.get("/tg/check/{platform}/{ad_id}/result", response_class=HTMLResponse)
async def tg_check_result(request: Request, platform: str, ad_id: str, q: str = "", cat: str = "",
                          hit: int | None = None, own: str = ""):
    """The analysis of one ad (deals_web.deals_ad_result_platform) in the Mini App's look, with what runs for it."""
    from app import deals_web
    p = deals_web._platform(platform)
    resp = await deals_web.deals_ad_result_platform(request, p.id, ad_id, q=q, cat=cat, hit=hit, own=own)
    ctx = {k: v for k, v in (getattr(resp, "context", None) or {}).items() if k != "request"}
    running = {what: bool((task := _running.get((p.id, ad_id, what))) and not task.done())
               for what in ("claude", "geizhals")}
    return _render(request, "_check_result.html", **ctx, platform=p.id, ad_id=ad_id, hit=hit or "",
                   cat=cat, own=own, running=running,
                   run_errors={what: _errors.get((p.id, ad_id, what)) for what in ("claude", "geizhals")},
                   self_url=_check_url(p.id, ad_id, "/result", q=q, cat=cat, hit=hit, own=own))


async def _run(request: Request, platform: str, ad_id: str, what: str, q: str, hit: str, cat: str, own: str) -> None:
    """Claude's verdict or its Geizhals search through the dashboard's handler (its job line, the stored result);
    the error it showed is kept for the result section."""
    from app import deals_web
    key = (platform, ad_id, what)
    try:
        if what == "claude":
            resp = await deals_web.deal_claude_platform(request, platform, ad_id, q=q, hit=hit, cat=cat, own=own)
        else:
            resp = await deals_web.deal_geizhals_platform(request, platform, ad_id, q=q, hit=hit, cat=cat,
                                                          compact="", own=own)
        ctx = getattr(resp, "context", None) or {}
        res = ctx.get("res") or {}
        error = ctx.get("error") or (((res.get("claude") or {}).get("error")) if what == "claude"
                                     else res.get("geizhals_error"))
    except Exception as e:  # noqa: BLE001 - shown in the result section
        log.warning("mini app %s for %s:%s failed", what, platform, ad_id, exc_info=True)
        error = str(e) or type(e).__name__
    if error:
        _errors[key] = str(error)


@router.post("/tg/check/{platform}/{ad_id}/{what}")
async def tg_check_run(request: Request, platform: str, ad_id: str, what: str, q: str = Form(""),
                       hit: str = Form(""), cat: str = Form(""), own: str = Form("")):
    """Ask Claude (its verdict) or let Claude search Geizhals: started here, the page shows it running."""
    from app import deals_web
    if what not in ("claude", "geizhals"):
        raise HTTPException(404)
    p = deals_web._platform(platform)
    key = (p.id, ad_id, what)
    if not (task := _running.get(key)) or task.done():
        _errors.pop(key, None)
        _running[key] = asyncio.create_task(_run(request, p.id, ad_id, what, q, hit, cat, own))
    return RedirectResponse(_check_url(p.id, ad_id, q=q, cat=cat, hit=int(hit) if hit.isdigit() else None,
                                       own=own), status_code=303)
