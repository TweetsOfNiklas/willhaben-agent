"""FastAPI app: HTMX dashboard + JSON API (used by the MCP server)."""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import col, func, select
from starlette.middleware.base import BaseHTTPMiddleware

from app import AUTHOR, __version__, auth, i18n, scheduler
from app.i18n import t
from app.config import get_settings
from app.db import (
    AdBatch, AdDraft, Conversation, Draft, EventLog, Hit, MyAd, Purchase, SearchAgent, get_setting, init_db, recent_events,
    requests_per_hour, session, set_setting, utcnow,
)
from app.llm import usage as llm_usage
from app.llm.base import MODELS, PURPOSES, LLMError, get_backend, model_for
from app.llm.claude_login import claude_token, forget_token, get_login, token_source
from app.services import access_stats, ad_batch, ad_creator, chats, my_ads, screening, sold
from app.services import purchase as purchase_svc
from app.services import gcal, geizhals, ha_auth, label_fill, meetups, quiet_hours, shipping, telegram
from app.services import health, human_pace, jobs
from app.services import min_price as min_price_svc
from app.services import scam
from app.services.notify import PUSH_CHANNELS, notify
from app import backup_routes, label_buy_routes, paylivery_routes, printing_routes, purchase_routes
from app.services.search_agents import create_agent, run_agent
from app.willhaben import access_log
from app.willhaben import insert as _wh_insert
from app.willhaben.account import EndpointUnknown, SendFailed
from app.willhaben.parse import InvalidSearchUrl, to_webapi_url, url_param, with_params
from app.willhaben.public import WillhabenError, get_public, willhaben_limiter
from app.willhaben.session import NotLoggedIn, SessionUnavailable, get_session

log = logging.getLogger("willhaben_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
VIENNA = ZoneInfo("Europe/Vienna")
BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    init_db()
    ad_batch.recover()
    my_ads.recover()                               # queued jobs died with the last process: unmark what they held
    ad_creator.recover_publishing()
    chats.recover_sending()
    deals_web.product_search.recover()
    purchase_svc.recover()
    if s.browser_enabled:
        sess = get_session()
        await sess.start()
        if sess.started:
            try:
                await sess.check_login()
            except Exception as e:  # noqa: BLE001
                log.warning("initial login check failed: %s", e)
    scheduler.start()
    from app.services import ha_actions
    tasks = [asyncio.create_task(ha_actions.listen_forever())] if s.ha_listener else []  # HA push buttons
    if s.telegram_listener:                         # Telegram buttons and replies (long polling)
        tasks.append(asyncio.create_task(telegram.listen_forever()))
    sess = get_session()
    if s.chat_live and sess.started:               # new chat messages arrive without polling
        try:
            await sess.start_chat_listener(chats.on_live_event)
            tasks.append(asyncio.create_task(sess.chat_watchdog()))
        except Exception as e:  # noqa: BLE001 - polling still works
            log.warning("live chat not started: %s", e)
    app.state.listener_tasks = tasks             # Settings → Backup stops them before a restore
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    scheduler.shutdown()
    try:
        await get_session().stop()
        await get_public().aclose()
    finally:
        access_log.flush()                        # last unwritten willhaben access counts


_background: set[asyncio.Task] = set()
app = FastAPI(title="willhaben Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(backup_routes.router)   # Settings → Backup
app.include_router(label_buy_routes.router)   # Settings → Shipping → payment card, checkout
app.include_router(printing_routes.router)    # Settings → Printers, print buttons
app.include_router(paylivery_routes.router)   # PayLivery card on the chat page
app.include_router(purchase_routes.router)    # Kaufauftrag: buy dialog, order pages, order buttons, Settings → Buying
paylivery_routes.paylivery.on_stage(purchase_svc.on_paylivery_stage)      # purchase orders follow PayLivery stages
paylivery_routes.paylivery.on_action(purchase_svc.on_paylivery_action)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class DashboardAuth(BaseHTTPMiddleware):
    """Login cookie for browsers (see /login), HTTP Basic auth still accepted for scripts and the MCP server."""
    async def dispatch(self, request: Request, call_next):
        if not auth.enabled() or request.url.path in auth.PUBLIC_PATHS or request.url.path.startswith(auth.PUBLIC_PREFIXES):
            return await call_next(request)
        if auth.valid_token(request.cookies.get(auth.COOKIE)) or auth.valid_basic(request.headers.get("authorization", "")):
            return await call_next(request)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        login = f"/login?next={quote(target, safe='')}"
        if request.headers.get("hx-request"):                       # htmx fragment: reload the whole page to the login
            return Response(status_code=401, headers={"HX-Redirect": login})
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(login, status_code=303)
        return JSONResponse({"detail": "login required"}, status_code=401)   # no WWW-Authenticate: no browser popup


app.add_middleware(DashboardAuth)


@app.middleware("http")
async def ui_language(request: Request, call_next):
    pref = i18n.preference()
    code = pref if pref in i18n.LANGS else i18n.from_accept_language(request.headers.get("accept-language"))
    if pref not in i18n.LANGS and request.headers.get("accept-language") and get_setting("ui_lang_seen") != code:
        set_setting("ui_lang_seen", code)
    i18n.set_request_lang(code)
    return await call_next(request)


# ------------------------------------------------------------------ template helpers
def _local(dt: datetime | None, fmt: str = "%d.%m. %H:%M") -> str:
    if not dt:
        return "–"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VIENNA).strftime(fmt)


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "–"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (utcnow() - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    if secs < 90:
        txt = t("{n} s", n=f"{secs:.0f}")
    elif secs < 5400:
        txt = t("{n} min", n=f"{secs / 60:.0f}")
    elif secs < 172800:
        txt = t("{n} h", n=f"{secs / 3600:.0f}")
    else:
        txt = t("{n} days", n=f"{secs / 86400:.0f}")
    return t("in {t}", t=txt) if future else t("{t} ago", t=txt)


def _eur(v: float | None) -> str:
    if v is None:
        return "–"
    return f"€ {v:,.0f}".replace(",", ".") if v == int(v) else f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


templates.env.filters.update(local=_local, ago=_ago, eur=_eur)
from app.services.price_gauge import gauge as _price_gauge  # noqa: E402
from app.services.deals import level_label as _level_label  # noqa: E402

templates.env.globals.update(price_gauge=_price_gauge, deal_level_label=_level_label)
templates.env.globals.update(deal_label=screening.deal_label, screen_labels=screening.verdict_labels,
                             screen_issue_labels=screening.issue_labels, screen_hideable=screening.HIDEABLE,
                             screen_hide_set=screening.hide_set, screen_default_hide=screening.DEFAULT_HIDE)   # Claude verdict on hit cards
templates.env.globals.update(scam_warnings=scam.warnings, scam_for_viewer=scam.for_viewer)   # scam check badges
templates.env.globals.update(paylivery_card=paylivery_routes.card)   # _paylivery.html on the chat page
templates.env.globals.update(purchase_card=purchase_svc.card, purchase_enabled=purchase_routes.enabled,   # Kaufauftrag
                             purchase_settings=purchase_routes.settings_values, purchase_notice=purchase_routes.notice)
templates.env.globals.update(parcel_sizes=_wh_insert.PARCEL_SIZES, parcel_carriers=_wh_insert.PAYLIVERY_CARRIERS)  # _wh_handover.html
templates.env.globals.update(_=t, ui_lang=i18n.lang, ui_langs=i18n.LANGS, ui_lang_pref=i18n.preference,
                             app_version=__version__, app_author=AUTHOR, auth_enabled=auth.enabled, wh_limiter=willhaben_limiter,
                             wh_budget=requests_per_hour, wh_popup=access_stats.popup,
                             bg_jobs=jobs.snapshot, bg_working=jobs.working)   # background jobs (header, spinners)


def _asset_version() -> str:
    """Cache key for /static/app.css: changes whenever the stylesheet is rebuilt."""
    try:
        return str(int((BASE_DIR / "static" / "app.css").stat().st_mtime))
    except OSError:
        return __version__


templates.env.globals.update(asset_version=_asset_version)


def _inbox_seen_at() -> datetime:
    raw = get_setting("inbox_seen_at")
    try:
        return datetime.fromisoformat(raw) if raw else datetime(2000, 1, 1, tzinfo=timezone.utc)
    except ValueError:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)


def _nav_counts() -> dict[str, int]:
    """Badges in the sidebar: new hits, unread messages (hidden chats left out), purchases waiting for your payment,
    and notifications since the inbox was last opened."""
    with session() as db:
        return {"inbox": db.exec(select(func.count()).select_from(EventLog).where(EventLog.ts > _inbox_seen_at())).one(),
                "hits": db.exec(select(func.count()).select_from(Hit).where(Hit.state == "new")).one(),
                "chats": db.exec(select(func.coalesce(func.sum(Conversation.unread_count), 0))
                                 .where(col(Conversation.hidden_at).is_(None))).one(),
                "purchases": db.exec(select(func.count()).select_from(Purchase).where(Purchase.status == "pay_now")).one()}


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("session_status", get_session().status())
    if not ctx.get("embed"):
        ctx.setdefault("nav_counts", _nav_counts())
        lim = llm_usage.limit()
        if lim["limit"]:       # the Claude meter + popover in the sidebar (monthly limit from Settings → Claude)
            ctx.setdefault("claude_usage", {**lim, "by_task": llm_usage.month_by_task()})
        ctx.setdefault("inbox_items", _inbox_items())
        ctx.setdefault("inbox_unread", ctx["nav_counts"].get("inbox", 0))
        if auth.enabled():
            ctx.setdefault("current_user", get_settings().dashboard_user)
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ login
def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if not auth.enabled() or auth.valid_token(request.cookies.get(auth.COOKIE)):
        return back(auth.safe_next(next))
    return templates.TemplateResponse(request, "login.html", {"next": auth.safe_next(next), "error": None,
                                                              "user": get_settings().dashboard_user})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                       remember: str | None = Form(None), next: str = Form("/")):
    client = request.client.host if request.client else "?"
    target = auth.safe_next(next)
    if auth.locked_out(client):
        error = t("Too many failed attempts. Try again in 15 minutes.")
    elif auth.check_password(username.strip(), password):
        r = back(target)
        r.set_cookie(auth.COOKIE, auth.make_token(bool(remember)), max_age=auth.REMEMBER_S if remember else None,
                     httponly=True, samesite="lax", secure=_https(request))
        return r
    else:
        auth.record_failure(client)
        await asyncio.sleep(1)                     # slows down guessing
        error = t("Wrong user name or password.")
    return templates.TemplateResponse(request, "login.html", {"next": target, "error": error, "user": username},
                                      status_code=401)


@app.post("/logout")
async def logout():
    r = back("/login")
    r.delete_cookie(auth.COOKIE)
    return r


# ------------------------------------------------------------------ dashboard
@app.get("/healthz")
async def healthz():
    return {"ok": True, "browser": get_session().started, "logged_in": get_session().logged_in}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with session() as db:
        stats = {
            "agents": db.exec(select(func.count()).select_from(SearchAgent).where(SearchAgent.enabled)).one(),
            "new_hits": db.exec(select(func.count()).select_from(Hit).where(Hit.state == "new")).one(),
            "unread": db.exec(select(func.coalesce(func.sum(Conversation.unread_count), 0))).one(),
            "open_drafts": db.exec(select(func.count()).select_from(Draft).where(Draft.status == "draft")).one(),
            "myads": db.exec(select(func.count()).select_from(MyAd)).one(),
            "expired": db.exec(select(func.count()).select_from(MyAd).where(MyAd.status == "expired",
                                                                             col(MyAd.replaced_by).is_(None))).one(),
        }
        soon = list(db.exec(select(MyAd).where(MyAd.status == "active", col(MyAd.end_date).is_not(None))
                            .order_by(MyAd.end_date).limit(5)))
        latest = list(db.exec(select(Hit).where(Hit.state == "new").order_by(col(Hit.first_seen_at).desc()).limit(8)))
    return render(request, "index.html", stats=stats, soon=soon, latest=latest, events=recent_events(20),
                  llm_ok=bool(claude_token()), health=health.collect())


