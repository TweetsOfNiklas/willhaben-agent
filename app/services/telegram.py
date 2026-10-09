"""Telegram bot: a second push channel next to Home Assistant, with buttons.

Long polling (getUpdates), so the server needs no public URL. The bot answers exactly one chat: the one
that sent `/start <code>` with the one-time code shown in Settings. Updates from any other chat are ignored.

Buttons carry a short random token (`t:<token>`, table TelegramAction) instead of the action itself:
  D_SEND / D_REPLY / D_REVISE / D_DISCARD  held-back answer -> ha_actions.handle (the one send path)
  D_CAL                                     agreed appointment -> Google Calendar (ha_actions.handle)
  D_SHIP                                    address received -> fill the carrier's label form (ha_actions.handle)
  C_DRAFT / C_REPLY                         new chat message -> Claude draft / answer typed in Telegram
  F_UP / F_DOWN                             feedback on an automatic answer (👎 asks for a note)
  A_ON / A_OFF                              automatic answers for one own ad
  H_SAVE / H_IGNORE                         search hit: keep / ignore
Buttons that need text ("Direkte Antwort", "Mit Claude überarbeiten", 👎 note) send a ForceReply prompt;
the user's reply to it is the text.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any

import httpx
from sqlmodel import col, delete, select

from app.config import get_settings
from app.db import Conversation, Draft, Hit, MyAd, TelegramAction, TelegramMessage, get_setting, session, set_setting, utcnow
from app.i18n import t

log = logging.getLogger(__name__)


class _HideBotToken(logging.Filter):
    """The Bot API has the token in the URL and httpx logs every request URL at INFO: drop those lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "api.telegram.org/bot" not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideBotToken())

API = "https://api.telegram.org"
_transport: httpx.AsyncBaseTransport | None = None   # tests inject a mock transport
_tasks: set[asyncio.Task] = set()
_pending: dict[int, tuple[str, str, float]] = {}     # ForceReply prompt message id -> (verb, ref, time)
PENDING_TTL = 30 * 60


def kinds() -> dict[str, str]:
    """Notification kinds that can go to Telegram (event kind -> label)."""
    return {
        "message": t("New chat messages"),
        "auto_reply_pending": t("Answers waiting for approval"),
        "auto_reply": t("Automatic answers sent"),
        "hit": t("New search hits"),
        "price_drop": t("Price drops"),
        "ad_expiring": t("Own ads about to expire"),
        "ad_expired": t("Own ads expired"),
        "ad_renewed": t("Own ads renewed automatically"),
        "meetup": t("Appointments for the calendar"),
        "shipping": t("Shipping addresses and labels"),
    }


class TelegramError(RuntimeError):
    pass


# ------------------------------------------------------------------ config
def _token_file():
    return get_settings().data_dir / "telegram-token"


def token() -> str:
    try:
        tok = _token_file().read_text().strip()
    except OSError:
        tok = ""
    return tok or get_settings().telegram_bot_token


def token_source() -> str | None:
    if _token_file().exists():
        return "login"
    return "env" if get_settings().telegram_bot_token else None


def chat_id() -> int | None:
    v = get_setting("telegram_chat_id")
    return int(v) if v.lstrip("-").isdigit() else None


def enabled_kinds() -> set[str]:
    raw = get_setting("telegram_kinds")
    return set(kinds()) if not raw else {k for k in raw.split(",") if k in kinds()}   # "-" = none chosen


def set_kinds(selected: list[str]) -> None:
    set_setting("telegram_kinds", ",".join(k for k in selected if k in kinds()) or "-")


def active() -> bool:
    return bool(token() and chat_id() and get_setting("telegram_enabled") != "0")


def link_url() -> str | None:
    user, code = get_setting("telegram_username"), get_setting("telegram_link_code")
    return f"https://t.me/{user}?start={code}" if user and code else None


def status() -> dict[str, Any]:
    return {"configured": bool(token()), "source": token_source(), "username": get_setting("telegram_username"),
            "linked": chat_id() is not None, "chat_name": get_setting("telegram_chat_name"),
            "enabled": get_setting("telegram_enabled") != "0", "kinds": enabled_kinds(), "link_url": link_url()}


