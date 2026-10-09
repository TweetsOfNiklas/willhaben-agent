"""FastAPI app: HTMX dashboard + JSON API (used by the MCP server)."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import col, func, select
from starlette.middleware.base import BaseHTTPMiddleware

from app import AUTHOR, __version__, i18n, scheduler
from app.i18n import t
from app.config import get_settings
from app.db import (
    AdBatch, AdDraft, Conversation, Draft, Hit, MyAd, SearchAgent, get_setting, init_db, recent_events,
    session, set_setting, utcnow,
)
from app.llm.base import MODELS, PURPOSES, LLMError, get_backend, model_for
from app.llm.claude_login import claude_token, forget_token, get_login, token_source
from app.services import ad_batch, ad_creator, chats, my_ads, screening
from app.services import gcal, geizhals, ha_auth, label_fill, meetups, quiet_hours, shipping, telegram
from app.services.notify import PUSH_CHANNELS, notify
from app.services.search_agents import create_agent, run_agent
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
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    scheduler.shutdown()
    await get_session().stop()
    await get_public().aclose()


_background: set[asyncio.Task] = set()
app = FastAPI(title="willhaben Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class BasicAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        if s.dashboard_password and request.url.path != "/healthz":
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user, s.dashboard_user) and secrets.compare_digest(pw, s.dashboard_password)
                except ValueError:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="willhaben-agent"'})
        return await call_next(request)


app.add_middleware(BasicAuth)


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
templates.env.globals.update(deal_label=screening.deal_label)   # Claude verdict on hit cards
templates.env.globals.update(_=t, ui_lang=i18n.lang, ui_langs=i18n.LANGS, ui_lang_pref=i18n.preference,
                             app_version=__version__, app_author=AUTHOR, wh_limiter=willhaben_limiter,
                             wh_budget=lambda: get_settings().requests_per_hour)


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("session_status", get_session().status())
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


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
            "expired": db.exec(select(func.count()).select_from(MyAd).where(MyAd.status == "expired")).one(),
        }
        soon = list(db.exec(select(MyAd).where(MyAd.status == "active", col(MyAd.end_date).is_not(None))
                            .order_by(MyAd.end_date).limit(5)))
        latest = list(db.exec(select(Hit).where(Hit.state == "new").order_by(col(Hit.first_seen_at).desc()).limit(8)))
    return render(request, "index.html", stats=stats, soon=soon, latest=latest, events=recent_events(20),
                  llm_ok=bool(claude_token()))


# ------------------------------------------------------------------ search agents
@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, error: str | None = None):
    with session() as db:
        agents = list(db.exec(select(SearchAgent).order_by(SearchAgent.created_at)))
        counts = dict(db.exec(select(Hit.agent_id, func.count()).where(Hit.state == "new").group_by(Hit.agent_id)).all())
        thumbs: dict[int, list[Hit]] = {}
        for h in db.exec(select(Hit).where(col(Hit.image_url).is_not(None)).order_by(col(Hit.first_seen_at).desc())):
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
                        screen_on: str | None = Form(None), screen_brief: str = Form(""), screen_min: str = Form("match")):
    try:
        agent = create_agent(url, name or None, interval_min * 60)
    except InvalidSearchUrl as e:
        return back(f"/agents?error={e}")
    if screen_on:
        agent = _set_screening(agent.id, True, screen_brief, screen_min)
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
                      screen_on: str | None = Form(None), screen_brief: str = Form(""), screen_min: str = Form("match")):
    s = get_settings()
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        a.name = name.strip() or a.name
        a.interval_s = max(interval_min * 60, s.min_agent_interval_s)
        a.notify, a.notify_price_drops = bool(notify_on), bool(drops_on)
        db.add(a)
        db.commit()
        db.refresh(a)
    a = _set_screening(agent_id, bool(screen_on), screen_brief, screen_min)
    scheduler.schedule_agent(a, run_soon=False)
    return back("/agents")


def _set_screening(agent_id: int, on: bool, brief: str, minimum: str) -> SearchAgent:
    """Claude screening of new hits (see app/services/screening.py)."""
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        a.screen = on
        a.screen_brief = brief.replace("\r\n", "\n").strip() or None
        a.screen_min = minimum if minimum in screening.MIN_RANK else "match"
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
    return render(request, "hits.html", hits=hits, agents=agents, agent_id=agent_id, state=state)


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
@app.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, msg: str | None = None):
    with session() as db:
        convs = list(db.exec(select(Conversation).order_by(col(Conversation.last_message_at).desc().nulls_last())))
        open_drafts = set(db.exec(select(Draft.conversation_id).where(Draft.status == "draft")).all())
    return render(request, "chats.html", convs=convs, open_drafts=open_drafts, msg=msg)


@app.post("/chats/sync")
async def chats_sync():
    try:
        res = await chats.sync(force=True)
    except (SessionUnavailable, NotLoggedIn, RuntimeError) as e:
        return back(f"/chats?msg={quote(t('Sync failed: {error}', error=e), safe='')}")
    msg = res.get("skipped") or t("{n} conversations, {changed} updated", n=res["conversations"], changed=res["changed"])
    return back(f"/chats?msg={quote(msg, safe='')}")


@app.get("/chats/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: str, msg: str | None = None):
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
                  drafts=drafts, msg=msg, meetup=meetups.current(conv, drafts), gcal_on=gcal.connected(),
                  gcal_duration=gcal.config()["duration"], **_shipping_ctx(conversation_id, conv))


def _shipping_ctx(conversation_id: str, conv: Conversation) -> dict[str, Any]:
    s = shipping.open_for(conversation_id)
    return {"shipment": s, "ship_options": shipping.quote(s.parcel) if s else [],
            "ship_option": shipping.by_key(s.option) if s else None, "ship_on": conv.role == "seller",
            "ship_sender_ok": shipping.sender_complete()}


@app.post("/chats/{conversation_id}/refresh")
async def chat_refresh(conversation_id: str):
    try:
        mode = await chats.sync_messages(conversation_id)
        await chats.ensure_ad_context(conversation_id)
    except (SessionUnavailable, NotLoggedIn, WillhabenError) as e:
        return back(f"/chats/{conversation_id}?msg={e}")
    note = "" if mode == "full" else t("Only the last message is available (messages endpoint not verified yet).")
    return back(f"/chats/{conversation_id}?msg={quote(note, safe='')}")


@app.post("/chats/{conversation_id}/draft", response_class=HTMLResponse)
async def chat_draft(request: Request, conversation_id: str, hint: str = Form("")):
    try:
        await chats.draft_reply(conversation_id, hint.strip() or None)
    except (LLMError, WillhabenError) as e:
        return back(f"/chats/{conversation_id}?msg={quote(t('Draft failed: {error}', error=e), safe='')}")
    return back(f"/chats/{conversation_id}")


@app.post("/drafts/{draft_id}/send")
async def draft_send(draft_id: int, text: str = Form(...)):
    with session() as db:
        d = db.get(Draft, draft_id)
    try:
        await chats.send_draft(draft_id, text)
    except (EndpointUnknown, SendFailed, NotLoggedIn, SessionUnavailable, ValueError) as e:
        return back(f"/chats/{d.conversation_id}?msg={quote(str(e), safe='')}")
    return back(f"/chats/{d.conversation_id}?msg={quote(t('Sent – confirmed in the willhaben history'), safe='')}")


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
@app.get("/myads", response_class=HTMLResponse)
async def myads_page(request: Request, msg: str | None = None):
    with session() as db:
        empty = db.exec(select(MyAd).limit(1)).first() is None
    if empty and get_session().logged_in:
        try:
            await my_ads.import_from_account()
        except (NotLoggedIn, SessionUnavailable, ValueError) as e:
            msg = msg or t("Could not load ads: {error}", error=e)
    with session() as db:
        ads = list(db.exec(select(MyAd).order_by(col(MyAd.end_date).asc().nulls_last())))
    return render(request, "myads.html", ads=ads, msg=msg, myadverts_url=my_ads.MYADVERTS_URL)


@app.post("/myads/check")
async def myads_check_all():
    res = await my_ads.check_all()
    msg = t("{checked} checked, {warned} warnings, {reactivated} reactivations",
            checked=res["checked"], warned=res["warned"], reactivated=res["reactivation"])
    if res["imported"]:
        msg = t("{n} loaded from willhaben", n=res["imported"]) + ", " + msg
    return back(f"/myads?msg={quote(msg, safe='')}")


@app.post("/myads/{ad_id}/auto-renew")
async def myads_auto(ad_id: str, on: str = Form("0")):
    my_ads.set_auto_renew(ad_id, on == "1")
    return back("/myads")


@app.post("/myads/{ad_id}/auto-reply")
async def myads_auto_reply(ad_id: str, on: str | None = Form(None), instructions: str = Form("")):
    my_ads.set_auto_reply(ad_id, bool(on), instructions)
    if on:  # answer buyers who are already waiting, in the background (Claude takes a few seconds)
        task = asyncio.create_task(chats.process_pending_auto(ad_id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    return back(f"/myads?msg={quote(t('Automatic replies on') if on else t('Automatic replies off'), safe='')}#ad-{ad_id}")


@app.post("/myads/{ad_id}/delete")
async def myads_delete(ad_id: str):
    my_ads.remove(ad_id)
    return back("/myads")


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
    if instruction.strip():
        await ad_creator.refine_draft(draft_id, instruction)
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
    d = await ad_creator.refine_draft(draft_id, instruction) if instruction.strip() else _draft_or_404(draft_id)
    return _draft_card(request, d)


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
    """Publish as free private ad on willhaben (the user clicked; the chosen category and field values come along)."""
    from app.willhaben.insert import InsertError

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
    try:
        d = await ad_creator.publish_willhaben(draft_id, option, values)
    except (InsertError, NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        log.warning("publishing draft %s failed: %s", draft_id, e)
        d = ad_creator.update_draft(draft_id, error=t("Publishing failed: {error}", error=str(e)))
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
@app.get("/session", response_class=HTMLResponse)
async def session_page(request: Request, msg: str | None = None):
    caps = sorted(get_settings().captures_dir.glob("*.json"), reverse=True)[:30]
    return render(request, "session.html", captures=[c.name for c in caps], msg=msg, settings=get_settings(),
                  label_form=label_fill.active())


@app.post("/session/start")
async def session_start():
    sess = get_session()
    if not sess.started:
        await sess.start()
    return back("/session")


@app.post("/session/login")
async def session_login():
    try:
        await get_session().open_login()
    except SessionUnavailable as e:
        return back(f"/session?msg={e}")
    return back(f"/session?msg={quote(t('Login page opened in the browser window'), safe='')}")


@app.post("/session/check")
async def session_check():
    ok = await get_session().check_login() if get_session().started else False
    return back(f"/session?msg={quote(t('Logged in') if ok else t('Not logged in'), safe='')}")


@app.post("/session/open")
async def session_open(url: str = Form(...)):
    if not url.startswith("https://www.willhaben.at/"):
        return back(f"/session?msg={quote(t('Only willhaben.at URLs'), safe='')}")
    await get_session().open_url(url)
    return back("/session")


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
async def capture_start(label: str = Form("capture")):
    try:
        get_session().start_capture(label.strip() or "capture")
    except SessionUnavailable as e:
        return back(f"/session?msg={e}")
    return back(f"/session?msg={quote(t('Recording in progress'), safe='')}")


@app.post("/session/capture/stop")
async def capture_stop():
    path = await get_session().stop_capture()
    return back(f"/session?msg={quote(t('Saved: {name}', name=path.name if path else '–'), safe='')}")


@app.get("/session/captures/{name}")
async def capture_download(name: str):
    p = get_settings().captures_dir / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/json", filename=p.name)


# ------------------------------------------------------------------ settings
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str | None = None):
    keys = ["reply_rules", "auto_draft", "notify_messages", "push_channel", "expiry_warn_days", "auto_reply_rules", "send_verified",
            "reply_humanize", "reply_delay_min_s", "reply_delay_max_s", "reply_fast_min_s", "reply_fast_max_s"]
    return render(request, "settings.html", values={k: get_setting(k) for k in keys}, msg=msg, settings=get_settings(),
                  claude_source=token_source(), claude_login=get_login(), models=MODELS,
                  purposes=[(p, label, hint, _alias(model_for(p))) for p, (_, label, hint) in PURPOSES.items()],
                  ha=await ha_auth.status(), ha_url=ha_auth.ha_url(), ha_source=ha_auth.token_source(),
                  ha_service=ha_auth.notify_service(), quiet=quiet_hours.state(), quiet_cfg=quiet_hours.config(),
                  tg=telegram.status(), tg_kinds=telegram.kinds(), **await _gcal_ctx(),
                  ship=shipping.config(), ship_carriers=shipping.CARRIERS)


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
                         mode: str = Form("slow"), interval_min: int = Form(60)):
    quiet_hours.save(bool(enabled), start, end, mode, interval_min)
    return back(f"/settings?msg={quote(t('Quiet hours saved'), safe='')}#quiet")


@app.post("/settings")
async def settings_save(reply_rules: str = Form(""), auto_draft: str | None = Form(None),
                        notify_messages: str | None = Form(None), expiry_warn_days: int = Form(3),
                        push_channel: str = Form(""),
                        auto_reply_rules: str = Form(""), reply_humanize: str | None = Form(None),
                        reply_delay_min_s: int = Form(40), reply_delay_max_s: int = Form(180),
                        reply_fast_min_s: int = Form(5), reply_fast_max_s: int = Form(20)):
    set_setting("reply_rules", reply_rules.strip())
    set_setting("auto_reply_rules", auto_reply_rules.strip())
    set_setting("auto_draft", "1" if auto_draft else "0")
    set_setting("notify_messages", "1" if notify_messages else "0")
    if push_channel in PUSH_CHANNELS:
        set_setting("push_channel", push_channel)
    set_setting("expiry_warn_days", str(max(0, expiry_warn_days)))
    set_setting("reply_humanize", "1" if reply_humanize else "0")
    lo = min(max(0, reply_delay_min_s), 3600)
    set_setting("reply_delay_min_s", str(lo))
    set_setting("reply_delay_max_s", str(min(max(lo, reply_delay_max_s), 3 * 3600)))
    fast_lo = min(max(0, reply_fast_min_s), 300)
    set_setting("reply_fast_min_s", str(fast_lo))
    set_setting("reply_fast_max_s", str(min(max(fast_lo, reply_fast_max_s), 600)))
    return back(f"/settings?msg={quote(t('Changes saved'), safe='')}")


def _alias(model: str) -> str:
    # LLM_MODEL=opus / sonnet from .env -> the matching entry in the selector
    return {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}.get(model, model)


@app.post("/settings/model")
async def settings_model(purpose: str = Form(...), model: str = Form(...)):
    if purpose not in PURPOSES or model not in {m for m, _, _ in MODELS}:
        raise HTTPException(400, t("unknown model"))
    set_setting(PURPOSES[purpose][0], model)
    label = next(lbl for m, lbl, _ in MODELS if m == model)
    return back(f"/settings?msg={quote(f'{t(PURPOSES[purpose][1])}: {label}', safe='')}#claude")


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
    return back(f"/settings?msg={quote(msg, safe='')}")


@app.post("/settings/claude/start")
async def claude_login_start():
    try:
        await get_login().start()
    except RuntimeError as e:
        return back(f"/settings?msg={quote(t('Claude login: {error}', error=e), safe='')}#claude")
    return back("/settings#claude")


@app.post("/settings/claude/code")
async def claude_login_code(code: str = Form(...), attempt: str | None = Form(None)):
    if not await get_login().submit_code(code, attempt):
        return back(f"/settings?msg={quote(t('Claude login failed: {error}', error=get_login().error), safe='')}#claude")
    return await settings_test_llm()


@app.post("/settings/claude/cancel")
async def claude_login_cancel():
    await get_login().cancel()
    return back("/settings#claude")


@app.post("/settings/claude/logout")
async def claude_logout():
    forget_token()
    return back(f"/settings?msg={quote(t('Claude sign-in removed'), safe='')}#claude")


@app.post("/settings/ha/connect")
async def ha_connect(request: Request, ha_url: str = Form(...)):
    try:
        url = ha_auth.authorize_url(ha_url, str(request.base_url))
    except ha_auth.HAError as e:
        return back(f"/settings?msg={quote(str(e), safe='')}#ha")
    return RedirectResponse(url, status_code=303)


@app.get("/settings/ha/callback")
async def ha_callback(code: str | None = None, state: str = "", error: str | None = None):
    if not code:
        reason = error or t("cancelled")
        return back(f"/settings?msg={quote(t('Home Assistant: {reason}', reason=reason), safe='')}#ha")
    try:
        await ha_auth.finish(code, state)
    except ha_auth.HAError as e:
        return back(f"/settings?msg={quote(t('Home Assistant: {reason}', reason=e), safe='')}#ha")
    return back(f"/settings?msg={quote(t('Home Assistant connected – now choose a push target'), safe='')}#ha")


@app.post("/settings/ha/push")
async def ha_push(service: str = Form("")):
    if service and service != ha_auth.NO_PUSH and not re.fullmatch(r"[a-z0-9_]+", service):
        raise HTTPException(400, t("invalid service"))
    set_setting("ha_notify_service", service or ha_auth.NO_PUSH)
    return back(f"/settings?msg={quote(t('Changes saved'), safe='')}#ha")


@app.post("/settings/ha/disconnect")
async def ha_disconnect():
    ha_auth.disconnect()
    return back(f"/settings?msg={quote(t('Home Assistant disconnected (delete the token in HA under Profile → Security)'), safe='')}#ha")


@app.post("/settings/test-chat")
async def settings_test_chat():
    from app.willhaben.account import AccountClient
    try:
        res = await AccountClient(get_session()).ping_chat()
        msg = t("Chat connection OK") if res.get("status") == "success" else \
            t("Chat connection: {error}", error=f"{res.get('status')} {res.get('code') or ''}")
    except (SessionUnavailable, NotLoggedIn) as e:
        msg = t("Chat connection: {error}", error=e)
    return back(f"/settings?msg={quote(msg.strip(), safe='')}#chats")


# ------------------------------------------------------------------ google calendar
def _gcal_back(msg: str) -> RedirectResponse:
    return back(f"/settings?msg={quote(msg, safe='')}#calendar")


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
                         bool(f.get("in_drafts")), bool(f.get("in_auto")))
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
    return back(f"/settings?msg={quote(t('Changes saved'), safe='')}#shipping")


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
    try:
        if key:
            shipping.choose(shipment_id, key)
        msg = await label_fill.start(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return back(f"/session?msg={quote(msg, safe='')}")


@app.get("/shipments/{shipment_id}/back")
async def shipment_back(shipment_id: int):
    return _chat_back(_shipment_or_404(shipment_id).conversation_id)


@app.post("/session/label-fill")
async def session_label_fill():
    try:
        msg = await label_fill.fill_current()
    except shipping.ShippingError as e:
        msg = str(e)
    return back(f"/session?msg={quote(msg, safe='')}")


@app.post("/shipments/{shipment_id}/status")
async def shipment_status(shipment_id: int, status: str = Form(...), tracking: str = Form("")):
    s = _shipment_or_404(shipment_id)
    if status not in ("draft", "labelled", "sent", "discarded"):
        raise HTTPException(400)
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


# ------------------------------------------------------------------ telegram
def _tg_back(msg: str) -> RedirectResponse:
    return back(f"/settings?msg={quote(msg, safe='')}#telegram")


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
    return {"linked": st["linked"], "chat_name": st["chat_name"]}


@app.post("/settings/telegram/kinds")
async def settings_telegram_kinds(request: Request):
    form = await request.form()
    set_setting("telegram_enabled", "1" if form.get("enabled") else "0")
    telegram.set_kinds([str(k) for k in form.getlist("kind")])
    return _tg_back(t("Changes saved"))


@app.post("/settings/telegram/relink")
async def settings_telegram_relink():
    telegram.unlink()
    return _tg_back(t("Chat unlinked – link it again with the button"))


@app.post("/settings/telegram/forget")
async def settings_telegram_forget():
    telegram.forget()
    return _tg_back(t("Telegram bot removed"))


@app.post("/settings/telegram/test")
async def settings_telegram_test():
    ok = await telegram.send("test", "willhaben Agent", t("Test notification"))
    return _tg_back(t("Test message sent") if ok else t("Telegram not linked or error (see log)"))


@app.post("/settings/test-notify")
async def settings_test_notify():
    ok = await notify("test", "willhaben Agent", t("Test notification"), url=get_settings().public_url)
    msg = t("HA event sent") if ok else t("HA not configured or error (see log)")
    return back(f"/settings?msg={quote(msg, safe='')}#ha")


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
                for c in db.exec(select(Conversation).order_by(col(Conversation.last_message_at).desc().nulls_last()))]


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
    stop = payload.get("stop_after")
    if stop is not None and stop not in STEPS:
        raise HTTPException(400, f"stop_after must be one of {STEPS}")
    if payload.get("prepare") or not (_draft_or_404(draft_id).wh_plan or {}).get("options"):
        await ad_creator.prepare_willhaben(draft_id)
    try:
        res = await ad_creator.publish_willhaben(draft_id, int(payload.get("option", 0)), payload.get("values") or {},
                                                 stop_after=stop)
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
