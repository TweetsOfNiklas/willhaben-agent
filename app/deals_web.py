"""Produktsuche routes (formerly Preischeck): the /deals tab with the Claude product search and the single-ad price
check, plus the lazily loaded price section of the ad viewer.

Kept out of main.py (one include there); `render`/`templates` are imported from app.main at call time.
"""
from __future__ import annotations

import itertools
import logging
import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sqlmodel import select

from app import platforms, scheduler
from app.db import Hit, SearchAgent, session
from app.i18n import t
from app.platforms import AdDetail, InvalidSearchUrl, PlatformError, UnknownPlatform, condition
from app.services import deals, jobs, price_bar, price_zones, product_search, screening, search_agents
from app.willhaben.parse import url_param
from app.willhaben.public import WillhabenError

router = APIRouter()
log = logging.getLogger(__name__)


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    templates.env.globals.update(deal_level_label=deals.level_label, deal_explain=deals.explain,
                                 plan_query_url=plan_query_url, condition_text=condition.shown,
                                 condition_tone=condition.tone, deal_rerate=deals.rerate, deal_bar=price_bar.layout,
                                 deal_card_position=deals.card_position, deal_zone_span=deals.zone_span,
                                 share_pct=price_zones.share, hit_level=deals.hit_level,
                                 price_zone_config=price_zones.config, price_zone_view=price_zone_view,
                                 price_zone_error=price_zones.error_text)
    app.include_router(router)


def price_zone_view() -> dict[str, Any]:
    """What settings/prices.html and its script need: the zones, the defaults, the label names and texts, and the
    ends of the bar's scale (deals.position's axis)."""
    zones = price_zones.config()
    return {"used": list(zones["used"]), "new": list(zones["new"]),
            "defaults": {g: list(v) for g, v in price_zones.DEFAULTS.items()}, "names": list(deals.LEVEL_NAMES),
            "labels": {n: deals.level_label(n) for n in deals.LEVEL_NAMES},
            "t": {"below": t("below {n} %"), "from": t("from {n} %")}, "axis": [deals.AXIS_MIN, deals.AXIS_MAX]}


@router.post("/settings/prices")
async def settings_prices(request: Request):
    """The price zones (Settings → Search & prices → Price rating): saved only when both groups are valid; a refusal
    redirects with an error code, never with text from the request. reset=1 sets both groups back to the defaults,
    whatever the fields hold."""
    form = await request.form()
    if form.get("reset"):
        price_zones.reset()
        return RedirectResponse(f"/settings/prices?msg={quote(t('Default values restored'), safe='')}", status_code=303)
    try:
        price_zones.save({g: form.getlist(g) for g in price_zones.GROUPS})
    except price_zones.ZoneError as e:
        return RedirectResponse(f"/settings/prices?err={e.code}", status_code=303)
    return RedirectResponse(f"/settings/prices?msg={quote(t('Price zones saved'), safe='')}", status_code=303)


def plan_query_url(q: dict[str, Any], settings: dict[str, Any] | None = None) -> str:
    """The link of one planned search: its own URL once it ran, before that the search as run_queries will send it
    with the search's settings (on its site, else the first site the run uses; the search's sort)."""
    if q.get("url"):
        return q["url"]
    try:
        p = platforms.get(q["platform"]) if q.get("platform") else product_search.run_sites(settings)[0]
        return product_search.query_spec(p, q, settings).url
    except Exception:  # noqa: BLE001 - a site that is gone: no link is better than a wrong one
        return ""


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


def _known(p: platforms.Platform, ad_id: str, hit: Hit | None) -> AdDetail | None:
    """What the hit or the list that showed the ad knows (product search, direct search)."""
    if hit is not None:
        return AdDetail.from_hit(hit)
    r = deals.shown_result(p.id, ad_id)
    return AdDetail.from_result(r) if r else None