COMMANDS = [("drafts", "Open answer drafts"), ("auto", "Automatic answers per ad"),
            ("status", "Session and counters"), ("help", "What this bot can do")]


async def save_token(tok: str) -> str:
    """Check a BotFather token, store it (600) and start a fresh link. Returns the bot's @username."""
    tok = tok.strip()
    me = await api("getMe", _token=tok)
    p = _token_file()
    p.write_text(tok)
    p.chmod(0o600)
    set_setting("telegram_username", me.get("username") or "")
    set_setting("telegram_chat_id", "")
    set_setting("telegram_chat_name", "")
    new_link_code()
    try:
        await api("deleteWebhook")          # getUpdates does not work while a webhook is set
        await api("setMyCommands", commands=[{"command": c, "description": t(d)} for c, d in COMMANDS])
    except (TelegramError, httpx.HTTPError) as e:
        log.info("telegram setup calls failed: %s", e)
    return me.get("username") or ""


def new_link_code() -> str:
    code = secrets.token_urlsafe(12)
    set_setting("telegram_link_code", code)
    return code


def unlink() -> None:
    set_setting("telegram_chat_id", "")
    set_setting("telegram_chat_name", "")
    new_link_code()


def forget() -> None:
    _token_file().unlink(missing_ok=True)
    for k in ("telegram_chat_id", "telegram_chat_name", "telegram_username", "telegram_link_code"):
        set_setting(k, "")


# ------------------------------------------------------------------ Bot API
async def api(method: str, _token: str | None = None, _timeout: float = 20, **params: Any) -> Any:
    tok = _token or token()
    if not tok:
        raise TelegramError("no bot token")
    body = {k: v for k, v in params.items() if v is not None}
    async with httpx.AsyncClient(timeout=_timeout, transport=_transport) as c:
        r = await c.post(f"{API}/bot{tok}/{method}", json=body)
    try:
        data = r.json()
    except ValueError as e:
        raise TelegramError(f"{method}: HTTP {r.status_code}") from e
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
    return data.get("result")


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def _button(text: str, verb: str, ref: str) -> dict[str, str]:
    tok = secrets.token_urlsafe(9)
    with session() as db:
        db.add(TelegramAction(token=tok, verb=verb, ref=str(ref)))
        db.commit()
    return {"text": text, "callback_data": f"t:{tok}"}


