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

from sqlmodel import select

from app import platforms, scheduler
from app.db import Hit, SearchAgent, session
from app.i18n import t
from app.platforms import AdDetail, PlatformError, UnknownPlatform
from app.services import deals, product_search, search_agents
from app.willhaben.parse import url_param
from app.willhaben.public import WillhabenError

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
    if agent is None:
        return h, None
    return h, (agent.spec or {}).get("keyword") or url_param(agent.source_url, "keyword")


def _cat(cat: str | None, platform: str = "willhaben") -> str | None:
    """A category the user picked: only willhaben marketplace result lists, only for willhaben ads."""
    cat = (cat or "").strip()
    return cat if platform == "willhaben" and cat.startswith("https://www.willhaben.at/iad/kaufen-und-verkaufen/") else None


def _platform(platform_id: str) -> platforms.Platform:
    try:
        return platforms.get(platform_id)
    except UnknownPlatform as e:
        raise HTTPException(404) from e


async def _analysis(platform_id: str, ad_id: str, q: str | None, fallback: str | None = None,
                    use_claude: bool = False, cat: str | None = None,
                    hit: Hit | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(result, error). A fresh cached result costs no request; otherwise ad detail (cached) + analysis. Sites
    without an ad-detail call use what the hit knows; the comparables are willhaben listings either way."""
    p = _platform(platform_id)
    res = deals.cached(p.ref(ad_id), q, cat)
    if res is not None and not use_claude:
        return res, None
    if p.can("detail"):
        try:
            d = await p.detail(ad_id, url=hit.url if hit else None)
        except PlatformError as e:
            return res, str(e)
    elif hit is not None:
        d = AdDetail.from_hit(hit)
    else:
        return None, t("No details for this ad; open it on {platform}.", platform=p.name)
    if d is None:
        return None, t("This ad is no longer online (sold, expired or deleted).")
    return await deals.analyze(d, query=q, use_claude=use_claude, fallback_query=fallback, category_url=cat), None


# Route order matters: Starlette takes the first path that matches, so the 3-segment legacy routes
# (/hits/ad/<id>/deal, /deals/ad/<id>/result|claude) come before the 2-segment platform routes.
@router.get("/hits/ad/{ad_id}/deal", response_class=HTMLResponse)
async def hit_ad_deal(request: Request, ad_id: str, hit: int | None = None, q: str | None = None):
    """Price section of the ad viewer (willhaben ad), fetched after the ad itself so opening stays fast."""
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await hit_ad_deal_platform(request, "willhaben", ad_id, hit, q)


@router.get("/hits/ad/{platform}/{ad_id}/deal", response_class=HTMLResponse)
async def hit_ad_deal_platform(request: Request, platform: str, ad_id: str, hit: int | None = None,
                               q: str | None = None):
    h, fallback = _hit(hit)
    res, error = await _analysis(platform, ad_id, q, fallback, hit=h)
    return _templates().TemplateResponse(request, "_deal.html", {"res": res, "error": error, "compact": True, "h": h})


@router.get("/hits/ad/{platform}/{ad_id}", response_class=HTMLResponse)
async def hit_ad_detail_platform(request: Request, platform: str, ad_id: str, hit: int | None = None):
    """Ad detail of any platform as a fragment for the in-dashboard viewer (/hits/ad/<id> = willhaben)."""
    from app.main import ad_fragment

    return await ad_fragment(request, _platform(platform), ad_id, hit)


@router.post("/deals/ad/{ad_id}/claude", response_class=HTMLResponse)
async def deal_claude(request: Request, ad_id: str, q: str = Form(""), hit: str = Form(""), cat: str = Form("")):
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await deal_claude_platform(request, "willhaben", ad_id, q, hit, cat)


@router.post("/deals/ad/{platform}/{ad_id}/claude", response_class=HTMLResponse)
async def deal_claude_platform(request: Request, platform: str, ad_id: str, q: str = Form(""), hit: str = Form(""),
                               cat: str = Form("")):
    """Claude's verdict – only ever on this explicit click; cached with the analysis."""
    h, fallback = _hit(int(hit) if hit.isdigit() else None)
    res, error = await _analysis(platform, ad_id, q or None, fallback, use_claude=True, cat=_cat(cat, platform), hit=h)
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
              "empty": t("Describe what you are looking for."),
              "noplan": t("This search has no planned willhaben searches yet.")}.get(msg or "")
    return {"ps": shown, "ps_running": run, "ps_recent": product_search.recent(), "ps_msg": notice, "ps_oob": oob,
            "ps_depths": product_search.DEPTHS, "ps_claude_calls": product_search.CLAUDE_CALLS,
            "ps_is_running": product_search.is_running, "ps_answer_pairs": product_search.answer_pairs,
            **_agent_ctx(shown)}