# ------------------------------------------------------------------ search agents
@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, error: str | None = None):
    with session() as db:
        agents = list(db.exec(select(SearchAgent).order_by(SearchAgent.created_at)))
        counts = dict(db.exec(select(Hit.agent_id, func.count()).where(Hit.state == "new").group_by(Hit.agent_id)).all())
        # all hits, pictures or not: the template shows a placeholder for a hit without one
        thumbs: dict[int, list[Hit]] = {}
        for h in db.exec(select(Hit).order_by(col(Hit.first_seen_at).desc())):
            row = thumbs.setdefault(h.agent_id, [])
            if len(row) < 4:
                row.append(h)
    return render(request, "agents.html", agents=agents, counts=counts, thumbs=thumbs, error=error,
                  quiet=quiet_hours.state(), quiet_cfg=quiet_hours.config(),
                  next_run=scheduler.next_run, settings=get_settings(), thresholds=screening.thresholds())


BUILDER_START = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz"


@app.get("/agents/new", response_class=HTMLResponse)
async def agents_new(request: Request):
    """Search-agent builder: keyword + willhaben's own filters, no copied link needed.

    State is the browser search URL in `url`; `keyword` and `r_<PARAM>` (range inputs such as
    r_PRICE_TO) are merged into it. Every filter value links to the URL with that value applied.
    """
    qp = request.query_params
    url = qp.get("url") or BUILDER_START
    changes: dict[str, str | None] = {k[2:]: re.sub(r"[^0-9]", "", v) or None
                                      for k, v in qp.items() if k.startswith("r_") and k[2:].isidentifier()}
    if "keyword" in qp:
        changes["keyword"] = qp["keyword"].strip() or None
    if changes:
        url = with_params(url, changes)
    error = filters = None
    try:
        filters = await get_public().search_filters(url)
    except (WillhabenError, InvalidSearchUrl) as e:
        error = str(e)
    return render(request, "agent_new.html", f=filters, url=url, error=error, url_param=url_param,
                  settings=get_settings(), thresholds=screening.thresholds())


@app.post("/agents")
async def agents_create(url: str = Form(...), name: str = Form(""), interval_min: int = Form(5),
                        screen_on: str | None = Form(None), screen_brief: str = Form(""), screen_min: str = Form("match"),
                        screen_hide: list[str] = Form([])):
    try:
        agent = create_agent(url, name or None, interval_min * 60)
    except InvalidSearchUrl as e:
        return back(f"/agents?error={e}")
    if screen_on:
        agent = _set_screening(agent.id, True, screen_brief, screen_min, screen_hide)
    scheduler.schedule_agent(agent)
    return back("/agents")


@app.post("/agents/{agent_id}/run")
async def agents_run(agent_id: int):
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        last = a.last_run_at if a else None
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last and (utcnow() - last).total_seconds() < 60:   # no hammering willhaben via the button
        return back(f"/hits?agent_id={agent_id}")
    res = await run_agent(agent_id)
    if res.error:
        return back(f"/agents?error={res.error}")
    return back(f"/hits?agent_id={agent_id}")


@app.post("/agents/{agent_id}/toggle")
async def agents_toggle(agent_id: int):
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        a.enabled = not a.enabled
        db.add(a)
        db.commit()
        db.refresh(a)
    scheduler.schedule_agent(a)
    return back("/agents")


@app.post("/agents/{agent_id}/edit")
async def agents_edit(agent_id: int, name: str = Form(...), interval_min: int = Form(...),
                      notify_on: str | None = Form(None), drops_on: str | None = Form(None),
                      screen_on: str | None = Form(None), screen_brief: str = Form(""), screen_min: str = Form("match"),
                      screen_hide: list[str] = Form([])):
    s = get_settings()
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        a.name = name.strip() or a.name
        a.interval_s = max(interval_min * 60, s.min_agent_interval_s)
        a.notify, a.notify_price_drops = bool(notify_on), bool(drops_on)
        db.add(a)
        db.commit()
        db.refresh(a)
    a = _set_screening(agent_id, bool(screen_on), screen_brief, screen_min, screen_hide)
    scheduler.schedule_agent(a, run_soon=False)
    return back("/agents")


def _set_screening(agent_id: int, on: bool, brief: str, minimum: str, hide: list[str] | None = None) -> SearchAgent:
    """Claude screening of new hits (see app/services/screening.py)."""
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        a.screen = on
        a.screen_brief = brief.replace("\r\n", "\n").strip() or None
        a.screen_min = minimum if minimum in screening.MIN_RANK else "match"
        if hide is not None:
            a.screen_hide = ",".join(i for i in screening.HIDEABLE if i in hide) or "-"
        db.add(a)
        db.commit()
        db.refresh(a)
    return a


@app.post("/agents/{agent_id}/delete")
async def agents_delete(agent_id: int):
    scheduler.unschedule_agent(agent_id)
    with session() as db:
        for h in db.exec(select(Hit).where(Hit.agent_id == agent_id)):
            db.delete(h)
        a = db.get(SearchAgent, agent_id)
        if a:
            db.delete(a)
        db.commit()
    return back("/agents")


@app.get("/hits", response_class=HTMLResponse)
async def hits_page(request: Request, agent_id: int | None = None, state: str = "new"):
    with session() as db:
        q = select(Hit)
        if agent_id:
            q = q.where(Hit.agent_id == agent_id)
        if state != "all":
            q = q.where(Hit.state == state)
        hits = list(db.exec(q.order_by(col(Hit.published_at).desc().nulls_last(), col(Hit.id).desc()).limit(200)))
        agents = {a.id: a for a in db.exec(select(SearchAgent))}
        counts_q = select(Hit.state, func.count()).group_by(Hit.state)
        state_counts = dict(db.exec(counts_q.where(Hit.agent_id == agent_id) if agent_id else counts_q).all())
    state_counts["all"] = sum(state_counts.values())
    return render(request, "hits.html", hits=hits, agents=agents, agent_id=agent_id, state=state, state_counts=state_counts)


@app.get("/hits/ad/{ad_id}", response_class=HTMLResponse)
async def hit_ad_detail(request: Request, ad_id: str, hit: int | None = None):
    """Ad detail as a fragment for the in-dashboard viewer (no redirect to willhaben)."""
    if not ad_id.isdigit():
        raise HTTPException(400)
    h = None
    if hit:
        with session() as db:
            h = db.get(Hit, hit)
            if h and h.state == "new":   # opened = seen
                h.state = "seen"
                db.add(h)
                db.commit()
                db.refresh(h)
    error = d = None
    try:
        d = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        error = str(e)
    # the price verdict is the lazily loaded deals section (/hits/ad/{id}/deal), one verdict per ad
    return templates.TemplateResponse(request, "_ad_detail.html", {"d": d, "h": h, "ad_id": ad_id, "error": error})


@app.post("/hits/{hit_id}/state", response_class=HTMLResponse)
async def hit_state(request: Request, hit_id: int, state: str = Form(...)):
    with session() as db:
        h = db.get(Hit, hit_id)
        h.state = state
        db.add(h)
        db.commit()
        db.refresh(h)
    if request.headers.get("hx-request"):
        return HTMLResponse("")
    return back("/hits")


@app.post("/hits/mark-seen")
async def hits_mark_seen(agent_id: int | None = Form(None)):
    with session() as db:
        q = select(Hit).where(Hit.state == "new")
        if agent_id:
            q = q.where(Hit.agent_id == agent_id)
        for h in db.exec(q):
            h.state = "seen"
            db.add(h)
        db.commit()
    return back(f"/hits?agent_id={agent_id}" if agent_id else "/hits")


# ------------------------------------------------------------------ chats
def _chat_list(show_hidden: bool) -> dict[str, Any]:
    """The conversation list (chats.html); the open chat (chat.html) shows it next to the thread."""
    with session() as db:
        convs = list(db.exec(select(Conversation).where(col(Conversation.removed).is_not(True))
                             .order_by(col(Conversation.last_message_at).desc().nulls_last())))
        open_drafts = set(db.exec(select(Draft.conversation_id).where(Draft.status == "draft")).all())
    hidden_count = sum(1 for c in convs if c.hidden_at)
    if not show_hidden:
        convs = [c for c in convs if not c.hidden_at]
    return {"convs": convs, "open_drafts": open_drafts, "hidden_count": hidden_count, "show_hidden": show_hidden}


@app.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, msg: str | None = None, show_hidden: bool = False):
    return render(request, "chats.html", msg=msg, live_state=chats.live_state(), **_chat_list(show_hidden))


@app.get("/chats/live-state")
async def chats_live_state(c: str | None = None):
    """What the open chat page shows, as fingerprints: it polls this (local database only, no willhaben request)
    and swaps just the part that changed – new messages appear without a reload."""
    return chats.live_state(c)


@app.post("/chats/bulk/{action}")
async def chats_bulk(request: Request, action: str):
    """Selected chats: mark read (read receipts on willhaben, best effort) or hide here."""
    if action not in ("read", "hide"):
        raise HTTPException(404)
    ids = [str(i) for i in (await request.form()).getlist("ids")]
    for cid in ids:
        if action == "hide":
            chats.hide(cid)
        else:
            if get_session().logged_in:
                await chats.mark_read(cid)
            with session() as db:
                conv = db.get(Conversation, cid)
                if conv and conv.unread_count:
                    conv.unread_count = 0
                    db.add(conv)
                    db.commit()
    msg = t("{n} chats hidden", n=len(ids)) if action == "hide" else t("{n} chats marked read", n=len(ids))
    return back(f"/chats?msg={quote(msg, safe='')}")


@app.post("/chats/{conversation_id}/hide")
async def chat_hide(conversation_id: str, hidden: str = Form("1")):
    chats.hide(conversation_id, hidden == "1")
    return back(f"/chats?msg={quote(t('Chat hidden here (still on willhaben); it comes back when they write again') if hidden == '1' else '', safe='')}")


@app.post("/chats/{conversation_id}/remove")
async def chat_remove(conversation_id: str):
    chats.remove(conversation_id)
    return back(f"/chats?msg={quote(t('Chat removed here (still on willhaben); it comes back when they write again'), safe='')}")


@app.post("/chats/sync")
async def chats_sync(request: Request):
    """Background job (lane chat): the list updates itself as the conversations come in (live view)."""
    chats.enqueue_sync()
    if _hx(request):
        return Response(status_code=204)
    return back(f"/chats?msg={quote(t('Sync started – it runs in the background'), safe='')}")