def _rows(buttons: list[dict[str, str]], per_row: int = 2) -> list[list[dict[str, str]]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def keyboard(kind: str, data: dict[str, Any], actions: list[dict[str, Any]], url: str | None) -> dict | None:
    """Telegram buttons for one notification (HA push actions are translated, others derived from data)."""
    from app.services import ha_actions

    buttons: list[dict[str, str]] = []
    for a in actions:
        parsed = ha_actions.parse_action(str(a.get("action") or ""))
        if parsed:
            verb, draft_id = parsed
            icon = {"SEND": "✅ ", "REPLY": "↩️ ", "REVISE": "✍️ ", "DISCARD": "🗑 ", "CAL": "📅 ", "SHIP": "📦 "}.get(verb, "")
            buttons.append(_button(icon + str(a.get("title") or verb), f"D_{verb}", str(draft_id)))
    conv, ad_id = data.get("conversation_id"), data.get("ad_id")
    if kind == "message" and conv:
        buttons += [_button("✍️ " + t("Draft with Claude"), "C_DRAFT", conv), _button("↩️ " + t("Reply"), "C_REPLY", conv)]
    elif kind == "auto_reply" and conv:
        ref = str(data.get("draft_id") or f"conv:{conv}")
        buttons += [_button("👍", "F_UP", ref), _button("👎 " + t("Bad answer"), "F_DOWN", ref)]
        if ad_id:
            buttons.append(_button("⏸ " + t("Auto answers off for this ad"), "A_OFF", ad_id))
    elif kind in ("hit", "price_drop") and ad_id:
        buttons += [_button("⭐ " + t("Keep"), "H_SAVE", ad_id), _button("🚫 " + t("Ignore"), "H_IGNORE", ad_id)]
    rows = _rows(buttons)
    if url and url.startswith("https://"):
        rows.append([{"text": "🔗 " + t("Open"), "url": url}])
    return {"inline_keyboard": rows} if rows else None


# ------------------------------------------------------------------ outgoing
async def send(kind: str, title: str, message: str, *, url: str | None = None, image: str | None = None,
               data: dict[str, Any] | None = None, actions: list[dict[str, Any]] | None = None,
               tag: str | None = None, fresh: bool = False) -> bool:
    """Post one notification to the linked chat. A known `tag` edits the earlier message instead (like HA)."""
    if not active() or (kind != "test" and kind not in enabled_kinds()):
        return False
    text = f"<b>{_esc(title)}</b>\n{_esc(message)}"
    try:
        markup = keyboard(kind, data or {}, actions or [], url)
        prev = _message_for(tag) if tag and not fresh else None
        if prev is not None:
            try:
                await _edit(prev, text, markup)
                return True
            except TelegramError as e:
                if "not modified" in str(e):
                    return True
                log.info("telegram edit failed, sending new: %s", e)
        if image and image.startswith("https://"):
            try:
                msg = await api("sendPhoto", chat_id=chat_id(), photo=image, caption=text[:1024], parse_mode="HTML",
                                reply_markup=markup)
            except TelegramError as e:      # image URL Telegram cannot fetch -> plain text
                log.info("telegram photo failed: %s", e)
                image, msg = None, None
            else:
                _remember(tag, msg, photo=True)
                return True
        msg = await api("sendMessage", chat_id=chat_id(), text=text[:4096], parse_mode="HTML",
                        reply_markup=markup, link_preview_options={"is_disabled": True})
        _remember(tag, msg, photo=False)
        return True
    except (TelegramError, httpx.HTTPError) as e:
        log.warning("telegram send failed: %s", e)
        return False


TYPING_EVERY_S = 4.5   # Telegram shows "typing…" for ~5 s per call


@asynccontextmanager
async def typing():
    """Show "typing…" in the linked chat while the block runs (Claude writing an answer)."""
    if not active():
        yield
        return

    async def keep_typing() -> None:
        while True:
            try:
                await api("sendChatAction", chat_id=chat_id(), action="typing")
            except (TelegramError, httpx.HTTPError) as e:
                log.debug("sendChatAction failed: %s", e)
            await asyncio.sleep(TYPING_EVERY_S)

    task = asyncio.get_running_loop().create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def clear(tag: str) -> None:
    """Take the buttons off a tagged message (it was handled somewhere)."""
    prev = _message_for(tag) if active() else None
    if prev is None:
        return
    try:
        await api("editMessageReplyMarkup", chat_id=prev.chat_id, message_id=prev.message_id,
                  reply_markup={"inline_keyboard": []})
    except (TelegramError, httpx.HTTPError) as e:
        log.info("telegram clear failed: %s", e)


def _message_for(tag: str) -> TelegramMessage | None:
    with session() as db:
        return db.get(TelegramMessage, tag)


def _remember(tag: str | None, msg: dict[str, Any] | None, photo: bool) -> None:
    if not tag or not msg:
        return
    with session() as db:
        row = db.get(TelegramMessage, tag) or TelegramMessage(tag=tag, chat_id=0, message_id=0)
        row.chat_id, row.message_id, row.photo = msg["chat"]["id"], msg["message_id"], photo
        db.add(row)
        db.commit()


async def _edit(prev: TelegramMessage, text: str, markup: dict | None) -> None:
    markup = markup or {"inline_keyboard": []}
    if prev.photo:
        await api("editMessageCaption", chat_id=prev.chat_id, message_id=prev.message_id, caption=text[:1024],
                  parse_mode="HTML", reply_markup=markup)
    else:
        await api("editMessageText", chat_id=prev.chat_id, message_id=prev.message_id, text=text[:4096],
                  parse_mode="HTML", reply_markup=markup, link_preview_options={"is_disabled": True})


async def _say(text: str, markup: dict | None = None, reply_to: int | None = None) -> dict | None:
    try:
        return await api("sendMessage", chat_id=chat_id(), text=text[:4096], parse_mode="HTML", reply_markup=markup,
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
                         link_preview_options={"is_disabled": True})
    except (TelegramError, httpx.HTTPError) as e:
        log.warning("telegram reply failed: %s", e)
        return None


async def _ask(prompt: str, verb: str, ref: str, placeholder: str) -> None:
    """ForceReply prompt; the user's reply to it carries the text for verb/ref."""
    msg = await _say(prompt, {"force_reply": True, "input_field_placeholder": placeholder[:64]})
    if msg:
        _pending[msg["message_id"]] = (verb, ref, time.monotonic())


# ------------------------------------------------------------------ incoming
async def handle_update(u: dict[str, Any]) -> None:
    if cq := u.get("callback_query"):
        await _on_callback(cq)
    elif msg := u.get("message"):
        await _on_message(msg)


async def _on_message(msg: dict[str, Any]) -> None:
    chat = (msg.get("chat") or {})
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        code = text.partition(" ")[2].strip()
        if code and secrets.compare_digest(code, get_setting("telegram_link_code") or "-"):
            set_setting("telegram_chat_id", str(chat.get("id")))
            set_setting("telegram_chat_name", chat.get("username") or chat.get("first_name") or chat.get("title") or "")
            set_setting("telegram_link_code", "")      # one-time
            await _say("✅ " + t("Linked. Notifications from willhaben Agent now arrive here.") + "\n\n" + _help())
            return
    if chat_id() is None or chat.get("id") != chat_id():
        return                                          # not our user: stay silent
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    pending = _pending.pop(reply_to, None) if reply_to else None
    if pending is None and text and not text.startswith("/"):
        pending = _latest_pending()
    if pending:
        verb, ref, _ = pending
        await _complete(verb, ref, text, msg.get("message_id"))
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd in ("/drafts", "/entwuerfe"):
        await _cmd_drafts()
    elif cmd == "/auto":
        await _cmd_auto()
    elif cmd == "/status":
        await _cmd_status()
    else:
        await _say(_help())


def _latest_pending() -> tuple[str, str, float] | None:
    """Plain text without "reply": use the newest open prompt (the usual case on a phone)."""
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v[2] > PENDING_TTL]:
        _pending.pop(k, None)
    if not _pending:
        return None
    key = max(_pending, key=lambda k: _pending[k][2])
    return _pending.pop(key)