def _sites_off() -> list[platforms.Platform]:
    """Sites the product search could use that are switched off: the form names them, with a link to switch on."""
    return [p for p in platforms.all(enabled_only=False) if p.can("keyword_search") and not platforms.enabled(p.id)]


def _agent_ctx(shown: Any) -> dict[str, Any]:
    """"Search agent from this search": the existing agents (one per site), or the form's defaults."""
    sites = [p for p in product_search.search_platforms()]
    if shown is None or shown.id is None:
        return {"ps_agent": None, "ps_agents": [], "ps_agent_n": 0, "ps_agent_interval_min": 0, "ps_agent_name": "",
                "ps_sites": sites, "ps_agent_sites": [], "ps_sites_off": _sites_off()}
    with session() as db:
        agents = list(db.exec(select(SearchAgent).where(SearchAgent.search_id == shown.id).order_by(SearchAgent.id)))
    n = len((shown.plan or {}).get("queries") or [])
    used = [s for s in ((shown.settings or {}).get("platforms") or ["willhaben"])]
    taken = {a.platform for a in agents}
    agent_sites = [p for p in platforms.all() if p.can("keyword_search") and p.id in used and p.id not in taken]
    return {"ps_agent": agents[0] if agents else None, "ps_agents": agents, "ps_agent_n": n,
            "ps_agent_interval_min": search_agents.default_interval_s(n) // 60,
            "ps_agent_name": search_agents.agent_name(shown), "ps_sites": sites, "ps_agent_sites": agent_sites,
            "ps_sites_off": _sites_off()}


@router.get("/search")
async def search_alias():
    return RedirectResponse("/deals", status_code=307)


@router.post("/deals/search")
async def product_search_start(description: str = Form(""), depth: str = Form("normal"), unusual: str = Form(""),
                               categories: str = Form(""), sort: str = Form("relevance"), photos: str = Form(""),
                               ask: str = Form(""), sites: list[str] = Form([])):
    if not description.strip():
        return RedirectResponse("/deals?msg=empty", status_code=303)
    settings = product_search.make_settings(depth, bool(unusual), bool(categories), sort, bool(photos), bool(ask),
                                            sites=sites)
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


@router.post("/deals/search/{search_id}/answers")
async def product_search_answers(request: Request, search_id: int):
    """Answers to the plan's questions (a_<id> = a chosen option, o_<id> = free text, which wins) or `skip`; the
    search then goes on with the ranking and the photo check (no new willhaben request, no extra Claude call)."""
    row = product_search.get(search_id)
    if row is None:
        raise HTTPException(404)
    form = await request.form()
    answers: dict[str, str] = {}
    if not form.get("skip"):
        for q in (row.plan or {}).get("questions") or []:
            qid = q.get("id") if isinstance(q, dict) else None
            if not qid:
                continue
            other, chosen = form.get(f"o_{qid}"), form.get(f"a_{qid}")
            other = other.strip() if isinstance(other, str) else ""
            chosen = chosen.strip() if isinstance(chosen, str) else ""
            if other or chosen:
                answers[qid] = other or chosen
    try:
        product_search.continue_run(search_id, answers)
    except KeyError as e:
        raise HTTPException(404) from e
    except product_search.Busy:
        return RedirectResponse(f"/deals?search={search_id}&msg=busy", status_code=303)
    return RedirectResponse(f"/deals?search={search_id}#ps-result", status_code=303)


