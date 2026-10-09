"""Produktsuche routes (formerly Preischeck): the /deals tab with the Claude product search and the single-ad price
check, plus the lazily loaded price section of the ad viewer.

Kept out of main.py (one include there); `render`/`templates` are imported from app.main at call time.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import Hit, SearchAgent, session
from app.i18n import t
from app.services import deals, product_search
from app.willhaben.parse import url_param
from app.willhaben.public import WillhabenError, get_public

router = APIRouter()


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    templates.env.globals.update(deal_level_label=deals.level_label, deal_explain=deals.explain)
    app.include_router(router)


def _templates() -> Jinja2Templates:
    from app.main import templates
    return templates


def _hit(hit_id: int | None) -> tuple[Hit | None, str | None]:
    """The hit and its search agent's keyword (fallback query for the comparables)."""
    if not hit_id:
        return None, None
    with session() as db:
        h = db.get(Hit, hit_id)
        agent = db.get(SearchAgent, h.agent_id) if h else None
    return h, (url_param(agent.source_url, "keyword") if agent else None)


async def _analysis(ad_id: str, q: str | None, fallback: str | None = None,
                    use_claude: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """(result, error). A fresh cached result costs no request; otherwise ad detail (cached) + analysis."""
    res = deals.cached(ad_id, q)
    if res is not None and not use_claude:
        return res, None
    try:
        d = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        return res, str(e)
    if d is None:
        return None, t("This ad is no longer online (sold, expired or deleted).")
    return await deals.analyze(d, query=q, use_claude=use_claude, fallback_query=fallback), None


@router.get("/hits/ad/{ad_id}/deal", response_class=HTMLResponse)
async def hit_ad_deal(request: Request, ad_id: str, hit: int | None = None, q: str | None = None):
    """Price section of the ad viewer, fetched after the ad itself so opening stays fast."""
    if not ad_id.isdigit():
        raise HTTPException(400)
    h, fallback = _hit(hit)
    res, error = await _analysis(ad_id, q, fallback)
    return _templates().TemplateResponse(request, "_deal.html", {"res": res, "error": error, "compact": True, "h": h})


@router.post("/deals/ad/{ad_id}/claude", response_class=HTMLResponse)
async def deal_claude(request: Request, ad_id: str, q: str = Form(""), hit: str = Form("")):
    """Claude's verdict – only ever on this explicit click; cached with the analysis."""
    if not ad_id.isdigit():
        raise HTTPException(400)
    _, fallback = _hit(int(hit) if hit.isdigit() else None)
    res, error = await _analysis(ad_id, q or None, fallback, use_claude=True)
    if res is not None:
        deals.remember(res)
    return _templates().TemplateResponse(request, "_deal_claude.html", {"res": res, "error": error, "q": q,
                                                                        "hit": hit})


def _search_ctx(search: int | None = None, msg: str | None = None, oob: bool = False) -> dict[str, Any]:
    """Template context of the product search part of the page. `msg` is a short code from our own redirects;
    unknown values show nothing (no attacker-chosen banner text via a crafted link)."""
    run = product_search.running()
    shown = product_search.get(search) if search else run
    notice = {"busy": t("A product search is still running – wait for it to finish."),
              "empty": t("Describe what you are looking for.")}.get(msg or "")
    return {"ps": shown, "ps_running": run, "ps_recent": product_search.recent(), "ps_msg": notice, "ps_oob": oob,
            "ps_depths": product_search.DEPTHS, "ps_claude_calls": product_search.CLAUDE_CALLS,
            "ps_is_running": product_search.is_running}


@router.get("/search")
async def search_alias():
    return RedirectResponse("/deals", status_code=307)


@router.post("/deals/search")
async def product_search_start(description: str = Form(""), depth: str = Form("normal"), unusual: str = Form(""),
                               categories: str = Form(""), sort: str = Form("relevance")):
    if not description.strip():
        return RedirectResponse("/deals?msg=empty", status_code=303)
    settings = product_search.make_settings(depth, bool(unusual), bool(categories), sort)
    try:
        sid = product_search.start(description, settings)
    except product_search.Busy:
        run = product_search.running()
        return RedirectResponse(f"/deals?search={run.id if run else ''}&msg=busy", status_code=303)
    return RedirectResponse(f"/deals?search={sid}#ps-result", status_code=303)


@router.post("/deals/search/{search_id}/rerun")
async def product_search_rerun(search_id: int):
    try:
        product_search.rerun(search_id)
    except KeyError as e:
        raise HTTPException(404) from e
    except product_search.Busy:
        return RedirectResponse(f"/deals?search={search_id}&msg=busy", status_code=303)
    return RedirectResponse(f"/deals?search={search_id}#ps-result", status_code=303)


@router.get("/deals/search/{search_id}", response_class=HTMLResponse)
async def product_search_status(request: Request, search_id: int):
    """The result block of one search; polled by htmx while the search runs."""
    if product_search.get(search_id) is None:
        raise HTTPException(404)
    return _templates().TemplateResponse(request, "_product_search.html", _search_ctx(search_id, oob=True))


@router.get("/deals", response_class=HTMLResponse)
async def deals_page(request: Request, q: str = "", search: int | None = None, msg: str | None = None):
    from app.main import render

    q = q.strip()
    ad_id = deals.parse_ad_ref(q)
    if ad_id:
        return RedirectResponse(f"/deals/ad/{ad_id}", status_code=303)
    results, error, spread = None, None, None
    if q and re.search(r"(^https?://|^www\.|willhaben\.at)", q, re.I):    # a link, but not to an ad
        error = t("This link is not a willhaben ad.")
    elif q:
        try:
            data = await deals.search_ads(q)
        except WillhabenError as e:
            error = str(e)
        else:
            ads = [a for a in data["ads"] if not deals._wanted(a.get("title"))]
            spread = deals.stats([a["price"] for a in ads if a.get("price")])
            results = []
            for a in ads:
                pos = deals.position(a.get("price"), spread, None) if spread and spread["count"] >= deals.MIN_COMPARABLES else None
                results.append({**a, "level": (pos or {}).get("level")})
    return render(request, "deals.html", q=q, results=results, spread=spread, error=error, recent=deals.recent(),
                  res=None, quote=quote, **_search_ctx(search, msg))


@router.get("/deals/ad/{ad_id}", response_class=HTMLResponse)
async def deals_ad(request: Request, ad_id: str, q: str = ""):
    from app.main import render

    if not ad_id.isdigit():
        raise HTTPException(400)
    res, error = await _analysis(ad_id, q.strip() or None)
    if res is not None:
        deals.remember(res)
    return render(request, "deals.html", q=q.strip(), results=None, spread=None, error=error,
                  recent=deals.recent(), res=res, quote=quote, **_search_ctx())