def _help() -> str:
    return "\n".join([
        t("Buttons under each notification: send, answer, let Claude rewrite, feedback."),
        t("To type an answer, just write it after tapping the button."),
        "", "/drafts – " + t("Open answer drafts"), "/auto – " + t("Automatic answers per ad"),
        "/status – " + t("Session and counters"),
    ])


async def _on_callback(cq: dict[str, Any]) -> None:
    from app.services import ha_actions

    msg = cq.get("message") or {}
    cq_id = cq.get("id")
    if chat_id() is None or (msg.get("chat") or {}).get("id") != chat_id():
        await _answer(cq_id, t("Not allowed."))
        return
    raw = str(cq.get("data") or "")
    with session() as db:
        row = db.get(TelegramAction, raw[2:]) if raw.startswith("t:") else None
    if row is None:
        await _answer(cq_id, t("This button has expired."))
        return
    verb, ref = row.verb, row.ref

    if verb in ("D_SEND", "D_DISCARD", "D_CAL", "D_SHIP"):
        await _answer(cq_id, "…")
        res = await ha_actions.handle({"action": ha_actions.action_id(verb[2:], int(ref))})
        if res is None:
            await _say("ℹ️ " + t("Already done."), reply_to=msg.get("message_id"))
            await _drop_buttons(msg)
        return
    if verb == "D_REPLY":
        await _answer(cq_id)
        await _ask(t("Type the answer – it is sent exactly like this:"), verb, ref, t("Your answer"))
    elif verb == "D_REVISE":
        await _answer(cq_id)
        await _ask(t("How should Claude change the answer?"), verb, ref, t("e.g. friendlier, offer 18 €"))
    elif verb == "C_REPLY":
        await _answer(cq_id)
        await _ask(t("Type your answer:"), verb, ref, t("Your answer"))
    elif verb == "C_DRAFT":
        await _answer(cq_id, t("Claude is writing a draft …"))
        await _draft_for(ref)
    elif verb == "F_UP":
        d = _resolve_draft(ref)
        if d:
            from app.services import feedback
            feedback.rate(d.id, "up")
        await _answer(cq_id, "👍 " + t("Thanks, noted."))
    elif verb == "F_DOWN":
        d = _resolve_draft(ref)
        if d:
            from app.services import feedback
            feedback.rate(d.id, "down")
            await _answer(cq_id)
            await _ask(t("What was wrong? Your note is used for future automatic answers."), "F_NOTE", str(d.id),
                       t("e.g. never promise shipping"))
        else:
            await _answer(cq_id, t("Answer not found."))
    elif verb in ("A_ON", "A_OFF"):
        name = _set_auto(ref, verb == "A_ON")
        await _answer(cq_id, t("Automatic answers on: {ad}", ad=name) if verb == "A_ON" else t("Automatic answers off: {ad}", ad=name))
        if verb == "A_OFF":
            await _say("⏸ " + t("Automatic answers off: {ad}", ad=_esc(name)),
                       {"inline_keyboard": [[_button("▶️ " + t("Switch on again"), "A_ON", ref)]]})
    elif verb in ("H_SAVE", "H_IGNORE"):
        n = _set_hit_state(ref, "saved" if verb == "H_SAVE" else "ignored")
        await _answer(cq_id, ("⭐ " + t("Kept")) if verb == "H_SAVE" else ("🚫 " + t("Ignored")) if n else t("Hit not found."))
        if n:
            await _drop_buttons(msg)
    else:
        await _answer(cq_id)