async def _analysis(platform_id: str, ad_id: str, q: str | None, fallback: str | None = None,
                    use_claude: bool = False, cat: str | None = None, hit: Hit | None = None,
                    find_geizhals: bool = False, own: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """(result, error). A fresh cached result costs no request; otherwise ad detail (cached) + analysis. Sites
    without an ad-detail call, and a site that fails, use what the hit or the list that showed the ad knows (as the
    ad viewer does); the comparables are willhaben listings either way. `own`: the user typed q to compare with
    (deals.analyze own_query); a q from a list's "Check" link only changes the search."""
    p = _platform(platform_id)
    res = deals.cached(p.ref(ad_id), q, cat, own)
    # a condition the hit learned since (the viewer wrote the ad page's): analyze() re-rates, its searches are cached
    stale = res is not None and hit is not None and (
        bool(hit.item_condition) and (res.get("ad") or {}).get("item_condition") != hit.item_condition
        or bool((hit.geizhals or {}).get("price_from")) and not res.get("geizhals"))   # a product the hit knows
    if res is not None and not (use_claude or find_geizhals or stale):
        return res, None
    if p.can("detail"):
        try:
            d = await p.detail(ad_id, url=hit.url if hit else None)
        except PlatformError as e:
            if (d := _known(p, ad_id, hit)) is None:
                return res, str(e)
    elif (d := _known(p, ad_id, hit)) is None:
        return None, t("No details for this ad; open it on {platform}.", platform=p.name)
    if d is None:
        return None, t("This ad is no longer online (sold, expired or deleted).")
    if d.item_condition is None and hit is not None and hit.item_condition:    # a page without one: what the list said
        d.item_condition, d.item_condition_label = hit.item_condition, hit.item_condition_label
    return await deals.analyze(d, query=q, own_query=own, use_claude=use_claude, find_geizhals=find_geizhals,
                               fallback_query=fallback, category_url=cat,
                               known_geizhals=hit.geizhals if hit is not None else None), None


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
async def deal_claude(request: Request, ad_id: str, q: str = Form(""), hit: str = Form(""), cat: str = Form(""),
                      own: str = Form("")):
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await deal_claude_platform(request, "willhaben", ad_id, q, hit, cat, own)


@router.post("/deals/ad/{platform}/{ad_id}/claude", response_class=HTMLResponse)
async def deal_claude_platform(request: Request, platform: str, ad_id: str, q: str = Form(""), hit: str = Form(""),
                               cat: str = Form(""), own: str = Form("")):
    """Claude's verdict – only ever on this explicit click; cached with the analysis. Shown among the background
    jobs while Claude thinks (quietly: this page shows the answer)."""
    h, fallback = _hit(int(hit) if hit.isdigit() else None)
    async with jobs.track("deal-claude", t("Claude rates the price: „{title}“",
                                           title=_title(platform, ad_id, q, cat, h, own == "1")),
                          link=_page(platform, ad_id), quiet=True) as job:
        res, error = await _analysis(platform, ad_id, q or None, fallback, use_claude=True, cat=_cat(cat, platform),
                                     hit=h, own=own == "1")
        cl = (res or {}).get("claude") or {}
        if error or cl.get("error"):
            job.status, job.result = "failed", error or cl["error"]
        else:
            job.result = screening.deal_label(cl.get("deal")) if cl.get("deal") else ""
    if res is not None:
        deals.remember(res)
    return _templates().TemplateResponse(request, "_deal_claude.html", {"res": res, "error": error, "q": q,
                                                                        "hit": hit})


@router.post("/deals/ad/{platform}/{ad_id}/geizhals", response_class=HTMLResponse)
async def deal_geizhals_platform(request: Request, platform: str, ad_id: str, q: str = Form(""), hit: str = Form(""),
                                 cat: str = Form(""), compact: str = Form(""), own: str = Form("")):
    """Claude looks for the product on Geizhals – only on this explicit click, when the automatic lookup found none.
    Answers with the whole price section: a new price can place the ad on the bar. A hit the viewer was opened from
    gets the product too (the hit list shows the comparison)."""
    h, fallback = _hit(int(hit) if hit.isdigit() else None)
    async with jobs.track("geizhals-claude", t("Claude searches Geizhals: „{title}“",
                                               title=_title(platform, ad_id, q, cat, h, own == "1")),
                          link=_page(platform, ad_id), quiet=True) as job:
        res, error = await _analysis(platform, ad_id, q or None, fallback, cat=_cat(cat, platform), hit=h,
                                     find_geizhals=True, own=own == "1")
        r = res or {}
        if error or r.get("geizhals_error"):
            job.status, job.result = "failed", error or r["geizhals_error"]
        else:
            job.result = (r.get("geizhals") or {}).get("name") or r.get("geizhals_none") or ""
    if res is not None:
        deals.remember(res)
        if h is not None and (res.get("geizhals") or {}).get("by") == "claude":
            search_agents.remember_geizhals(h.id, res["geizhals"])
    return _templates().TemplateResponse(request, "_deal.html", {"res": res, "error": error, "compact": bool(compact),
                                                                 "h": h})


def _title(platform: str, ad_id: str, q: str, cat: str, h: Hit | None, own: bool = False) -> str:
    """The ad's title for a job label, without a request: the hit's, or the one of the analysis on screen."""
    res = deals.cached(_platform(platform).ref(ad_id), q or None, _cat(cat, platform), own)
    title = (h.heading if h else None) or ((res or {}).get("ad") or {}).get("title") or ad_id
    return title if len(title) <= 60 else title[:59] + "…"


def _page(platform: str, ad_id: str) -> str:
    return f"/deals/ad/{ad_id}" if platform == "willhaben" else f"/deals/ad/{platform}/{ad_id}"


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
            "ps_planned": product_search.planned, "ps_by_term": product_search.by_term, **_agent_ctx(shown)}


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
    taken = {pid for a in agents for pid in search_agents.agent_platforms(a)}
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
                               ask: str = Form(""), sites: list[str] = Form([]), cond_new: str = Form(""),
                               cond_used: str = Form(""), cond_shown: str = Form("")):
    if not description.strip():
        return RedirectResponse("/deals?msg=empty", status_code=303)
    # an older form without the boxes searches both
    condition = product_search.condition_wanted(bool(cond_new), bool(cond_used)) if cond_shown else "any"
    settings = product_search.make_settings(depth, bool(unusual), bool(categories), sort, bool(photos), bool(ask),
                                            sites=sites, condition=condition)
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