@router.post("/deals/search/{search_id}/agent")
async def product_search_agent(search_id: int, name: str = Form(""), interval_min: str = Form(""),
                               screen_on: str | None = Form(None), sites: list[str] = Form([])):
    """Search agents that keep running this search's keyword searches, one per chosen site (no further Claude
    planning call)."""
    minutes = int(interval_min) if interval_min.strip().isdigit() else None
    row = product_search.get(search_id)
    chosen = [s for s in dict.fromkeys(sites) if s] or ((row.settings or {}).get("platforms") if row else None) or ["willhaben"]
    created = []
    for pid in chosen:
        label = name.strip() or None
        if label and len(chosen) > 1 and pid != "willhaben":
            label = f"{label} ({_platform(pid).name})"
        try:
            created.append(search_agents.create_agent_from_search(search_id, label, minutes * 60 if minutes else None,
                                                                  screen=bool(screen_on), platform=pid))
        except KeyError as e:
            raise HTTPException(404) from e
        except (ValueError, UnknownPlatform):
            if not created and pid == chosen[-1]:
                return RedirectResponse(f"/deals?search={search_id}&msg=noplan#ps-result", status_code=303)
    for agent in created:
        scheduler.schedule_agent(agent)
    return RedirectResponse("/agents", status_code=303)


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
    ref = deals.parse_ad_ref(q)
    if ref:
        platform, ad_id = ref
        return RedirectResponse(f"/deals/ad/{ad_id}" if platform == "willhaben" else f"/deals/ad/{platform}/{ad_id}",
                                status_code=303)
    results, error, spread = None, None, None
    if q and (re.search(r"(^https?://|^www\.)", q, re.I) or deals.is_site_link(q)):    # a link, but not to an ad
        error = t("This link is not an ad on a supported site.")
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


@router.get("/deals/ad/{ad_id}/result", response_class=HTMLResponse)
async def deals_ad_result(request: Request, ad_id: str, q: str = "", cat: str = ""):
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await deals_ad_result_platform(request, "willhaben", ad_id, q, cat)


@router.get("/deals/ad/{platform}/{ad_id}/result", response_class=HTMLResponse)
async def deals_ad_result_platform(request: Request, platform: str, ad_id: str, q: str = "", cat: str = "",
                                   hit: int | None = None):
    """The price check of one ad as a fragment (_deal_result.html), loaded by /deals/ad/<id>."""
    from app.main import templates

    h, fallback = _hit(hit)
    res, error = await _analysis(platform, ad_id, q.strip() or None, fallback, cat=_cat(cat, platform), hit=h)
    choices = None
    if res is not None:
        deals.remember(res)
        if platform == "willhaben":        # willhaben's category navigator; other sites compare by keyword
            choices = await deals.category_choices(res.get("query") or "", _cat(cat))
    return templates.TemplateResponse(request, "_deal_result.html", {"res": res, "error": error, "choices": choices,
                                                                     "q": q.strip(), "quote": quote})


@router.get("/deals/ad/{ad_id}", response_class=HTMLResponse)
async def deals_ad(request: Request, ad_id: str, q: str = "", cat: str = ""):
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await deals_ad_platform(request, "willhaben", ad_id, q, cat)


@router.get("/deals/ad/{platform}/{ad_id}", response_class=HTMLResponse)
async def deals_ad_platform(request: Request, platform: str, ad_id: str, q: str = "", cat: str = "",
                            hit: int | None = None):
    from app.main import render

    p = _platform(platform)
    # the page answers at once; the analysis (willhaben requests) loads into it: /deals/ad/<id>/result
    return render(request, "deals.html", q=q.strip(), results=None, spread=None, error=None, recent=deals.recent(),
                  res=None, pending_ad=ad_id, pending_platform=p.id, pending_hit=hit, pending_cat=cat, quote=quote,
                  **_search_ctx())
