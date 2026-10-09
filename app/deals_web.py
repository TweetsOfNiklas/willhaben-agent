"""Preischeck routes: the /deals tab and the lazily loaded price section of the ad viewer.

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
from app.services import deals
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


def _cat(cat: str | None) -> str | None:
    """A category the user picked: only willhaben marketplace result lists."""
    cat = (cat or "").strip()
    return cat if cat.startswith("https://www.willhaben.at/iad/kaufen-und-verkaufen/") else None


async def _analysis(ad_id: str, q: str | None, fallback: str | None = None,
                    use_claude: bool = False, cat: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(result, error). A fresh cached result costs no request; otherwise ad detail (cached) + analysis."""
    res = deals.cached(ad_id, q, cat)
    if res is not None and not use_claude:
        return res, None
    try:
        d = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        return res, str(e)
    if d is None:
        return None, t("This ad is no longer online (sold, expired or deleted).")
    return await deals.analyze(d, query=q, use_claude=use_claude, fallback_query=fallback, category_url=cat), None


@router.get("/hits/ad/{ad_id}/deal", response_class=HTMLResponse)
async def hit_ad_deal(request: Request, ad_id: str, hit: int | None = None, q: str | None = None):
    """Price section of the ad viewer, fetched after the ad itself so opening stays fast."""
    if not ad_id.isdigit():
        raise HTTPException(400)
    h, fallback = _hit(hit)
    res, error = await _analysis(ad_id, q, fallback)
    return _templates().TemplateResponse(request, "_deal.html", {"res": res, "error": error, "compact": True, "h": h})


@router.post("/deals/ad/{ad_id}/claude", response_class=HTMLResponse)
async def deal_claude(request: Request, ad_id: str, q: str = Form(""), hit: str = Form(""), cat: str = Form("")):
    """Claude's verdict – only ever on this explicit click; cached with the analysis."""
    if not ad_id.isdigit():
        raise HTTPException(400)
    _, fallback = _hit(int(hit) if hit.isdigit() else None)
    res, error = await _analysis(ad_id, q or None, fallback, use_claude=True, cat=_cat(cat))
    if res is not None:
        deals.remember(res)
    return _templates().TemplateResponse(request, "_deal_claude.html", {"res": res, "error": error, "q": q,
                                                                        "hit": hit})


@router.get("/deals", response_class=HTMLResponse)
async def deals_page(request: Request, q: str = ""):
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
                  res=None, quote=quote)


@router.get("/deals/ad/{ad_id}", response_class=HTMLResponse)
async def deals_ad(request: Request, ad_id: str, q: str = "", cat: str = ""):
    from app.main import render

    if not ad_id.isdigit():
        raise HTTPException(400)
    res, error = await _analysis(ad_id, q.strip() or None, cat=_cat(cat))
    choices = None
    if res is not None:
        deals.remember(res)
        choices = await deals.category_choices(res.get("query") or "", _cat(cat))   # willhaben's category navigator
    return render(request, "deals.html", q=q.strip(), results=None, spread=None, error=error,
                  recent=deals.recent(), res=res, quote=quote, choices=choices)