@router.post("/deals/search/{search_id}/more")
async def product_search_more(search_id: int):
    """The next offers of a finished search: Claude ranks them, the best are added below (in the background)."""
    try:
        product_search.load_more(search_id)
    except KeyError as e:
        raise HTTPException(404) from e
    except product_search.Busy:
        return RedirectResponse(f"/deals?search={search_id}&msg=busy", status_code=303)
    return RedirectResponse(f"/deals?search={search_id}#ps-more", status_code=303)


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
    """One search agent that keeps running this search's keyword searches on every chosen site (no further Claude
    planning call)."""
    minutes = int(interval_min) if interval_min.strip().isdigit() else None
    row = product_search.get(search_id)
    chosen = [s for s in dict.fromkeys(sites) if s] or ((row.settings or {}).get("platforms") if row else None) or ["willhaben"]
    chosen = [s for s in chosen if platforms.enabled(s)] or ["willhaben"]
    try:
        agent = search_agents.create_agent_from_search(search_id, name.strip() or None, minutes * 60 if minutes else None,
                                                       screen=bool(screen_on), sites=chosen)
    except KeyError as e:
        raise HTTPException(404) from e
    except (ValueError, UnknownPlatform):
        return RedirectResponse(f"/deals?search={search_id}&msg=noplan#ps-result", status_code=303)
    scheduler.schedule_agent(agent)
    return RedirectResponse("/agents", status_code=303)


@router.get("/deals/search/{search_id}", response_class=HTMLResponse)
async def product_search_status(request: Request, search_id: int):
    """The result block of one search; polled by htmx while the search runs."""
    if product_search.get(search_id) is None:
        raise HTTPException(404)
    return _templates().TemplateResponse(request, "_product_search.html", _search_ctx(search_id, oob=True))


def _direct_ctx(sites: list[str] | None = None) -> dict[str, Any]:
    """The direct search's site choice: every switched-on site with a keyword search; `sites` = the ticked ones
    (none ticked or none given: all of them)."""
    choices = [p for p in platforms.all() if p.can("keyword_search")]
    ticked = [p for p in choices if p.id in set(sites or [])] or choices
    return {"direct_sites": choices, "direct_chosen": [p.id for p in ticked]}


async def _direct_search(q: str, sites: list[platforms.Platform]) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    """A search text on every ticked site at once (willhaben through the price check's cached search, the others
    sorted by relevance): the ads interleaved site by site, error lines per site, the number found per site."""
    import asyncio

    async def one(p: platforms.Platform) -> tuple[list[dict[str, Any]], int]:
        if p.id == "willhaben":
            data = await deals.search_ads(q)
            return data["ads"], int(data.get("rows_found") or 0)
        page = await p.search(p.keyword_search(q, sort="relevance"), rows=deals.SEARCH_ROWS, cache_s=600)
        return [a.as_dict() for a in page.ads], int(page.rows_found or 0)

    answers = await asyncio.gather(*(one(p) for p in sites), return_exceptions=True)
    per_site, errors, counts = [], [], {}
    for p, ans in zip(sites, answers, strict=True):
        if isinstance(ans, BaseException):
            if not isinstance(ans, (WillhabenError, InvalidSearchUrl)):
                log.warning("direct search on %s failed: %r", p.id, ans)
            errors.append(f"{p.name}: {ans}")
            continue
        ads, counts[p.id] = ans
        per_site.append([a for a in ads if not deals._wanted(a.get("title"))])
    merged = [a for row in itertools.zip_longest(*per_site) for a in row if a is not None]
    deals.remember_shown(merged)        # the viewer shows them for sites without ad details
    return merged, errors, counts


def _prefill(search_id: int | None) -> dict[str, Any] | None:
    """"New search" on a past search: its text and options fill the form; nothing starts until the user clicks."""
    row = product_search.get(search_id) if search_id else None
    if row is None:
        return None
    st = {"photos": True, "ask": True, **(row.settings or {})}       # older searches do not store these two
    return {"id": row.id, "description": row.description, "settings": st}