@app.get("/chats/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: str, msg: str | None = None, sort: str | None = None,
                    show_hidden: bool = False):
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404)
    if conv.unread_count and get_session().logged_in:   # you read it here, so the buyer sees "read" (background)
        task = asyncio.create_task(chats.mark_read(conversation_id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    drafts = chats.drafts_of(conversation_id)
    return render(request, "chat.html", conv=conv, messages=chats.messages_of(conversation_id),
                  drafts=drafts, msg=msg, live_state=chats.live_state(conversation_id), meetup=meetups.current(conv, drafts), gcal_on=gcal.connected(),
                  sale=sold.state_for(conv),
                  gcal_duration=gcal.config()["duration"], **await _shipping_ctx(conversation_id, conv, sort),
                  **_chat_list(show_hidden or bool(conv.hidden_at)))


async def _shipping_ctx(conversation_id: str, conv: Conversation, sort: str | None = None) -> dict[str, Any]:
    from app.services import dropoff
    s = shipping.open_for(conversation_id)
    if sort in ("price", "near"):
        set_setting("ship_sort", sort)
    sort = get_setting("ship_sort") or "price"
    near: dict[str, list[dict[str, Any]]] = {}
    if s and conv.role == "seller":
        try:
            near = await asyncio.wait_for(dropoff.nearest(shipping.sender(), shipping.enabled_carriers()), 10)
        except TimeoutError:
            near = {}
    options = shipping.quote(s.parcel) if s else []
    if sort == "near" and near:
        far = 10 ** 9
        options = sorted(options, key=lambda o: ((near.get(o.carrier) or [{}])[0].get("dist_m") or far, o.price))
    return {"shipment": s, "ship_options": options, "ship_near": near, "ship_sort": sort,
            "fmt_dist": dropoff.fmt_dist,
            "ship_option": shipping.by_key(s.option) if s else None, "ship_on": conv.role == "seller",
            "ship_sender_ok": shipping.sender_complete(), "ship_carriers": shipping.CARRIERS,
            "ship_all": shipping.all_options(s.parcel) if s else [], **(shipping.page_state(s) if s else {}),
            **printing_routes.shipment_panel(s)}


@app.post("/chats/{conversation_id}/refresh")
async def chat_refresh(request: Request, conversation_id: str):
    """Background job (lane chat): the thread updates itself (live view)."""
    chats.enqueue_reload(conversation_id)
    if _hx(request):
        return Response(status_code=204)
    return back(f"/chats/{conversation_id}")


@app.post("/chats/{conversation_id}/draft", response_class=HTMLResponse)
async def chat_draft(request: Request, conversation_id: str, hint: str = Form("")):
    """Background job (lane claude): the composer shows "Claude is writing…", the draft appears by itself."""
    chats.enqueue_draft(conversation_id, hint.strip() or None)
    return back(f"/chats/{conversation_id}")


@app.post("/drafts/{draft_id}/send")
async def draft_send(draft_id: int, text: str = Form(...)):
    """Queued: the chat page comes back at once with the message marked "sending…" (chats.enqueue_send)."""
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is None:
        raise HTTPException(404)
    if not chats.enqueue_send(draft_id, text):
        return back(f"/chats/{d.conversation_id}?msg={quote(t('Not sent: the draft is not open any more or empty'), safe='')}")
    return back(f"/chats/{d.conversation_id}#draft-{draft_id}")


@app.post("/drafts/{draft_id}/mark-sent")
async def draft_mark_sent(draft_id: int, text: str = Form("")):
    d = chats.mark_sent(draft_id, text or None)
    return back(f"/chats/{d.conversation_id}")


@app.post("/drafts/{draft_id}/discard")
async def draft_discard(draft_id: int):
    with session() as db:
        d = db.get(Draft, draft_id)
    chats.discard_draft(draft_id)
    return back(f"/chats/{d.conversation_id}")


# ------------------------------------------------------------------ my ads
_MYADS_FILTERS = ("show_active", "show_inactive", "show_sold", "show_hidden")


def _myads_back(request: Request, msg: str = "", anchor: str = "", form: Any = None) -> RedirectResponse:
    """Back to My ads with the filters the list was showing: from the posted form, else from the page that posted."""
    page = request.headers.get("hx-current-url") or request.headers.get("referer") or ""   # htmx sends its page
    src = form if form is not None and any(form.get(k) for k in _MYADS_FILTERS) else dict(parse_qsl(urlsplit(page).query))
    keep = "".join(f"&{k}=1" for k in _MYADS_FILTERS if src.get(k))
    ad = anchor[4:] if anchor.startswith("#ad-") else str((form.get("ad") if form is not None else None) or src.get("ad") or "")
    if ad:
        keep += f"&ad={quote(ad, safe='')}"   # the ad whose detail is open (myads.html list -> detail)
    return back(f"/myads?msg={quote(msg, safe='')}{keep}{anchor}")


@app.get("/myads", response_class=HTMLResponse)
async def myads_page(request: Request, msg: str | None = None, show_hidden: bool = False, show_sold: bool = False,
                     show_inactive: bool = False, show_active: bool = False):
    with session() as db:
        empty = db.exec(select(MyAd).limit(1)).first() is None
    if empty and get_session().logged_in:
        try:
            await my_ads.import_from_account()
        except (NotLoggedIn, SessionUnavailable, ValueError) as e:
            msg = msg or t("Could not load ads: {error}", error=e)
    with session() as db:   # deleted on willhaben: gone from the list (the row stays for old chats and the sold workflow)
        rows = list(db.exec(select(MyAd).where(MyAd.status != "deleted")
                            .order_by(col(MyAd.end_date).desc().nulls_last())))   # most days left first
    rows = [a for a in rows if not a.replaced_by]   # a renewed ad's old copy: its chats use the new one (current_ad)
    hidden = [a for a in rows if a.hidden]
    ads = rows if show_hidden else [a for a in rows if not a.hidden]
    # sections: active on top, then expired (ran out recently, always shown: they need renewing), then the rest;
    # sold / given away and inactive (deactivated, expired before the cutoff) ads are hidden unless asked for
    # The filter chips toggle three groups on and off: Active (active + recently expired: they need renewing),
    # Deactivated (deactivated, expired before the cutoff), Sold / reserved (sold, given away, reserved).
    # Nothing switched on: Active.
    cutoff = int(get_setting("myads_expired_days") or 0)
    reserved = lambda a: a.status == "reserved" or a.sale_status == "reserved"   # noqa: E731
    sold = [a for a in ads if my_ads.is_sold(a) or reserved(a)]
    active = [a for a in ads if a not in sold and a.status == "active"]
    expired = [a for a in ads if a not in sold and my_ads.is_expired(a, cutoff)]
    inactive = [a for a in ads if a not in sold and a not in active and a not in expired]
    show_active = show_active or not (show_inactive or show_sold)
    groups = [g for g in ((t("Active ({n})", n=len(active)), active if show_active else []),
                          (t("Expired ({n})", n=len(expired)), expired if show_active else []),
                          (t("Deactivated ({n})", n=len(inactive)), inactive if show_inactive else []),
                          (t("Sold / reserved ({n})", n=len(sold)), sold if show_sold else [])) if g[1]]
    sections = {}    # list index -> section heading; only when there is more than one section
    if len(groups) > 1:
        i = 0
        for title, group in groups:
            sections[i] = title
            i += len(group)
    ads = [a for _, group in groups for a in group]
    return render(request, "myads.html", ads=ads, msg=msg, myadverts_url=my_ads.MYADVERTS_URL,
                  show_active=show_active, active_count=len(active) + len(expired),
                  hidden_count=len(hidden), show_hidden=show_hidden, sold_count=len(sold), show_sold=show_sold,
                  inactive_count=len(inactive), show_inactive=show_inactive, sections=sections)


def _hx(request: Request) -> bool:
    return bool(request.headers.get("hx-request"))


@app.post("/myads/check")
async def myads_check_all(request: Request):
    my_ads.start_update()
    if _hx(request):        # the header shows the progress, the list refreshes itself when it is done
        return Response(status_code=204)
    return _myads_back(request, t("Update from willhaben started – it runs in the background"))


@app.post("/myads/{ad_id}/auto-renew")
async def myads_auto(request: Request, ad_id: str, on: str = Form("0")):
    my_ads.set_auto_renew(ad_id, on == "1")
    if request.headers.get("x-autosave"):          # My ads saves switches as you flip them (no reload)
        return JSONResponse({"ok": True})
    return _myads_back(request)


@app.post("/myads/{ad_id}/auto-reply")
async def myads_auto_reply(request: Request, ad_id: str, on: str | None = Form(None), instructions: str = Form(""),
                           min_price: str = Form("")):
    error = None
    with session() as db:
        row = db.get(MyAd, ad_id)
        was_on = bool(row and row.auto_reply)
    try:
        floor = min_price_svc.parse_form(min_price)     # empty = no floor
        my_ads.set_auto_reply(ad_id, bool(on), instructions, floor)
    except ValueError:  # bad floor: still save on/off + instructions, keep the stored floor
        error = t('Minimum price must be a number, e.g. 25 or 25,50')
        my_ads.set_auto_reply(ad_id, bool(on), instructions)
    if request.headers.get("x-autosave"):          # auto-save while typing: no reload, no reply run per keystroke
        if on and not was_on:
            task = asyncio.create_task(chats.process_pending_auto(ad_id))
            _background.add(task)
            task.add_done_callback(_background.discard)
        return JSONResponse({"ok": error is None, "error": error})
    if on:  # answer buyers who are already waiting, in the background (Claude takes a few seconds)
        task = asyncio.create_task(chats.process_pending_auto(ad_id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    msg = error or (t('Automatic replies on') if on else t('Automatic replies off'))
    return _myads_back(request, msg, f"#ad-{ad_id}")


@app.get("/myads/{ad_id}/edit", response_class=HTMLResponse)
async def myads_edit_page(request: Request, ad_id: str, msg: str | None = None):
    with session() as db:
        row = db.get(MyAd, ad_id)
    if row is None:
        raise HTTPException(404)
    try:
        cur = await my_ads.load_for_edit(ad_id)
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        return back(f"/myads?msg={quote(t('Could not load the ad from willhaben: {error}', error=e), safe='')}")
    unsaved = (row.pending_payload or {}).get("unsaved") if not row.pending_action else None
    if unsaved:     # a save that failed or was interrupted: its text comes back instead of willhaben's
        cur = {**cur, **unsaved}
    return render(request, "myad_edit.html", a=row, cur=cur, msg=msg, unsaved=bool(unsaved))


@app.post("/myads/{ad_id}/edit/ai", response_class=HTMLResponse)
async def myads_edit_ai(request: Request, ad_id: str, heading: str = Form(""), description: str = Form(""),
                        price: str = Form(""), instruction: str = Form("")):
    """Claude's proposal goes back into the form, unsaved; the user checks it and saves to willhaben."""
    with session() as db:
        row = db.get(MyAd, ad_id)
    if row is None:
        raise HTTPException(404)
    try:
        value = float(price.replace("€", "").replace(".", "").replace(",", ".").strip()) if price.strip() else None
    except ValueError:
        value = None
    cur = {"heading": heading, "description": description, "price": value}
    try:
        prop = await my_ads.ai_revise(ad_id, heading, description, value, instruction)
    except LLMError as e:   # shown inside the part htmx swaps (#edit-main), not in the page head
        return render(request, "myad_edit.html", a=row, cur=cur, instruction=instruction,
                      ai_error=t("Claude could not revise the ad: {error}", error=e))
    return render(request, "myad_edit.html", a=row, instruction=instruction, proposal=prop,
                  cur={"heading": prop["heading"], "description": prop["description"], "price": value})


@app.post("/myads/{ad_id}/edit")
async def myads_edit_save(ad_id: str, heading: str = Form(...), description: str = Form(...), price: str = Form("")):
    """Background job (lane ads): back to the ad, which shows "Saving…" until willhaben has it."""
    from app.willhaben.insert import InsertError
    try:
        value = float(price.replace("€", "").replace(".", "").replace(",", ".").strip()) if price.strip() else None
    except ValueError:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Invalid price'), safe='')}")
    try:
        queued = my_ads.enqueue_edit(ad_id, heading, description, value)
    except InsertError as e:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Saving failed: {error}', error=e), safe='')}")
    if not queued:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Nothing queued: these ads are already being processed'), safe='')}")
    return back(f"/myads?ad={quote(ad_id, safe='')}&msg={quote(t('Saving on willhaben in the background'), safe='')}")


@app.post("/myads/{ad_id}/photos")
async def myads_photos_save(ad_id: str, order: list[str] = Form(default=[]),
                            photos: list[UploadFile] = File(default=[]), new_first: str = Form("")):
    """Photos in the order shown on the edit page; new uploads go to the start or the end."""
    from app.willhaben.insert import InsertError
    files = [(p.filename or "foto.jpg", await p.read()) for p in photos if p.filename]
    new = [f"new:{i}" for i in range(len(files))]
    order = (new + order) if new_first else (order + new)
    try:
        queued = my_ads.enqueue_photos(ad_id, order, files)     # background job: uploads one after another
    except InsertError as e:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Saving the photos failed: {error}', error=e), safe='')}#photos")
    if not queued:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Nothing queued: these ads are already being processed'), safe='')}#photos")
    return back(f"/myads?ad={quote(ad_id, safe='')}&msg={quote(t('Saving the photos on willhaben in the background'), safe='')}")


@app.post("/myads/bulk/{action}")
async def myads_bulk(request: Request, action: str):
    if action not in ("activate", "deactivate", "delete"):
        raise HTTPException(404)
    form = await request.form()
    ids = [str(i) for i in form.getlist("ids") if str(i).isdigit()]
    if not ids:
        return _myads_back(request, t("No ads selected"), form=form)
    return _queued(request, my_ads.enqueue(action, ids), form)


def _queued(request: Request, n: int, form: Any = None) -> RedirectResponse:
    msg = t("Queued: {n} – processed in the background, one willhaben request after another", n=n) if n \
        else t("Nothing queued: these ads are already being processed")
    return _myads_back(request, msg, form=form)


@app.get("/myads/cleanup", response_class=HTMLResponse)
async def myads_cleanup_page(request: Request):
    try:
        cands = await my_ads.cleanup_candidates()
    except (NotLoggedIn, SessionUnavailable, WillhabenError, ValueError) as e:
        return back(f"/myads?msg={quote(t('Could not load ads: {error}', error=e), safe='')}")
    with session() as db:   # already queued (a clean-up or delete still running): not offered twice
        pending = set(db.exec(select(MyAd.ad_id).where(col(MyAd.pending_action).is_not(None))))
    return render(request, "myads_cleanup.html", cands=cands, pending=pending)


@app.post("/myads/cleanup")
async def myads_cleanup(request: Request):
    """Runs as a background job (the header shows the progress); the picked ads are marked in the list at once."""
    ids = [str(i) for i in (await request.form()).getlist("ids") if str(i).isdigit()]
    if not ids:
        return back(f"/myads?msg={quote(t('No ads selected'), safe='')}")
    n = my_ads.enqueue_cleanup(ids)
    return back(f"/myads?msg={quote(t('Clean-up of {n} ads started – it runs in the background', n=n), safe='')}")


@app.post("/myads/{ad_id}/hide")
async def myads_hide(request: Request, ad_id: str, hidden: str = Form("1")):
    my_ads.set_hidden(ad_id, hidden == "1")
    return _myads_back(request, t("Hidden from the list (still online on willhaben)") if hidden == "1" else "")


@app.post("/myads/{ad_id}/deactivate")
async def myads_deactivate(request: Request, ad_id: str):
    return _queued(request, my_ads.enqueue("deactivate", [ad_id]))


@app.post("/myads/{ad_id}/delete")
async def myads_delete(request: Request, ad_id: str):
    return _queued(request, my_ads.enqueue("delete", [ad_id]))


@app.get("/jobs")
async def jobs_status():
    """Background jobs for the jobs indicator, plus every element still waiting for one (spinners on the page):
    keys "ad:<id>" (own ad action), "draft:<id>" (chat message being sent), "addraft:<id>" (ad being published),
    "refining:<id>" (Claude revises an ad draft), "drafting:<conversation>" (Claude writes an answer)."""
    with session() as db:
        pending = [f"ad:{i}" for i in db.exec(select(MyAd.ad_id).where(col(MyAd.pending_action).is_not(None)))]
        pending += [f"draft:{i}" for i in db.exec(select(Draft.id).where(Draft.status == "sending"))]
        pending += [f"addraft:{i}" for i in db.exec(select(AdDraft.id).where(AdDraft.publishing == True))]  # noqa: E712
        pending += [f"refining:{i}" for i in db.exec(select(AdDraft.id).where(AdDraft.refining == True))]  # noqa: E712
        pending += [f"drafting:{i}" for i in db.exec(select(Conversation.id).where(Conversation.drafting == True))]  # noqa: E712
    return {**jobs.snapshot(), "pending": pending}


# ------------------------------------------------------------------ sold / reserved workflow (app/services/sold.py)
def _sold_url(ad_id: str, kind: str, buyer: str | None, msg: str | None = None, anchor: str = "") -> str:
    q = f"kind={sold.kind_of(kind)}" + (f"&buyer={quote(buyer, safe='')}" if buyer else "")
    return f"/sold/{quote(ad_id, safe='')}?{q}" + (f"&msg={quote(msg, safe='')}" if msg else "") + anchor


@app.get("/sold/{ad_id}", response_class=HTMLResponse)
async def sold_page(request: Request, ad_id: str, kind: str = "sold", buyer: str | None = None, msg: str | None = None):
    return _sold_render(request, ad_id, kind, buyer, msg)


def _sold_render(request: Request, ad_id: str, kind: str, buyer: str | None, msg: str | None,
                 reasons: list | None = None) -> HTMLResponse:
    kind = sold.kind_of(kind)
    convs = sold.seller_conversations(ad_id)
    buyer_conv = next((c for c in convs if c.id == buyer), None)
    return render(request, "sold.html", ad_id=ad_id, kind=kind, ad=sold.ad_row(ad_id), title=sold.title_of(ad_id),
                  convs=convs, buyer=buyer_conv, others=sold.others(ad_id, buyer_conv.id if buyer_conv else None),
                  reasons=reasons or sold.pending_reasons(ad_id), msg=msg)   # reasons a background step left behind


@app.post("/sold/{ad_id}/willhaben", response_class=HTMLResponse)
async def sold_willhaben(request: Request, ad_id: str, kind: str = Form("sold"), buyer: str = Form(""),
                         undo: str = Form(""), reason_id: str = Form("")):
    """Background job (lane ads). If willhaben asks for a sold reason, the job leaves the reasons on the ad and this
    page offers them; the next confirm queues it again with the reason."""
    if sold.kind_of(kind) == "sold" and undo:
        return back(_sold_url(ad_id, kind, buyer or None, t("willhaben cannot undo \"sold\"."), "#step1"))
    queued = sold.enqueue_willhaben(ad_id, kind, undo=bool(undo), reason_id=reason_id or None)
    msg = t("On its way to willhaben – it runs in the background") if queued else t("Nothing queued: these ads are already being processed")
    return back(_sold_url(ad_id, kind, buyer or None, msg, "#step1"))


@app.post("/sold/{ad_id}/notices")
async def sold_notices(request: Request, ad_id: str):
    form = await request.form()
    kind, buyer = str(form.get("kind") or "sold"), str(form.get("buyer") or "") or None
    convs = [str(x) for x in form.getlist("conv")]
    if form.get("mode") == "claude":      # Claude takes a while per chat: background job (lane claude)
        msg = t("Claude writes {n} drafts in the background – nothing is sent", n=sold.enqueue_notice_drafts(ad_id, kind, convs, buyer=buyer))
    else:
        made, errors = await sold.prepare_notices(ad_id, kind, convs, buyer=buyer)
        msg = t("{n} drafts prepared – nothing sent yet", n=made) + ("\n" + "\n".join(errors) if errors else "")
    return back(_sold_url(ad_id, kind, buyer, msg, "#step2"))


@app.post("/sold/{ad_id}/notices/send")
async def sold_notices_send(request: Request, ad_id: str):
    form = await request.form()
    kind, buyer = str(form.get("kind") or "sold"), str(form.get("buyer") or "") or None
    ids = [int(x) for x in form.getlist("send") if str(x).isdigit()]
    if form.get("action") == "discard":
        msg = t("{n} drafts discarded", n=sold.discard_notices(ad_id, ids))
    else:
        msg = t("{n} messages queued – they go out in the background, one after another",
                n=sold.send_notices(ad_id, [(i, str(form.get(f"text_{i}") or "")) for i in ids]))
    return back(_sold_url(ad_id, kind, buyer, msg, "#step2"))


@app.post("/sold/{ad_id}/finish")
async def sold_finish(ad_id: str, kind: str = Form("sold"), buyer: str = Form(""), auto_off: str | None = Form(None)):
    sold.finish(ad_id, kind, buyer or None, auto_off=bool(auto_off))
    msg = t("Saved: {status}", status=t("sold") if sold.kind_of(kind) == "sold" else t("reserved"))
    return back(_sold_url(ad_id, kind, buyer or None, msg, "#step3"))


@app.post("/sold/{ad_id}/reset")
async def sold_reset(ad_id: str, kind: str = Form("sold"), buyer: str = Form("")):
    sold.reset(ad_id)
    return back(_sold_url(ad_id, kind, buyer or None, t("Sale status removed here (willhaben unchanged)"), "#step3"))


# ------------------------------------------------------------------ new ad
@app.get("/new-ad", response_class=HTMLResponse)
async def new_ad_page(request: Request):
    with session() as db:
        drafts = list(db.exec(select(AdDraft).order_by(col(AdDraft.created_at).desc()).limit(20)))
        batches = list(db.exec(select(AdBatch).order_by(col(AdBatch.created_at).desc()).limit(10)))
    return render(request, "new_ad.html", drafts=drafts, batches=batches, max_batch=ad_batch.MAX_BATCH_IMAGES)


@app.post("/new-ad")
async def new_ad_create(notes: str = Form(""), photos: list[UploadFile] = File(default=[])):
    files = [(p.filename or "", await p.read()) for p in photos if p.filename]
    if not notes.strip() and not files:
        return back("/new-ad")
    draft = await ad_creator.create_draft(notes, files)
    return back(f"/new-ad/{draft.id}")


@app.get("/new-ad/{draft_id:int}", response_class=HTMLResponse)
async def ad_draft_page(request: Request, draft_id: int, msg: str | None = None):
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None:
        raise HTTPException(404)
    gh = d.geizhals or {}
    return render(request, "ad_draft.html", d=d, msg=msg, insert_url=ad_creator.INSERT_URL, gh=gh,
                  spec_block=geizhals.spec_block(gh.get("match")))


@app.post("/new-ad/{draft_id:int}")
async def ad_draft_save(draft_id: int, title: str = Form(...), description: str = Form(...),
                        price: str = Form("")):
    ad_creator.save_fields(draft_id, title, description, price)
    return back(f"/new-ad/{draft_id}?msg={quote(t('Changes saved'), safe='')}")


@app.post("/new-ad/{draft_id:int}/refine")
async def ad_draft_refine(draft_id: int, instruction: str = Form(...)):
    """Background job (lane claude): the draft shows "Claude is revising…" and swaps itself in when done."""
    _draft_or_404(draft_id)
    ad_creator.enqueue_refine(draft_id, instruction)
    return back(f"/new-ad/{draft_id}")


@app.post("/new-ad/{draft_id:int}/reconcile")
async def ad_draft_reconcile(draft_id: int):
    await ad_creator.reconcile_with_geizhals(draft_id)
    return back(f"/new-ad/{draft_id}")


@app.post("/new-ad/{draft_id:int}/undo")
async def ad_draft_undo(draft_id: int):
    ad_creator.undo_revision(draft_id)
    return back(f"/new-ad/{draft_id}?msg={quote(t('Previous version restored'), safe='')}")


@app.post("/new-ad/{draft_id:int}/geizhals")
async def ad_draft_geizhals(draft_id: int, url: str = Form(""), query: str = Form("")):
    try:
        if url.strip():
            await ad_creator.choose_geizhals(draft_id, url.strip())
        else:
            await ad_creator.search_geizhals(draft_id, query)
    except geizhals.GeizhalsError as e:
        return back(f"/new-ad/{draft_id}?msg={quote(str(e), safe='')}#geizhals")
    return back(f"/new-ad/{draft_id}#geizhals")


def _save_posted_fields(draft_id: int, form: Any) -> None:
    """Unsaved title/description/price that came along with a willhaben action: saved first."""
    if form.get("title") is not None and form.get("description") is not None:
        ad_creator.save_fields(draft_id, str(form["title"]), str(form["description"]), str(form.get("price") or ""))


@app.post("/new-ad/{draft_id:int}/willhaben/prepare")
async def ad_draft_wh_prepare(request: Request, draft_id: int):
    """Read-only: categories and required fields from willhaben's insertion form."""
    _draft_or_404(draft_id)
    form = await request.form()
    _save_posted_fields(draft_id, form)
    try:
        await ad_creator.prepare_willhaben(draft_id, str(form.get("category_query") or "").strip() or None)
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        ad_creator.update_draft(draft_id, error=t("willhaben form not reachable: {error}", error=str(e)))
    return back(f"/new-ad/{draft_id}#willhaben")


@app.post("/new-ad/{draft_id:int}/willhaben/category")
async def ad_draft_wh_category(draft_id: int, code: str = Form(...)):
    """A category picked by hand ("Choose another category") becomes a selectable option in wh_plan."""
    _draft_or_404(draft_id)
    try:
        await ad_creator.add_manual_category(draft_id, code.strip())
    except ValueError as e:
        ad_creator.update_draft(draft_id, error=str(e))
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        ad_creator.update_draft(draft_id, error=t("willhaben form not reachable: {error}", error=str(e)))
    return back(f"/new-ad/{draft_id}#willhaben")


@app.post("/new-ad/{draft_id:int}/willhaben/publish")
async def ad_draft_wh_publish(request: Request, draft_id: int):
    """Publish on willhaben after the user's click + confirm, as a background job; never twice (enqueue_publish and
    publish_willhaben check the status)."""
    _draft_or_404(draft_id)
    form = await request.form()
    _save_posted_fields(draft_id, form)
    try:
        option = int(str(form.get("option", "")))
    except ValueError:
        option = -1
    prefix = f"o{option}_"
    values = {k[len(prefix):]: str(v) for k, v in form.multi_items() if k.startswith(prefix) and v}
    if not ad_creator.enqueue_publish(draft_id, option, values, shipping=ad_creator.parse_shipping(form)):
        return back(f"/new-ad/{draft_id}#willhaben")       # input error (on the draft) or already on its way
    return back(f"/new-ad/{draft_id}?msg={quote(t('Publishing in the background – you can leave this page'), safe='')}#willhaben")


@app.get("/api/willhaben/categories")
async def api_wh_categories(q: str = "", limit: int = 25):
    """Read-only: leaf categories of willhaben's insertion form matching q (form cached ~1 h in memory)."""
    from app.willhaben import insert
    if len(q.strip()) < 2:
        return {"results": []}
    try:
        form = await insert.cached_form(get_session())
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        return JSONResponse({"results": [], "error": str(e)}, status_code=503)
    return {"results": insert.search_categories(form.get("categoryTree") or {}, q, limit=max(1, min(limit, 50)))}


@app.get("/ad-drafts/{draft_id}/image/{idx}")
async def ad_draft_image(draft_id: int, idx: int):
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if not d or idx >= len(d.image_paths):
        raise HTTPException(404)
    return FileResponse(d.image_paths[idx])


# ------------------------------------------------------------------ batch of ads
@app.post("/new-ad/batch")
async def ad_batch_create(notes: str = Form(""), photos: list[UploadFile] = File(default=[])):
    b = ad_batch.new_batch(notes)
    skipped = 0
    for p in photos:
        if p.filename and not ad_batch.add_image(b.id, p.filename, await p.read()):
            skipped += 1
    if not ad_batch.get(b.id).image_paths:
        ad_batch.delete(b.id)
        return back("/new-ad?msg=" + quote(t("No usable photos in the batch"), safe=""))
    ad_batch.spawn(ad_batch.sort(b.id))
    msg = f"?msg={quote(t('{n} photos skipped (format, size or more than {max})', n=skipped, max=ad_batch.MAX_BATCH_IMAGES), safe='')}" \
        if skipped else ""
    return back(f"/new-ad/batch/{b.id}{msg}")


@app.get("/new-ad/batch/{batch_id}", response_class=HTMLResponse)
async def ad_batch_page(request: Request, batch_id: int, msg: str | None = None):
    b = ad_batch.get(batch_id)
    if b is None:
        raise HTTPException(404)
    ids = [g["draft_id"] for g in b.groups if g.get("draft_id")]
    with session() as db:
        drafts = {d.id: d for d in db.exec(select(AdDraft).where(col(AdDraft.id).in_(ids)))} if ids else {}
    return render(request, "ad_batch.html", b=b, msg=msg, drafts=drafts, unassigned=ad_batch.unassigned(b),
                  pending=ad_batch.pending(b), busy=b.status in ("sorting", "creating"),
                  max_images=ad_creator.MAX_IMAGES, parallel=ad_batch.CREATE_PARALLEL)


@app.post("/new-ad/batch/{batch_id}")
async def ad_batch_save(request: Request, batch_id: int):
    b = ad_batch.get(batch_id)
    if b is None:
        raise HTTPException(404)
    if b.status in ("sorting", "creating"):
        return back(f"/new-ad/batch/{batch_id}?msg=" + quote(t("Claude is still working on this batch"), safe=""))
    form = await request.form()
    order, fields = [], {}
    for key, value in form.multi_items():   # page order = photo order inside each group
        kind, _, ref = key.partition("_")
        if kind == "img" and ref.isdigit():
            order.append((int(ref), str(value)))
        elif kind in ("product", "comment", "include"):
            fields.setdefault(ref, {})[kind] = str(value)
    ad_batch.apply_review(batch_id, order, fields)
    action = form.get("action")
    if action == "resort":
        ad_batch.spawn(ad_batch.sort(batch_id, str(form.get("hint") or "")))
        return back(f"/new-ad/batch/{batch_id}")
    if action == "create":
        if not ad_batch.pending(ad_batch.get(batch_id)):
            return back(f"/new-ad/batch/{batch_id}?msg=" + quote(t("No group selected"), safe=""))
        ad_batch.spawn(ad_batch.create_drafts(batch_id))
        return back(f"/new-ad/batch/{batch_id}")
    return back(f"/new-ad/batch/{batch_id}?msg={quote(t('Changes saved'), safe='')}")


@app.post("/new-ad/batch/{batch_id}/delete")
async def ad_batch_delete(batch_id: int):
    b = ad_batch.get(batch_id)
    if b and b.status not in ("sorting", "creating"):
        ad_batch.delete(batch_id)
    return back("/new-ad")


@app.get("/new-ad/batch/{batch_id}/progress")
async def ad_batch_progress(batch_id: int):
    b = ad_batch.get(batch_id)
    if b is None:
        raise HTTPException(404)
    return {"status": b.status, "drafts": sum(1 for g in b.groups if g.get("draft_id"))}


def _draft_or_404(draft_id: int) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None:
        raise HTTPException(404)
    return d


def _draft_card(request: Request, d: AdDraft, saved: bool = False) -> HTMLResponse:
    """One draft as it appears on the batch page (htmx swaps it in place)."""
    return render(request, "_batch_draft.html", d=d, saved=saved, insert_url=ad_creator.INSERT_URL)


@app.post("/ad-drafts/{draft_id}/inline", response_class=HTMLResponse)
async def ad_draft_inline_save(request: Request, draft_id: int, title: str = Form(...), description: str = Form(...),
                               price: str = Form("")):
    _draft_or_404(draft_id)
    return _draft_card(request, ad_creator.save_fields(draft_id, title, description, price), saved=True)


@app.post("/ad-drafts/{draft_id}/inline/refine", response_class=HTMLResponse)
async def ad_draft_inline_refine(request: Request, draft_id: int, instruction: str = Form(...),
                                 title: str | None = Form(None), description: str | None = Form(None),
                                 price: str = Form("")):
    _draft_or_404(draft_id)
    if title is not None and description is not None:   # unsaved edits first, Claude works on the saved version
        ad_creator.save_fields(draft_id, title, description, price)
    ad_creator.enqueue_refine(draft_id, instruction)      # background job: the card swaps itself in when done
    return _draft_card(request, _draft_or_404(draft_id))


@app.post("/ad-drafts/{draft_id}/inline/status", response_class=HTMLResponse)
async def ad_draft_inline_status(request: Request, draft_id: int, status: str = Form(...),
                                 title: str | None = Form(None), description: str | None = Form(None),
                                 price: str = Form("")):
    if status not in ("draft", "published", "discarded"):
        raise HTTPException(400)
    _draft_or_404(draft_id)
    if title is not None and description is not None:
        ad_creator.save_fields(draft_id, title, description, price)
    return _draft_card(request, ad_creator.update_draft(draft_id, status=status))


@app.post("/ad-drafts/{draft_id}/inline/prepare", response_class=HTMLResponse)
async def ad_draft_inline_prepare(request: Request, draft_id: int, title: str | None = Form(None),
                                  description: str | None = Form(None), price: str = Form(""),
                                  category_query: str = Form("")):
    """Read-only: ask willhaben's insertion form for categories and required fields of this draft."""
    _draft_or_404(draft_id)
    if title is not None and description is not None:
        ad_creator.save_fields(draft_id, title, description, price)
    try:
        d = await ad_creator.prepare_willhaben(draft_id, category_query.strip() or None)
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        d = ad_creator.update_draft(draft_id, error=t("willhaben form not reachable: {error}", error=str(e)))
    return _draft_card(request, d)


@app.post("/ad-drafts/{draft_id}/inline/publish", response_class=HTMLResponse)
async def ad_draft_inline_publish(request: Request, draft_id: int):
    """Publish as free private ad on willhaben (the user clicked; the chosen category and field values come along).
    Runs as a background job: the card comes back at once with a spinner and swaps itself in when done."""
    _draft_or_404(draft_id)
    form = await request.form()
    if form.get("title") is not None and form.get("description") is not None:
        ad_creator.save_fields(draft_id, str(form["title"]), str(form["description"]), str(form.get("price") or ""))
    try:
        option = int(str(form.get("option", "")))
    except ValueError:
        option = -1
    prefix = f"o{option}_"
    values = {k[len(prefix):]: str(v) for k, v in form.multi_items() if k.startswith(prefix) and v}
    ad_creator.enqueue_publish(draft_id, option, values, shipping=ad_creator.parse_shipping(form))
    return _draft_card(request, _draft_or_404(draft_id))


@app.get("/ad-drafts/{draft_id}/card", response_class=HTMLResponse)
async def ad_draft_card(request: Request, draft_id: int):
    """The batch card on its own: swapped in when its background publish is done."""
    return _draft_card(request, _draft_or_404(draft_id))


@app.post("/ad-drafts/{draft_id}/inline/category", response_class=HTMLResponse)
async def ad_draft_inline_category(request: Request, draft_id: int):
    """Batch card: a category picked by hand becomes a selectable option in wh_plan (read-only on willhaben)."""
    _draft_or_404(draft_id)
    form = await request.form()
    _save_posted_fields(draft_id, form)
    try:
        d = await ad_creator.add_manual_category(draft_id, str(form.get("code") or "").strip())
    except ValueError as e:
        d = ad_creator.update_draft(draft_id, error=str(e))
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        d = ad_creator.update_draft(draft_id, error=t("willhaben form not reachable: {error}", error=str(e)))
    return _draft_card(request, d)


@app.get("/ad-drafts/{draft_id}/photos.zip")
async def ad_draft_photos_zip(draft_id: int):
    d = _draft_or_404(draft_id)
    return Response(ad_creator.photos_zip(d), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="draft-{d.id}-photos.zip"'})


@app.get("/ad-batches/{batch_id}/image/{idx}")
async def ad_batch_image(batch_id: int, idx: int):
    b = ad_batch.get(batch_id)
    if not b or not 0 <= idx < len(b.image_paths):
        raise HTTPException(404)
    return FileResponse(b.image_paths[idx])


# ------------------------------------------------------------------ session / capture
def _session_back(next_url: str | None, msg: str | None = None) -> RedirectResponse:
    """Back to where a session form was posted from (/session or Settings → willhaben account), with a message."""
    url = next_url if next_url and next_url.startswith("/") and not next_url.startswith("//") else "/session"
    path, _, frag = url.partition("#")
    if msg:
        path += ("&" if "?" in path else "?") + "msg=" + quote(msg, safe="")
    return back(path + (f"#{frag}" if frag else ""))


@app.get("/session", response_class=HTMLResponse)
async def session_page(request: Request, msg: str | None = None, embed: bool = False):
    """embed=1: bare page for the remote-view overlay (base.html opens it in a dialog on any page)."""
    caps = sorted(get_settings().captures_dir.glob("*.json"), reverse=True)[:30]
    return render(request, "session.html", captures=[c.name for c in caps], msg=msg, settings=get_settings(),
                  label_form=label_fill.active(), session_next="/session?embed=1" if embed else "/session",
                  embed=embed)


@app.post("/session/start")
async def session_start(next: str = Form("/session")):
    sess = get_session()
    if not sess.started:
        await sess.start()
    return _session_back(next)


@app.post("/session/login")
async def session_login(next: str = Form("/session")):
    try:
        await get_session().open_login()
    except SessionUnavailable as e:
        return _session_back(next, str(e))
    return _session_back(next, t('Login page opened in the browser window'))


@app.post("/session/check")
async def session_check(next: str = Form("/session")):
    ok = await get_session().check_login() if get_session().started else False
    return _session_back(next, t('Logged in') if ok else t('Not logged in'))


@app.post("/session/open")
async def session_open(url: str = Form(...), next: str = Form("/session")):
    if not url.startswith("https://www.willhaben.at/"):
        return _session_back(next, t('Only willhaben.at URLs'))
    await get_session().open_url(url)
    return _session_back(next)


@app.get("/session/screenshot.png")
async def session_screenshot():
    try:
        png = await get_session().screenshot()
    except SessionUnavailable:
        raise HTTPException(503) from None
    return Response(png, media_type="image/png", headers={"cache-control": "no-store"})


@app.get("/session/screen.jpg")
async def session_screen():
    """Frame for the remote view on the session page (polled by the browser)."""
    try:
        jpg = await get_session().screenshot("jpeg")
    except SessionUnavailable:
        raise HTTPException(503) from None
    return Response(jpg, media_type="image/jpeg", headers={"cache-control": "no-store"})


@app.post("/session/input", status_code=204)
async def session_input(payload: dict[str, Any]):
    try:
        await get_session().remote_input(payload)
    except SessionUnavailable as e:
        raise HTTPException(503, str(e)) from None
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None


@app.post("/session/capture/start")
async def capture_start(label: str = Form("capture"), next: str = Form("/session")):
    try:
        get_session().start_capture(label.strip() or "capture")
    except SessionUnavailable as e:
        return _session_back(next, str(e))
    return _session_back(next, t('Recording in progress'))


@app.post("/session/capture/stop")
async def capture_stop(next: str = Form("/session")):
    path = await get_session().stop_capture()
    return _session_back(next, t('Saved: {name}', name=path.name if path else '–'))


@app.get("/session/captures/{name}")
async def capture_download(name: str):
    p = get_settings().captures_dir / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/json", filename=p.name)


# ------------------------------------------------------------------ settings
SETTINGS_SECTIONS = ("willhaben", "shipping", "calendar", "purchase", "paylivery", "printers", "myads",
                     "claude", "rules", "chats", "ha", "telegram", "quiet", "backup", "language")   # settings.html groups


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str | None = None):
    return await settings_section(request, "willhaben", msg)


INBOX_KINDS = {"hit": "New hit", "price_drop": "Price drop", "message": "New message", "auto_reply": "Answered automatically",
               "auto_reply_pending": "Reply waiting for you", "ad_expiring": "Ad expiring soon", "ad_expired": "Ad expired",
               "ad_renewed": "Ad renewed", "paylivery": "Pay now!", "purchase": "Purchase order", "health": "Connections", "test": "Test"}


def _inbox_items(limit: int = 15) -> list[dict[str, Any]]:
    """The inbox popover (base.html): the latest events, unread = newer than the last "Mark all read"."""
    seen = _inbox_seen_at()
    out = []
    for e in recent_events(limit):
        ts = e.ts if e.ts.tzinfo else e.ts.replace(tzinfo=timezone.utc)
        out.append({"kind": "pay" if e.kind == "paylivery" else e.kind, "title": t(INBOX_KINDS.get(e.kind, "Notification")),
                    "text": e.message, "time": _ago(ts), "url": e.url, "unread": ts > seen})
    return out


CONTRIBUTORS_URL = "https://api.github.com/repos/lumascet/willhaben-agent/contributors"
_contributors_cache: dict[str, Any] = {"at": 0.0, "list": None}


async def _contributors() -> list[dict[str, Any]] | None:
    """The repository's contributors from the GitHub API (public, no token), without the author and bots;
    cached for a day. None when GitHub cannot be reached and nothing is cached."""
    import time

    if _contributors_cache["list"] is not None and time.time() - _contributors_cache["at"] < 86400:
        return _contributors_cache["list"]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(CONTRIBUTORS_URL, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("GitHub contributors not available: %s", e)
        return _contributors_cache["list"]
    people = [{"login": c.get("login") or "", "html_url": c.get("html_url") or "", "avatar_url": c.get("avatar_url") or "",
               "contributions": int(c.get("contributions") or 0)}
              for c in data if isinstance(c, dict) and c.get("type") == "User" and not str(c.get("login", "")).endswith("[bot]")
              and str(c.get("login", "")).lower() != AUTHOR.lower()]
    _contributors_cache.update(at=time.time(), list=people)
    return people


@app.get("/credits", response_class=HTMLResponse)
async def credits_page(request: Request):
    people = await _contributors()
    return render(request, "credits.html", **({"contributors": people} if people is not None else {}))


@app.post("/inbox/read-all")
async def inbox_read_all(request: Request):
    set_setting("inbox_seen_at", utcnow().isoformat())
    ref = request.headers.get("referer") or "/"
    return back(ref if ref.startswith(str(request.base_url)) or ref.startswith("/") else "/")


def _claude_section() -> dict[str, Any]:
    """Context of settings/claude.html (handoff design): models per task, the monthly limit, usage per task and model."""
    from app.llm.base import last_call

    lim = llm_usage.limit()
    rows = []
    fmt = lambda n: "{:,}".format(n or 0).replace(",", ".")   # noqa: E731
    for g in (llm_usage.summary().get("groups") or []):
        rows.append({"label": t(PURPOSES[g["purpose"]][1]) if g["purpose"] in PURPOSES else t("Other (tests, misc.)"),
                     "model": g["model"], "calls": g["calls"], "tokens_in": fmt(g["input"]), "tokens_out": fmt(g["output"]),
                     "cost": g["cost_usd"]})
    return {"models": {p: model_for(p).split("-")[1] for p in PURPOSES}, "claude_usage": {"used": lim["used"], "limit": lim["limit"]},
            "claude_limit_mode": lim["mode"], "token_usage": rows,
            **({"claude_last_call": last_call["at"]} if last_call.get("at") else {})}


@app.post("/settings/claude")
async def settings_claude(request: Request):
    """Model per task and the monthly token limit (Settings → Claude & usage)."""
    form = await request.form()
    ids = {m[0] for m in MODELS}
    short = {m[0].split("-")[1]: m[0] for m in MODELS}         # haiku / sonnet / opus / fable -> model id
    for purpose, (key, _label, _hint) in PURPOSES.items():
        m = str(form.get(f"model_{purpose}") or "")
        m = short.get(m, m)
        if m in ids:
            set_setting(key, m)
    try:
        lim = min(max(0.0, float(str(form.get("claude_token_limit_m") or "0").replace(",", "."))), 1000.0)
    except ValueError:
        lim = 0.0
    set_setting("claude_token_limit_m", f"{lim:g}" if lim else "")
    if form.get("claude_limit_mode") in llm_usage.LIMIT_MODES:
        set_setting("claude_limit_mode", str(form.get("claude_limit_mode")))
    return back(f"/settings/claude?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/willhaben")
async def settings_willhaben(requests_per_hour_: int = Form(0, alias="requests_per_hour"), next: str = Form("")):
    """The hourly willhaben request budget (REQUESTS_PER_HOUR from .env until changed here; 0 = none). `next`: the page
    of the usage popover the slider was moved on."""
    n = min(max(0, requests_per_hour_), 2000)
    set_setting("requests_per_hour", str(n))
    willhaben_limiter().per_hour = n
    target = next if next.startswith("/") and not next.startswith("//") else "/settings/willhaben"
    sep = "&" if "?" in target else "?"
    return back(f"{target}{sep}msg={quote(t('Changes saved'), safe='')}")


@app.get("/settings/{section}", response_class=HTMLResponse)
async def settings_section(request: Request, section: str, msg: str | None = None):
    """One settings section per page (settings.html index + settings/<section>.html)."""
    if section not in SETTINGS_SECTIONS:
        raise HTTPException(404)
    keys = ["reply_rules", "auto_draft", "notify_messages", "chat_sync_limit", "chat_sync_since", "push_channel", "expiry_warn_days", "myads_expired_days", "auto_reply_rules", "send_verified",
            "reply_humanize", "reply_delay_min_s", "reply_delay_max_s", "reply_fast_min_s", "reply_fast_max_s"]
    return render(request, "settings.html", section=section, ship_sender_ok=shipping.sender_complete(), llm_ok=bool(claude_token()),
                  **(_claude_section() if section == "claude" else {}),
                  values={k: get_setting(k) for k in keys}, msg=msg, settings=get_settings(),
                  claude_source=token_source(), claude_login=get_login(),
                  purposes=[(p, label, hint, _alias(model_for(p))) for p, (_, label, hint) in PURPOSES.items()],
                  llm_usage=llm_usage.summary(), purpose_labels={p: label for p, (_, label, _h) in PURPOSES.items()},
                  ha=await ha_auth.status(), ha_url=ha_auth.ha_url(), ha_source=ha_auth.token_source(),
                  ha_service=ha_auth.notify_service(), quiet=quiet_hours.state(), quiet_cfg=quiet_hours.config(),
                  tg=telegram.status(), tg_kinds=telegram.kinds(), tg_qr=telegram.bot_qr_svg(), **(await _gcal_ctx() if section == "calendar" else {}),
                  ship=shipping.config(), ship_carriers=shipping.CARRIERS, ship_logins=shipping.login_status(),
                  geizhals_auto_specs=ad_creator.auto_specs(), human=human_pace.config(),
                  captures=[c.name for c in sorted(get_settings().captures_dir.glob("*.json"), reverse=True)[:30]],
                  label_form=label_fill.active(), session_next="/settings/willhaben", backup=backup_routes.panel(),
                  **label_buy_routes.panel(), **printing_routes.panel())


async def _gcal_ctx() -> dict[str, Any]:
    st, cals, err = gcal.status(), [], None
    if st["connected"]:
        try:
            cals = await gcal.calendars()
        except gcal.CalendarError as e:
            err = str(e)
    return {"gcal": st, "gcal_cfg": gcal.config(), "gcal_cals": cals, "gcal_error": err,
            "gcal_redirect": gcal.REDIRECT_URI}


@app.post("/lang")
async def set_language(request: Request, lang: str = Form("auto"), next: str = Form("")):
    set_setting("ui_lang", lang if lang in i18n.LANGS else "auto")
    if next.startswith("/") and not next.startswith("//"):   # e.g. the language panel in Settings
        return back(next)
    ref = request.headers.get("referer") or "/"
    return back(ref if ref.startswith(str(request.base_url)) or ref.startswith("/") else "/")


@app.post("/settings/quiet")
async def settings_quiet(enabled: str | None = Form(None), start: str = Form("01:00"), end: str = Form("07:00"),
                         mode: str = Form("slow"), interval_min: int = Form(60), jitter: str | None = Form(None),
                         breaks: str | None = Form(None), pace: str | None = Form(None)):
    quiet_hours.save(bool(enabled), start, end, mode, interval_min)
    if pace:      # the section's "Human-like timing" switches (Settings → Quiet hours)
        cur = human_pace.config()
        human_pace.save((cur["variance_pct"] or 30) if jitter else 0, bool(breaks))
        scheduler.reschedule_agents()
    return back(f"/settings/quiet?msg={quote(t('Quiet hours saved'), safe='')}")


@app.post("/settings")
async def settings_save(request: Request):
    """The general settings. Each section posts only its own fields (hidden "section"); without it, all of them."""
    form = await request.form()
    groups = {"rules": ("reply_rules",),
              "chats": ("auto_draft", "notify_messages", "chat_sync_limit", "chat_sync_since", "push_channel", "auto_reply_rules",
                        "reply_humanize", "reply_delay_min_s", "reply_delay_max_s", "reply_fast_min_s", "reply_fast_max_s"),
              "myads": ("expiry_warn_days", "myads_expired_days")}
    section = str(form.get("section") or "")
    todo = set(groups.get(section) or [k for g in groups.values() for k in g])

    def num(key: str, default: int) -> int:
        try:
            return int(str(form.get(key, default)))
        except ValueError:
            return default

    if "reply_rules" in todo:
        set_setting("reply_rules", str(form.get("reply_rules", "")).strip())
    if "auto_reply_rules" in todo:
        set_setting("auto_reply_rules", str(form.get("auto_reply_rules", "")).strip())
        set_setting("auto_draft", "1" if form.get("auto_draft") else "0")
        set_setting("notify_messages", "1" if form.get("notify_messages") else "0")
        set_setting("chat_sync_limit", str(min(max(1, num("chat_sync_limit", 10)), 50)))
        since = str(form.get("chat_sync_since", "")).strip()
        try:
            set_setting("chat_sync_since", date.fromisoformat(since).isoformat() if since else "")
        except ValueError:
            pass
        if form.get("push_channel") in PUSH_CHANNELS:
            set_setting("push_channel", str(form.get("push_channel")))
        set_setting("reply_humanize", "1" if form.get("reply_humanize") else "0")
        lo = min(max(0, num("reply_delay_min_s", 40)), 3600)
        set_setting("reply_delay_min_s", str(lo))
        set_setting("reply_delay_max_s", str(min(max(lo, num("reply_delay_max_s", 180)), 3 * 3600)))
        fast_lo = min(max(0, num("reply_fast_min_s", 5)), 300)
        set_setting("reply_fast_min_s", str(fast_lo))
        set_setting("reply_fast_max_s", str(min(max(fast_lo, num("reply_fast_max_s", 20)), 600)))
    if "expiry_warn_days" in todo:
        set_setting("expiry_warn_days", str(max(0, num("expiry_warn_days", 3))))
        set_setting("myads_expired_days", str(max(0, num("myads_expired_days", 30))))
    return back(f"/settings{'/' + section if section in groups else ''}?msg={quote(t('Changes saved'), safe='')}")


def _alias(model: str) -> str:
    # LLM_MODEL=opus / sonnet from .env -> the matching entry in the selector
    return {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}.get(model, model)


@app.post("/settings/model")
async def settings_model(purpose: str = Form(...), model: str = Form(...)):
    if purpose not in PURPOSES or model not in {m for m, _, _ in MODELS}:
        raise HTTPException(400, t("unknown model"))
    set_setting(PURPOSES[purpose][0], model)
    label = next(lbl for m, lbl, _ in MODELS if m == model)
    return back(f"/settings/claude?msg={quote(f'{t(PURPOSES[purpose][1])}: {label}', safe='')}")


@app.post("/settings/claude/test")
@app.post("/settings/test-llm")
async def settings_test_llm():
    try:
        r = await get_backend().complete(
            system="Antworte nur im JSON-Schema.", prompt="Sag 'ok' im Feld status.",
            schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"],
                    "additionalProperties": False})
        msg = t("Claude OK ({model}, {ms} ms): {status}", model=r.model, ms=r.duration_ms, status=r.data.get("status"))
    except LLMError as e:
        msg = t("Claude error: {error}", error=e)
    return back(f"/settings/claude?msg={quote(msg, safe='')}")


@app.get("/settings/claude/login")
@app.post("/settings/claude/start")
async def claude_login_start():
    try:
        await get_login().start()
    except RuntimeError as e:
        return back(f"/settings/claude?msg={quote(t('Claude login: {error}', error=e), safe='')}")
    return back("/settings/claude")


@app.post("/settings/claude/code")
async def claude_login_code(code: str = Form(...), attempt: str | None = Form(None)):
    if not await get_login().submit_code(code, attempt):
        return back(f"/settings/claude?msg={quote(t('Claude login failed: {error}', error=get_login().error), safe='')}")
    return await settings_test_llm()


@app.post("/settings/claude/cancel")
async def claude_login_cancel():
    await get_login().cancel()
    return back("/settings/claude")


@app.post("/settings/claude/logout")
async def claude_logout():
    forget_token()
    return back(f"/settings/claude?msg={quote(t('Claude sign-in removed'), safe='')}")


@app.post("/settings/ha/connect")
async def ha_connect(request: Request, ha_url: str = Form(...)):
    try:
        url = ha_auth.authorize_url(ha_url, str(request.base_url))
    except ha_auth.HAError as e:
        return back(f"/settings/ha?msg={quote(str(e), safe='')}")
    return RedirectResponse(url, status_code=303)


@app.get("/settings/ha/callback")
async def ha_callback(code: str | None = None, state: str = "", error: str | None = None):
    if not code:
        reason = error or t("cancelled")
        return back(f"/settings/ha?msg={quote(t('Home Assistant: {reason}', reason=reason), safe='')}")
    try:
        await ha_auth.finish(code, state)
    except ha_auth.HAError as e:
        return back(f"/settings/ha?msg={quote(t('Home Assistant: {reason}', reason=e), safe='')}")
    return back(f"/settings/ha?msg={quote(t('Home Assistant connected – now choose a push target'), safe='')}")


@app.post("/settings/ha/push")
async def ha_push(service: str = Form("")):
    if service and service != ha_auth.NO_PUSH and not re.fullmatch(r"[a-z0-9_]+", service):
        raise HTTPException(400, t("invalid service"))
    set_setting("ha_notify_service", service or ha_auth.NO_PUSH)
    return back(f"/settings/ha?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/ha/disconnect")
async def ha_disconnect():
    ha_auth.disconnect()
    return back(f"/settings/ha?msg={quote(t('Home Assistant disconnected (delete the token in HA under Profile → Security)'), safe='')}")


@app.post("/settings/test-chat")
async def settings_test_chat():
    from app.willhaben.account import AccountClient
    try:
        res = await AccountClient(get_session()).ping_chat()
        msg = t("Chat connection OK") if res.get("status") == "success" else \
            t("Chat connection: {error}", error=f"{res.get('status')} {res.get('code') or ''}")
    except (SessionUnavailable, NotLoggedIn) as e:
        msg = t("Chat connection: {error}", error=e)
    return back(f"/settings/chats?msg={quote(msg.strip(), safe='')}")


# ------------------------------------------------------------------ google calendar
def _gcal_back(msg: str) -> RedirectResponse:
    return back(f"/settings/calendar?msg={quote(msg, safe='')}")


@app.post("/settings/gcal/client")
async def gcal_client(client_id: str = Form(""), client_secret: str = Form(""), client_json: str = Form(""),
                      client_file: UploadFile | None = File(None)):
    if client_file is not None and client_file.filename:
        client_json = (await client_file.read()).decode("utf-8", "replace")
    try:
        gcal.save_client(client_id, client_secret, client_json)
    except gcal.CalendarError as e:
        return _gcal_back(str(e))
    return _gcal_back(t("Client saved – now connect your Google account"))


@app.get("/settings/gcal/authorize")
async def gcal_authorize():
    try:
        return RedirectResponse(gcal.authorize_url(), status_code=303)
    except gcal.CalendarError as e:
        return _gcal_back(str(e))


@app.post("/settings/gcal/code")
async def gcal_code(pasted: str = Form(...)):
    if pasted.strip().startswith("{"):      # the client JSON pasted into the wrong field: take it as the client
        try:
            gcal.save_client(client_json=pasted)
        except gcal.CalendarError as e:
            return _gcal_back(str(e))
        return _gcal_back(t("Client saved – now connect your Google account"))
    try:
        account = await gcal.finish(pasted)
    except gcal.CalendarError as e:
        return _gcal_back(t("Google Calendar: {error}", error=e))
    return _gcal_back(t("Google Calendar connected: {account}", account=account or "ok"))


@app.post("/settings/gcal")
async def gcal_save(request: Request):
    f = await request.form()
    try:
        gcal.save_config([str(c) for c in f.getlist("calendar")], str(f.get("target") or "primary"),
                         int(f.get("days") or 7), str(f.get("window_start") or "09:00"),
                         str(f.get("window_end") or "20:00"), int(f.get("duration") or 30),
                         bool(f.get("in_drafts")), bool(f.get("in_auto")), int(f.get("buffer") or 0))
    except ValueError:
        return _gcal_back(t("Invalid value"))
    return _gcal_back(t("Changes saved"))


@app.post("/settings/gcal/test")
async def gcal_test():
    if not gcal.connected():
        return _gcal_back(t("Google Calendar is not connected"))
    txt = await gcal.availability_text()
    return _gcal_back(t("Free times Claude sees:") + "\n" + txt if txt else t("Could not read the calendar (see log)"))


@app.post("/settings/gcal/disconnect")
async def gcal_disconnect():
    await gcal.disconnect()
    return _gcal_back(t("Google Calendar disconnected"))


@app.post("/chats/{conversation_id}/calendar")
async def chat_calendar_add(conversation_id: str, start: str = Form(...), minutes: int = Form(30),
                            location: str = Form(""), summary: str = Form("")):
    try:
        await meetups.add(conversation_id, start=start, minutes=minutes, location=location, summary=summary)
    except gcal.CalendarError as e:
        return back(f"/chats/{conversation_id}?msg={quote(str(e), safe='')}")
    return back(f"/chats/{conversation_id}?msg={quote(t('Added to your calendar'), safe='')}")


@app.post("/chats/{conversation_id}/calendar/find")
async def chat_calendar_find(conversation_id: str):
    try:
        m = await meetups.extract(conversation_id)
    except LLMError as e:
        return back(f"/chats/{conversation_id}?msg={quote(t('Draft failed: {error}', error=e), safe='')}")
    msg = "" if m else t("Claude found no agreed appointment in this chat")
    return back(f"/chats/{conversation_id}?msg={quote(msg, safe='')}#meetup")


@app.post("/chats/{conversation_id}/calendar/forget")
async def chat_calendar_forget(conversation_id: str):
    meetups.forget(conversation_id)
    return back(f"/chats/{conversation_id}")


# ------------------------------------------------------------------ shipping (parcel labels)
def _chat_back(conversation_id: str, msg: str = "") -> RedirectResponse:
    return back(f"/chats/{conversation_id}?msg={quote(msg, safe='')}#shipping")


def _shipment_or_404(shipment_id: int):
    s = shipping.get(shipment_id)
    if s is None:
        raise HTTPException(404)
    return s


@app.post("/settings/shipping")
async def settings_shipping(request: Request):
    f = await request.form()
    shipping.save_sender({k: str(f.get(k) or "") for k in shipping.ADDRESS_FIELDS})
    shipping.save_carriers([str(c) for c in f.getlist("carrier")])
    set_setting("ship_detect", "1" if f.get("detect") else "0")
    return back(f"/settings/shipping?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/shipping/auto")
async def settings_shipping_auto(request: Request):
    f = await request.form()
    shipping.save_auto({"on": f.get("on"), "max_price": f.get("max_price"), "need_sold": f.get("need_sold"),
                        "need_parcel": f.get("need_parcel"), "carriers": f.getlist("auto_carrier"),
                        "grace_min": f.get("grace_min"), "daily": f.get("daily")})
    return back(f"/settings/shipping?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/human-pace")
async def settings_human_pace(variance_pct: int = Form(30), breaks: str | None = Form(None)):
    human_pace.save(variance_pct, bool(breaks))
    scheduler.reschedule_agents()                # new timing applies from the next check on
    return back(f"/settings/quiet?msg={quote(t('Search agents now check irregularly'), safe='')}")


@app.post("/settings/listings")
async def settings_listings(geizhals_auto_specs: str | None = Form(None)):
    set_setting("geizhals_auto_specs", "1" if geizhals_auto_specs else "0")
    return back(f"/settings/rules?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/shipping/login/{carrier}")
async def settings_shipping_login(carrier: str, user: str = Form(""), password: str = Form("")):
    try:
        shipping.save_login(carrier, user, password)
    except shipping.ShippingError as e:
        return back(f"/settings/shipping?msg={quote(str(e), safe='')}")
    return back(f"/settings/shipping?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/shipping/open/{carrier}")
async def settings_shipping_open(carrier: str):
    try:
        msg = await label_fill.open_carrier(carrier)
    except shipping.ShippingError as e:
        return back(f"/settings/shipping?msg={quote(str(e), safe='')}")
    return back(f"/settings/shipping?remote=1&msg={quote(msg, safe='')}")   # shows the page in the overlay


@app.post("/chats/{conversation_id}/shipping/detect")
async def shipping_detect(conversation_id: str):
    try:
        s = await shipping.detect(conversation_id, force=True)
    except (LLMError, shipping.ShippingError) as e:
        return _chat_back(conversation_id, str(e))
    return _chat_back(conversation_id, "" if s and s.recipient else t("No complete address in this chat yet – enter it by hand"))


@app.post("/shipments/{shipment_id}")
async def shipment_save(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    f = await request.form()
    addr = shipping.clean_address({k: str(f.get(k) or "") for k in shipping.ADDRESS_FIELDS})
    if addr:
        shipping.update(shipment_id, recipient=addr)
    try:
        shipping.set_parcel(shipment_id, {k: f.get(k) for k in ("length_cm", "width_cm", "height_cm", "weight_kg", "contents")})
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Changes saved") if addr else t("Address incomplete: name, street, postcode and city are needed"))


@app.post("/shipments/{shipment_id}/option")
async def shipment_option(shipment_id: int, key: str = Form(...)):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.choose(shipment_id, key)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id)


@app.post("/shipments/{shipment_id}/label")
async def shipment_label(shipment_id: int, key: str = Form("")):
    s = _shipment_or_404(shipment_id)
    if s.label_path or s.status in ("buying", "printed", "sent", "discarded"):
        return _chat_back(s.conversation_id, t("This shipment already has a label or a purchase is running"))
    try:
        if key:
            shipping.choose(shipment_id, key)
        msg = await label_fill.start(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    # stay on the chat; ?remote=1 opens the remote-view overlay with the carrier's form
    return back(f"/chats/{s.conversation_id}?remote=1&msg={quote(msg, safe='')}#shipping")


@app.get("/shipments/{shipment_id}/back")
async def shipment_back(shipment_id: int):
    return _chat_back(_shipment_or_404(shipment_id).conversation_id)


@app.post("/session/label-fill")
async def session_label_fill(next: str = Form("/session")):
    try:
        msg = await label_fill.fill_current()
    except shipping.ShippingError as e:
        msg = str(e)
    return _session_back(next, msg)


@app.post("/shipments/{shipment_id}/status")
async def shipment_status(shipment_id: int, status: str = Form(...), tracking: str = Form("")):
    s = _shipment_or_404(shipment_id)
    if status not in ("draft", "labelled", "sent", "discarded"):
        raise HTTPException(400)
    if s.status == "buying" and shipment_id in shipping._running:
        return _chat_back(s.conversation_id, t("A label purchase is running – cancel it in the browser window first"))
    shipping.update(shipment_id, status=status, tracking=tracking.strip() or s.tracking)
    return _chat_back(s.conversation_id)


@app.get("/shipments/{shipment_id}/label.pdf")
async def shipment_label_file(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    if not s.label_path:
        raise HTTPException(404)
    p = get_settings().data_dir / "labels" / Path(s.label_path).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name)


@app.post("/shipments/{shipment_id}/paid")
async def shipment_paid(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.mark_paid(shipment_id, "manual")
        await shipping.after_paid(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Marked as paid"))


@app.post("/shipments/{shipment_id}/unpaid")
async def shipment_unpaid(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.unmark_paid(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Payment mark removed"))


@app.post("/shipments/{shipment_id}/approve")
async def shipment_approve(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.approve(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Buying the label – this takes a moment"))


@app.post("/shipments/{shipment_id}/decline")
async def shipment_decline(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.decline(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Label not bought"))


@app.post("/shipments/{shipment_id}/retry")
async def shipment_retry(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.retry(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Trying again"))


@app.post("/shipments/{shipment_id}/cancel-auto")
async def shipment_cancel_auto(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        await shipping.cancel_auto(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Automatic purchase cancelled – approve the label below"))


# ------------------------------------------------------------------ telegram
def _tg_back(msg: str) -> RedirectResponse:
    return back(f"/settings/telegram?msg={quote(msg, safe='')}")


@app.post("/settings/telegram/token")
async def settings_telegram_token(token: str = Form(...)):
    try:
        name = await telegram.save_token(token)
    except (telegram.TelegramError, httpx.HTTPError) as e:
        return _tg_back(t("Telegram: {error}", error=str(e)))
    return _tg_back(t("Bot @{name} connected – now link your chat", name=name))


@app.get("/settings/telegram/status")
async def settings_telegram_status():
    st = telegram.status()
    return {"linked": st["linked"], "chat_name": st["chat_name"], "candidates": st["candidates"]}


@app.post("/settings/telegram/link")
async def settings_telegram_link(chat_id: int = Form(...)):
    name = await telegram.link(chat_id)
    return _tg_back(t("linked with {name}", name=name) if name else t("This request has expired – write to the bot again."))


@app.post("/settings/telegram/kinds")
async def settings_telegram_kinds(request: Request):
    form = await request.form()
    set_setting("telegram_enabled", "1" if form.get("enabled") else "0")
    telegram.set_kinds([str(k) for k in form.getlist("kind")])
    return _tg_back(t("Changes saved"))


@app.post("/settings/telegram/relink")
async def settings_telegram_relink():
    telegram.unlink()
    return _tg_back(t("Chat unlinked – write to the bot from the chat that should get the notifications"))


@app.post("/settings/telegram/forget")
async def settings_telegram_forget():
    telegram.forget()
    return _tg_back(t("Telegram bot removed"))


@app.post("/settings/telegram/test")
async def settings_telegram_test():
    ok = await telegram.send("test", "willhaben Agent", t("Test notification"))
    return _tg_back(t("Test message sent") if ok else t("Telegram not linked or error (see log)"))


@app.post("/settings/ha/test")
@app.post("/settings/test-notify")
async def settings_test_notify():
    ok = await notify("test", "willhaben Agent", t("Test notification"), url=get_settings().public_url)
    msg = t("HA event sent") if ok else t("HA not configured or error (see log)")
    return back(f"/settings/ha?msg={quote(msg, safe='')}")


# ------------------------------------------------------------------ JSON API (for MCP)
def _hit_dict(h: Hit) -> dict[str, Any]:
    return {"id": h.id, "agent_id": h.agent_id, "ad_id": h.ad_id, "title": h.heading, "price": h.price,
            "price_display": h.price_display, "old_price": h.old_price, "location": h.location,
            "published_at": h.published_at.isoformat() if h.published_at else None, "url": h.url,
            "state": h.state, "image_url": h.image_url}


@app.get("/api/status")
async def api_status():
    with session() as db:
        unread = db.exec(select(func.coalesce(func.sum(Conversation.unread_count), 0))).one()
        new_hits = db.exec(select(func.count()).select_from(Hit).where(Hit.state == "new")).one()
    st = get_session().status()
    return {"session": {k: st[k] for k in ("started", "logged_in", "user", "error")}, "unread_messages": unread,
            "new_hits": new_hits}


@app.get("/api/agents")
async def api_agents():
    with session() as db:
        return [a.model_dump(mode="json") for a in db.exec(select(SearchAgent))]


@app.post("/api/agents")
async def api_agents_create(payload: dict[str, Any]):
    try:
        a = create_agent(payload["url"], payload.get("name"), payload.get("interval_s"))
    except (KeyError, InvalidSearchUrl) as e:
        raise HTTPException(400, str(e)) from e
    scheduler.schedule_agent(a)
    return a.model_dump(mode="json")


@app.get("/api/hits")
async def api_hits(agent_id: int | None = None, state: str = "new", limit: int = 50):
    with session() as db:
        q = select(Hit)
        if agent_id:
            q = q.where(Hit.agent_id == agent_id)
        if state != "all":
            q = q.where(Hit.state == state)
        return [_hit_dict(h) for h in db.exec(q.order_by(col(Hit.first_seen_at).desc()).limit(min(limit, 200)))]


@app.get("/api/search")
async def api_search(keyword: str, price_from: float | None = None, price_to: float | None = None,
                     rows: int = 20, sort: int = 1):
    try:
        page = await get_public().search_keyword(keyword, min(rows, 50), price_from, price_to, sort)
    except WillhabenError as e:
        raise HTTPException(502, str(e)) from e
    return {"rows_found": page.rows_found, "ads": [a.as_dict() for a in page.ads]}


@app.get("/api/ads/{ad_id}")
async def api_ad(ad_id: str):
    try:
        d = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        raise HTTPException(502, str(e)) from e
    if d is None:
        raise HTTPException(404, "ad is not online (anymore)")
    return d.as_context()


@app.get("/api/chats")
async def api_chats():
    with session() as db:
        return [c.model_dump(mode="json", exclude={"ad_context"})
                for c in db.exec(select(Conversation).where(col(Conversation.removed).is_not(True))
                                 .order_by(col(Conversation.last_message_at).desc().nulls_last()))]


@app.get("/api/chats/{conversation_id}")
async def api_chat(conversation_id: str):
    with session() as db:
        c = db.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(404)
    return {"conversation": c.model_dump(mode="json"),
            "messages": [m.model_dump(mode="json") for m in chats.messages_of(conversation_id)],
            "drafts": [d.model_dump(mode="json") for d in chats.drafts_of(conversation_id)]}


@app.post("/api/chats/{conversation_id}/draft")
async def api_chat_draft(conversation_id: str, payload: dict[str, Any] | None = None):
    try:
        d = await chats.draft_reply(conversation_id, (payload or {}).get("hint"))
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    return d.model_dump(mode="json")


@app.post("/api/chats/{conversation_id}/drafts")
async def api_chat_save_draft(conversation_id: str, payload: dict[str, Any]):
    """Store a reply written elsewhere (e.g. by Claude Desktop via MCP) as a draft for review."""
    text = (payload.get("text") or "").strip()
    with session() as db:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(404)
        if not text:
            raise HTTPException(400, "text is missing")
        d = Draft(conversation_id=conversation_id, text=text, intent=payload.get("intent"), model="mcp")
        db.add(d)
        db.commit()
        db.refresh(d)
    return d.model_dump(mode="json")


@app.post("/api/drafts/{draft_id}/send")
async def api_draft_send(draft_id: int, payload: dict[str, Any]):
    with session() as db:
        current = db.get(Draft, draft_id)
    if current is not None and current.status == "sending":    # queued from the dashboard: never twice
        return JSONResponse({"sent": False, "reason": "already being sent in the background"}, status_code=409)
    try:
        d = await chats.send_draft(draft_id, payload["text"])
    except (EndpointUnknown, SendFailed, ValueError) as e:
        return JSONResponse({"sent": False, "reason": str(e)}, status_code=409)
    except (NotLoggedIn, SessionUnavailable) as e:
        return JSONResponse({"sent": False, "reason": str(e)}, status_code=503)
    return {"sent": True, "draft": d.model_dump(mode="json")}


@app.get("/api/myads")
async def api_myads():
    with session() as db:
        return [a.model_dump(mode="json") for a in db.exec(select(MyAd))]


@app.post("/api/myads")
async def api_myads_add(payload: dict[str, Any]):
    try:
        row = my_ads.add(payload["ref"], bool(payload.get("auto_renew")))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    row = await my_ads.check(row.ad_id) or row
    return row.model_dump(mode="json")


@app.post("/api/ad-drafts")
async def api_ad_draft(payload: dict[str, Any]):
    d = await ad_creator.create_draft(payload.get("notes", ""), [])
    return d.model_dump(mode="json")


@app.get("/api/myads/{ad_id}/editor-json")
async def api_myad_editor_json(ad_id: str):
    """Read-only: an own ad as willhaben's editor sees it (reference for the insertion format)."""
    from app.willhaben import insert
    with session() as db:
        if db.get(MyAd, ad_id) is None:
            raise HTTPException(404, "not one of my ads")
    try:
        return await insert.get_own_ad(get_session(), ad_id)
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        raise HTTPException(503, str(e)) from None


@app.get("/api/ad-drafts/{draft_id}/willhaben-plan")
async def api_ad_draft_plan(draft_id: int):
    """Read-only: what willhaben's insertion form suggests for this draft (category, required fields)."""
    from app.willhaben import insert
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None:
        raise HTTPException(404)
    sess = get_session()
    try:
        form = await insert.load_form(sess)
        raw = await insert.suggest_categories(sess, d.title or d.input_text[:80])
    except (NotLoggedIn, SessionUnavailable) as e:
        raise HTTPException(503, str(e)) from None
    cats = insert.category_index(form.get("categoryTree") or {})
    return {"title": d.title, "suggestions": raw, "categories": len(cats),
            "template": {k: v for k, v in (form.get("ad") or {}).items() if k in ("postCode", "location", "locationId", "countryId", "hidePhoneNo")},
            "prefill": form.get("prefillAttributes")}


@app.post("/api/ad-drafts/{draft_id}/willhaben-publish")
async def api_ad_draft_publish(draft_id: int, payload: dict[str, Any]):
    """Publish (or with stop_after a step-by-step trial up to presave/photos/save/cart) as free private ad.
    Returns the structure of willhaben's responses, without personal data."""
    from app.willhaben.insert import STEPS, InsertError

    _draft_or_404(draft_id)
    sh = payload.get("shipping")
    if sh is not None and not isinstance(sh, dict):
        raise HTTPException(400, "shipping must be an object")
    stop = payload.get("stop_after")
    if stop is not None and stop not in STEPS:
        raise HTTPException(400, f"stop_after must be one of {STEPS}")
    if payload.get("prepare") or not (_draft_or_404(draft_id).wh_plan or {}).get("options"):
        await ad_creator.prepare_willhaben(draft_id)
    try:
        res = await ad_creator.publish_willhaben(draft_id, int(payload.get("option", 0)), payload.get("values") or {},
                                                 stop_after=stop, shipping=ad_creator.parse_shipping(sh or {}))
    except (InsertError, NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        d = ad_creator.update_draft(draft_id, error=t("Publishing failed: {error}", error=str(e)))
        return JSONResponse({"ok": False, "error": str(e), "wh_ad_id": d.wh_ad_id}, status_code=502)
    if isinstance(res, dict):
        return {"ok": True, **res}
    return {"ok": True, "published": res.status == "published", "wh_ad_id": res.wh_ad_id}


@app.get("/api/convert-url")
async def api_convert(url: str):
    try:
        return {"webapi_url": to_webapi_url(url)}
    except InvalidSearchUrl as e:
        raise HTTPException(400, str(e)) from e


from app import deals_web  # noqa: E402  Produktsuche tab (/deals) + price section of the ad viewer
deals_web.install(app, templates)
from app import bugreport_routes  # noqa: E402  footer "Report a bug" (/bugreport), log ring buffer, unhandled-error log
bugreport_routes.install(app)
from app import stats_web  # noqa: E402  willhaben access statistics (/stats/willhaben, footer counter)
stats_web.install(app, templates)