async def _complete(verb: str, ref: str, text: str, msg_id: int | None) -> None:
    """The text for a ForceReply prompt arrived."""
    from app.services import chats, feedback, ha_actions

    if not text:
        return
    if verb in ("D_REPLY", "D_REVISE"):
        res = await ha_actions.handle({"action": ha_actions.action_id(verb[2:], int(ref)), "reply_text": text})
        if res is None:     # failures are reported by handle() itself (notify -> also Telegram)
            await _say("ℹ️ " + t("Already done."), reply_to=msg_id)
    elif verb == "C_REPLY":
        with session() as db:
            if db.get(Conversation, ref) is None:
                await _say(t("Chat not found."), reply_to=msg_id)
                return
            d = Draft(conversation_id=ref, text=text, model="telegram")
            db.add(d)
            db.commit()
            db.refresh(d)
        try:
            await chats.send_draft(d.id, text)
        except Exception as e:  # noqa: BLE001 - report every failure back
            await _say("⚠️ " + t("Sending failed: {error}", error=_esc(str(e)[:200])), reply_to=msg_id)
        else:
            await _say("✅ " + t("Sent."), reply_to=msg_id)
    elif verb == "F_NOTE":
        feedback.rate(int(ref), "down", text)
        await _say("📝 " + t("Noted – future automatic answers will take it into account."), reply_to=msg_id)


async def _draft_for(conversation_id: str) -> None:
    from app.services import chats

    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None:
        await _say(t("Chat not found."))
        return
    try:
        d = await chats.draft_reply(conversation_id)
    except Exception as e:  # noqa: BLE001
        await _say("⚠️ " + t("Claude could not write a draft: {error}", error=_esc(str(e)[:200])))
        return
    await chats.offer_draft(conv, d, t("requested in Telegram"))


def _resolve_draft(ref: str) -> Draft | None:
    """Draft id, or "conv:<id>" for the newest automatically sent answer in that chat."""
    with session() as db:
        if ref.isdigit():
            return db.get(Draft, int(ref))
        if ref.startswith("conv:"):
            return db.exec(select(Draft).where(Draft.conversation_id == ref[5:], Draft.status == "sent")
                           .order_by(col(Draft.id).desc())).first()
    return None


def _set_auto(ad_id: str, on: bool) -> str:
    """Only the switch; the per-ad instructions stay untouched."""
    with session() as db:
        ad = db.get(MyAd, ad_id)
        if ad is None:
            return ad_id
        ad.auto_reply = on
        db.add(ad)
        db.commit()
        return ad.heading or ad_id


def _set_hit_state(ad_id: str, state: str) -> int:
    with session() as db:
        hits = db.exec(select(Hit).where(Hit.ad_id == ad_id)).all()
        for h in hits:
            h.state = state
            db.add(h)
        db.commit()
    return len(hits)