@router.get("/deals", response_class=HTMLResponse)
async def deals_page(request: Request, q: str = "", search: int | None = None, msg: str | None = None,
                     from_: int | None = Query(None, alias="from"), sites: list[str] = Query([])):
    """Product search ("AI search": Claude plans and ranks) and the direct search (one ad's price, or the ads of the
    ticked sites for a text). `from` = a past search whose text and options fill the form."""
    from app.main import render

    q = q.strip()
    ref = deals.parse_ad_ref(q)
    if ref:
        platform, ad_id = ref
        return RedirectResponse(f"/deals/ad/{ad_id}" if platform == "willhaben" else f"/deals/ad/{platform}/{ad_id}",
                                status_code=303)
    results, error, spread = None, None, None
    direct = _direct_ctx(sites)
    site_errors: list[str] = []
    site_counts: dict[str, int] = {}
    if q and (re.search(r"(^https?://|^www\.)", q, re.I) or deals.is_site_link(q)):    # a link, but not to an ad
        error = t("This link is not an ad on a supported site.")
    elif q:
        ticked = [p for p in direct["direct_sites"] if p.id in direct["direct_chosen"]]
        ads, site_errors, site_counts = await _direct_search(q, ticked)
        if site_errors and not site_counts:
            error = "; ".join(site_errors)
        else:
            spread = deals.stats([a["price"] for a in ads if a.get("price")])
            results = []
            for a in ads:             # among the other results: no new price in a list (comparables only)
                pos = (deals.position(a.get("price"), spread, None, a.get("item_condition"))
                       if spread and spread["count"] >= deals.MIN_COMPARABLES else None)
                results.append({**a, "level": (pos or {}).get("level")})
    prefill = _prefill(from_)
    return render(request, "deals.html", q=q, results=results, spread=spread, error=error, recent=deals.recent(),
                  res=None, quote=quote, ps_prefill=prefill, site_errors=site_errors if results is not None else [],
                  site_counts=site_counts, **direct, **_search_ctx(search or (prefill or {}).get("id"), msg))


@router.get("/deals/ad/{ad_id}/result", response_class=HTMLResponse)
async def deals_ad_result(request: Request, ad_id: str, q: str = "", cat: str = "", own: str = ""):
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await deals_ad_result_platform(request, "willhaben", ad_id, q, cat, own=own)


@router.get("/deals/ad/{platform}/{ad_id}/result", response_class=HTMLResponse)
async def deals_ad_result_platform(request: Request, platform: str, ad_id: str, q: str = "", cat: str = "",
                                   hit: int | None = None, own: str = ""):
    """The price check of one ad as a fragment (_deal_result.html), loaded by /deals/ad/<id>."""
    from app.main import templates

    h, fallback = _hit(hit)
    res, error = await _analysis(platform, ad_id, q.strip() or None, fallback, cat=_cat(cat, platform), hit=h,
                                 own=own == "1")
    choices = None
    if res is not None:
        deals.remember(res)
        if platform == "willhaben":        # willhaben's category navigator; other sites compare by keyword
            choices = await deals.category_choices(res.get("query") or "", _cat(cat))
    return templates.TemplateResponse(request, "_deal_result.html", {"res": res, "error": error, "choices": choices,
                                                                     "q": q.strip(), "quote": quote})


@router.get("/deals/ad/{ad_id}", response_class=HTMLResponse)
async def deals_ad(request: Request, ad_id: str, q: str = "", cat: str = "", own: str = "", was: str | None = None):
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await deals_ad_platform(request, "willhaben", ad_id, q, cat, own=own, was=was)


@router.get("/deals/ad/{platform}/{ad_id}", response_class=HTMLResponse)
async def deals_ad_platform(request: Request, platform: str, ad_id: str, q: str = "", cat: str = "",
                            hit: int | None = None, own: str = "", was: str | None = None):
    """The page of one ad's price check. q from a list's "Check" link only changes the search; "Compare again"
    sends the term with `was` (the term it showed): a changed term is the user's own (own=1, deals.analyze
    own_query), an unchanged one stays what it was."""
    from app.main import render

    p = _platform(platform)
    own_q = own == "1" or (was is not None and " ".join(q.split()).lower() != " ".join(was.split()).lower())
    # the page answers at once; the analysis (willhaben requests) loads into it: /deals/ad/<id>/result
    return render(request, "deals.html", q=q.strip(), results=None, spread=None, error=None, recent=deals.recent(),
                  res=None, pending_ad=ad_id, pending_platform=p.id, pending_hit=hit, pending_cat=cat,
                  pending_own=own_q and bool(q.strip()), quote=quote, **_direct_ctx(), **_search_ctx())