async def _answer(cq_id: str | None, text: str = "") -> None:
    try:
        await api("answerCallbackQuery", callback_query_id=cq_id, text=text[:200] or None)
    except (TelegramError, httpx.HTTPError) as e:
        log.info("answerCallbackQuery failed: %s", e)


async def _drop_buttons(msg: dict[str, Any]) -> None:
    try:
        await api("editMessageReplyMarkup", chat_id=(msg.get("chat") or {}).get("id"), message_id=msg.get("message_id"),
                  reply_markup={"inline_keyboard": []})
    except (TelegramError, httpx.HTTPError):
        pass


# ------------------------------------------------------------------ commands
def can_send(d: Draft) -> bool:
    """Same rule as chats.offer_draft: no plain "send" while the text has [placeholders] or looks like a scam."""
    return not ("[" in d.text and "]" in d.text) and d.intent != "scam_suspect" and bool(d.text.strip())


async def _cmd_drafts() -> None:
    from app.services import ha_actions

    with session() as db:
        rows = db.exec(select(Draft).where(Draft.status == "draft").order_by(col(Draft.id).desc()).limit(5)).all()
        convs = {c.id: c for c in db.exec(select(Conversation).where(col(Conversation.id).in_([d.conversation_id for d in rows])))}
    if not rows:
        await _say(t("No open drafts."))
        return
    for d in reversed(rows):
        conv = convs.get(d.conversation_id)
        label = t("Draft")
        await send("auto_reply_pending", f"{label}: {conv.partner_name if conv else ''}".strip(),
                   f"„{d.text[:300]}“\n{(conv.ad_title if conv else '') or ''}",
                   actions=ha_actions.draft_actions(d.id, can_send(d)), tag=ha_actions.tag_for(d.id), fresh=True)


async def _cmd_auto() -> None:
    with session() as db:
        ads = db.exec(select(MyAd).where(MyAd.status != "expired").order_by(col(MyAd.heading))).all()
    if not ads:
        await _say(t("No own ads imported yet."))
        return
    rows = [[_button(("✅ " if ad.auto_reply else "⬜ ") + (ad.heading or ad.ad_id)[:40],
                     "A_OFF" if ad.auto_reply else "A_ON", ad.ad_id)] for ad in ads[:30]]
    await _say(t("Automatic answers – tap to switch:"), {"inline_keyboard": rows})


async def _cmd_status() -> None:
    from sqlalchemy import func

    from app.willhaben.session import get_session

    st = get_session().status()
    with session() as db:
        new_hits = db.exec(select(func.count()).select_from(Hit).where(Hit.state == "new")).one()
        unread = db.exec(select(func.coalesce(func.sum(Conversation.unread_count), 0))).one()
        drafts = db.exec(select(func.count()).select_from(Draft).where(Draft.status == "draft")).one()
    login = t("logged in") if st.get("logged_in") else t("NOT logged in")
    await _say("\n".join([f"willhaben: <b>{login}</b>", t("New hits: {n}", n=new_hits),
                          t("Unread messages: {n}", n=unread), t("Open drafts: {n}", n=drafts)]))


# ------------------------------------------------------------------ polling
def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _safe(u: dict[str, Any]) -> None:
    try:
        await handle_update(u)
    except Exception:  # noqa: BLE001 - one bad update must not stop the bot
        log.exception("telegram update failed")


def _prune_actions() -> None:
    with session() as db:
        db.exec(delete(TelegramAction).where(col(TelegramAction.created_at) < utcnow() - timedelta(days=30)))
        db.commit()


async def listen_forever() -> None:
    """getUpdates long polling; each update runs as its own task (a Claude draft takes a minute)."""
    offset, delay, last_prune = None, 5, 0.0
    while True:
        if not token():
            await asyncio.sleep(10)
            continue
        if time.monotonic() - last_prune > 86400:
            _prune_actions()
            last_prune = time.monotonic()
        try:
            updates = await api("getUpdates", _timeout=65, offset=offset, timeout=50,
                                allowed_updates=["message", "callback_query"])
            delay = 5
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - network, token revoked, Telegram down
            log.info("telegram polling failed: %s (retry in %ss)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        for u in updates or []:
            offset = u["update_id"] + 1
            _spawn(_safe(u))
