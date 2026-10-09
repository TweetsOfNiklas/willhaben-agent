"""willhaben chats: sync conversations, draft replies with Claude, send after approval or automatically.

Automatic answers are opt-in per own ad (MyAd.auto_reply), only in conversations where the user is
the seller, only after one manual send has been confirmed (setting send_verified), within rate limits,
and only when both Claude (auto_send) and the code checks below agree.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import col, select

from app.db import Conversation, Draft, Message, MyAd, get_setting, session, set_setting, utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import AUTO_REPLY_SCHEMA, AUTO_REPLY_SYSTEM, REPLY_SCHEMA, REPLY_SYSTEM, auto_rules, price_floor, reply_prompt
from app.services import feedback, gcal, humanize, meetups, min_price, shipping, telegram
from app.services.notify import notify
from app.willhaben.account import AccountClient, EndpointUnknown, SendFailed
from app.willhaben.public import WillhabenError, get_public
from app.willhaben.session import NotLoggedIn, SessionUnavailable, get_session

log = logging.getLogger(__name__)


def _account() -> AccountClient:
    return AccountClient(get_session())


def sync_window() -> tuple[int, datetime | None]:
    """Settings chat_sync_limit (N latest conversations, 1-50) and chat_sync_since (cutoff date, local midnight)."""
    try:
        limit = min(max(int(get_setting("chat_sync_limit") or 10), 1), 50)
    except ValueError:
        limit = 10
    since = None
    if raw := (get_setting("chat_sync_since") or "").strip():
        try:
            since = datetime.strptime(raw, "%Y-%m-%d").astimezone()   # naive -> local midnight
        except ValueError:
            pass
    return limit, since


async def sync(force: bool = False) -> dict[str, Any]:
    sess = get_session()
    if not sess.started:
        return {"skipped": t("Browser session is not running")}
    if not sess.logged_in and not await sess.check_login():
        return {"skipped": t("not logged in")}
    acct = _account()
    limit, since = sync_window()
    try:
        convs = await acct.conversations(limit=limit)
    except NotLoggedIn:
        return {"skipped": t("Session expired")}
    convs = sorted(convs, key=lambda c: _aware(c.last_at) if c.last_at else datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:limit]
    if since is not None:
        convs = [c for c in convs if c.last_at is None or _aware(c.last_at) >= since]

    changed: list[str] = []
    incoming: list[Conversation] = []
    with session() as db:
        first_sync = db.exec(select(Conversation).limit(1)).first() is None
        for c in convs:
            row = db.get(Conversation, c.id)
            is_new = row is None
            if row is None:
                row = Conversation(id=c.id)
            newer = c.last_at is not None and (row.last_message_at is None or c.last_at > row.last_message_at)
            more_unread = c.unread_count > (row.unread_count or 0)
            row.ad_id = c.ad_id or row.ad_id
            row.ad_title = c.ad_title or row.ad_title
            row.partner_name = c.partner_name or row.partner_name
            row.role = c.role or row.role
            row.unread_count = c.unread_count
            row.last_message_text = c.last_text or row.last_message_text
            row.last_message_at = c.last_at or row.last_message_at
            row.synced_at = utcnow()
            db.add(row)
            if is_new or newer or force:
                changed.append(c.id)
            if not first_sync and c.unread_count > 0 and (newer or more_unread) and not c.last_is_mine:
                incoming.append(row)
        db.commit()

    for cid in changed:
        await sync_messages(cid)

    notify_on = get_setting("notify_messages") == "1"
    for row in incoming:
        try:
            await shipping.on_incoming(row)    # buyer sent an address? -> parcel + label offer (background)
        except Exception as e:  # noqa: BLE001 - never block the chat sync
            log.warning("shipping check failed for %s: %s", row.id, e)
        if _auto_ad(row) is not None:
            continue   # auto reply on: process_pending_auto pushes "answered" or "approve?" instead of the raw message
        if notify_on:
            await notify("message", t("Message from {name}", name=row.partner_name or t("unknown")),
                         f"{row.ad_title or ''}: {(row.last_message_text or '')[:140]}",
                         url="https://www.willhaben.at/iad/myprofile/chat",
                         data={"conversation_id": row.id, "ad_id": row.ad_id})
        if get_setting("auto_draft") == "1":
            try:
                await draft_reply(row.id)
            except (LLMError, WillhabenError) as e:
                log.warning("auto draft failed for %s: %s", row.id, e)
    last_sync_at[0] = utcnow()
    answered = await process_pending_auto()
    return {"conversations": len(convs), "changed": len(changed), "incoming": len(incoming), "auto": answered}


def _is_fallback_id(ext_id: str) -> bool:
    """Ids we made up: last-message previews, content hashes from before message_id was parsed,
    and `draft-<id>` rows written when we sent a message (before willhaben's history has it)."""
    return ext_id.startswith(("last-", "draft-")) or (len(ext_id) == 16 and all(c in "0123456789abcdef" for c in ext_id))


SENT_MATCH_WINDOW = timedelta(minutes=10)   # our clock at send time vs. willhaben's created_at


def _drop_sent_placeholders(db, conversation_id: str) -> None:
    """Remove `draft-<id>` rows once willhaben's history holds the same sent message under its real id."""
    rows = list(db.exec(select(Message).where(Message.conversation_id == conversation_id)))
    real = [r for r in rows if r.direction == "out" and not _is_fallback_id(r.ext_id)]
    for p in (r for r in rows if r.ext_id.startswith("draft-")):
        if any(r.text == p.text and r.created_at and p.created_at
               and abs(_aware(r.created_at) - _aware(p.created_at)) <= SENT_MATCH_WINDOW for r in real):
            db.delete(p)
    db.flush()


def _same_message(row: Message, text: str, created_at) -> bool:
    if row.text != text or not _is_fallback_id(row.ext_id):
        return False
    if row.created_at == created_at:
        return True
    if row.ext_id.startswith("draft-") and row.created_at and created_at:
        return abs(_aware(row.created_at) - _aware(created_at)) <= SENT_MATCH_WINDOW
    return False


def _upsert_message(db, conversation_id: str, ext_id: str, direction: str, text: str, created_at,
                    authoritative: bool = False) -> None:
    """Insert or update one message. `authoritative` = direction comes from the full history, not a guess.

    A row with the same text and time but another ext_id is the same message stored earlier under a
    fallback id (last-message preview, or before the real message_id was parsed): it is re-keyed.
    """
    exists = db.exec(select(Message).where(Message.conversation_id == conversation_id,
                                           Message.ext_id == ext_id)).first()
    if exists is None:
        exists = next((m for m in db.exec(select(Message).where(
            Message.conversation_id == conversation_id, Message.text == text))
            if _same_message(m, text, created_at)), None)
        if exists:
            exists.ext_id = ext_id
            db.add(exists)
    if exists:
        if direction != exists.direction and (authoritative or exists.direction == "unknown"):
            exists.direction = direction
            db.add(exists)
        return
    db.add(Message(conversation_id=conversation_id, ext_id=ext_id, direction=direction,
                   text=text, created_at=created_at))


async def sync_messages(conversation_id: str) -> str:
    """Fetch the full history; falls back to the list's last message while the endpoint is unverified."""
    acct = _account()
    try:
        msgs = await acct.messages(conversation_id)
    except EndpointUnknown:
        msgs = None
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if msgs is not None:
            for m in msgs:
                _upsert_message(db, conversation_id, m.ext_id, m.direction, m.text, m.created_at,
                                authoritative=True)
            _drop_sent_placeholders(db, conversation_id)
            mode = "full"
        else:
            if conv and conv.last_message_text:
                ext = "last-" + hashlib.sha1(f"{conv.last_message_at}|{conv.last_message_text}".encode()).hexdigest()[:12]
                direction = "in" if conv.unread_count > 0 else "unknown"
                _upsert_message(db, conversation_id, ext, direction, conv.last_message_text, conv.last_message_at)
            mode = "last-only"
        if conv:
            conv.messages_synced_at = utcnow()
            db.add(conv)
        db.commit()
    return mode


async def ensure_ad_context(conversation_id: str) -> Conversation | None:
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None or not conv.ad_id or conv.ad_context:
        return conv
    try:
        detail = await get_public().get_detail(conv.ad_id)
    except WillhabenError as e:
        log.info("ad context for %s failed: %s", conv.ad_id, e)
        return conv
    me = get_session().user or {}
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if detail is None:
            # German on purpose: ad_context is part of the prompt Claude answers from
            conv.ad_context = {"title": conv.ad_title, "status": "nicht mehr online"}
        else:
            conv.ad_context = detail.as_context()
            mine = {str(v) for v in (me.get("id"), me.get("uuid")) if v}
            if mine:
                conv.role = "seller" if {detail.seller_login_id, detail.seller_uuid} & mine else "buyer"
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


def messages_of(conversation_id: str) -> list[Message]:
    with session() as db:
        return list(db.exec(select(Message).where(Message.conversation_id == conversation_id)
                            .order_by(col(Message.created_at).asc().nulls_first(), Message.id)))


def drafts_of(conversation_id: str) -> list[Draft]:
    with session() as db:
        return list(db.exec(select(Draft).where(Draft.conversation_id == conversation_id)
                            .order_by(Draft.created_at.desc())))


def _style_examples(limit: int = 3) -> list[str]:
    with session() as db:
        rows = db.exec(select(Draft).where(Draft.status == "sent").order_by(Draft.sent_at.desc()).limit(limit))
        return [r.text for r in rows]


async def draft_reply(conversation_id: str, hint: str | None = None, auto: bool = False) -> Draft:
    conv = await ensure_ad_context(conversation_id)
    if conv is None:
        raise LLMError(t("Conversation not found"))
    msgs = messages_of(conversation_id)
    if not msgs and conv.last_message_text:
        msgs_t = [("in" if conv.unread_count else "unknown", conv.last_message_at, conv.last_message_text)]
    else:
        msgs_t = [(m.direction, m.created_at, m.text) for m in msgs]
    rules = get_setting("reply_rules")
    with session() as db:
        ad = db.get(MyAd, conv.ad_id) if conv.ad_id else None
    floor = ad.min_price if ad and conv.role == "seller" else None
    asking = (conv.ad_context or {}).get("price")
    if auto:
        rules = f"{rules}\n\n{auto_rules(get_setting('auto_reply_rules'), ad.auto_reply_instructions if ad else None, floor, asking)}"
        if lessons := feedback.prompt_block():     # the owner's 👎 notes on earlier automatic answers
            rules = f"{rules}\n\n{lessons}"
    elif floor is not None:                       # manual drafts of my own ad: same negotiation range
        rules = f"{rules}\n\n{price_floor(floor, asking)}"
    cal = gcal.config()
    availability = await gcal.availability_text() if (cal["in_auto"] if auto else cal["in_drafts"]) else None
    prompt = reply_prompt(role=conv.role, ad=conv.ad_context, partner=conv.partner_name, messages=msgs_t,
                          rules=rules, examples=_style_examples(), hint=hint, availability=availability)
    backend = get_backend("chat")
    async with telegram.typing():                 # "typing…" in Telegram while Claude writes
        result = await backend.complete(system=AUTO_REPLY_SYSTEM if auto else REPLY_SYSTEM, prompt=prompt,
                                        schema=AUTO_REPLY_SCHEMA if auto else REPLY_SCHEMA)
    d = result.data
    note = (d.get("note") or "").strip()
    # "Hinweis:" marks Claude's note among needs_info (stored in the DB, matched below and in chat.html)
    draft = Draft(conversation_id=conversation_id, text=d.get("reply", "").strip(), intent=d.get("intent"),
                  needs_info=(d.get("needs_info") or []) + ([f"Hinweis: {note}"] if note else []),
                  suggested_price=d.get("suggested_price"), model=result.model, meetup=meetups.clean(d.get("meetup")),
                  auto=True if auto else None,
                  auto_reason=(("✓ " if d.get("auto_send") else "✗ ") + (d.get("auto_reason") or "").strip()) if auto else None)
    with session() as db:
        db.add(draft)
        db.commit()
        db.refresh(draft)
    return draft


async def mark_read(conversation_id: str) -> int:
    """Read receipts for the buyer's unread messages (best effort: never blocks answering)."""
    try:
        n = await _account().mark_read(conversation_id)
    except Exception as e:  # noqa: BLE001 - read receipts are cosmetic, answering must go on
        log.info("mark read failed for %s: %s", conversation_id, e)
        return 0
    if n:
        log.info("marked %d message(s) read in %s", n, conversation_id)
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv and conv.unread_count:
            conv.unread_count = 0
            db.add(conv)
            db.commit()
    return n


async def send_draft(draft_id: int, text: str) -> Draft:
    """Send an approved draft. Raises EndpointUnknown until the send endpoint has been captured."""
    with session() as db:
        draft = db.get(Draft, draft_id)
    if draft is None:
        raise ValueError(t("Draft not found"))
    text = text.strip()
    await mark_read(draft.conversation_id)   # nobody answers without having read the message
    try:
        await _account().send_message(draft.conversation_id, text)
    except (EndpointUnknown, SendFailed, NotLoggedIn, SessionUnavailable, ValueError) as e:   # keep the edited text
        with session() as db:
            draft = db.get(Draft, draft_id)
            draft.text, draft.error = text, str(e)
            db.add(draft)
            db.commit()
        raise
    if get_setting("send_verified") != "1":
        set_setting("send_verified", "1")     # sending works on this account: auto replies may now send
        log.info("first send confirmed via history; auto replies enabled")
    sent = mark_sent(draft_id, text)
    try:
        await meetups.on_sent(sent)           # agreed appointment -> "add to calendar?" push
    except Exception as e:  # noqa: BLE001 - the message is out; the calendar offer is a bonus
        log.warning("calendar offer for draft %s failed: %s", draft_id, e)
    return sent


# ------------------------------------------------------------------ automatic answers
AUTO_PER_CONVERSATION_PER_DAY = 3
AUTO_PER_HOUR = 10


def _auto_ad(conv: Conversation) -> MyAd | None:
    """The own ad whose buyers get automatic answers, if this conversation qualifies."""
    if conv.role != "seller" or not conv.ad_id:
        return None
    with session() as db:
        ad = db.get(MyAd, conv.ad_id)
    if ad and "sold" in (ad.sale_status, ad.status):
        return None   # sold: no automatic answers to further buyers (the sold workflow offers a polite "sold" draft)
    return ad if ad and ad.auto_reply else None


def _auto_blockers(conv_id: str, d: Draft, msgs: list[Message]) -> list[str]:
    """Hard checks in code, independent of what the model says."""
    why = []
    if get_setting("send_verified") != "1":
        why.append(t("Sending not confirmed yet – send once manually from the dashboard"))
    if not d.auto_reason or not d.auto_reason.startswith("✓"):
        why.append(t("Claude does not recommend an automatic answer"))
    if d.intent == "scam_suspect":
        why.append(t("Suspected scam"))
    if any(not x.startswith("Hinweis:") for x in d.needs_info or []):
        why.append(t("Claude needs info: {items}", items=", ".join(x for x in d.needs_info if not x.startswith("Hinweis:"))))
    if "[" in d.text and "]" in d.text:
        why.append(t("Answer contains placeholders"))
    if not d.text.strip():
        why.append(t("empty answer"))
    with session() as db:                 # price floor of the own ad, checked in code (app/services/min_price.py)
        conv = db.get(Conversation, conv_id)
        ad = db.get(MyAd, conv.ad_id) if conv and conv.ad_id else None
    why += min_price.blockers(ad.min_price if ad else None, d.suggested_price, d.text)
    last = next((m for m in reversed(msgs) if m.direction in ("in", "out")), None)
    if last is None or last.direction != "in":
        why.append(t("last message is not from the other person"))
    now = utcnow()
    with session() as db:
        sent = list(db.exec(select(Draft).where(Draft.auto == True, Draft.status == "sent",  # noqa: E712
                                                Draft.sent_at >= now - timedelta(days=1))))
    if sum(1 for x in sent if x.conversation_id == conv_id) >= AUTO_PER_CONVERSATION_PER_DAY:
        why.append(t("already {n} automatic answers today in this chat", n=AUTO_PER_CONVERSATION_PER_DAY))
    if sum(1 for x in sent if x.sent_at and x.sent_at >= now - timedelta(hours=1)) >= AUTO_PER_HOUR:
        why.append(t("Limit of {n} automatic answers per hour reached", n=AUTO_PER_HOUR))
    return why


AUTO_MAX_AGE = timedelta(hours=48)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _waiting_for_answer(conv_id: str) -> bool:
    """Buyer's last message is recent and nobody (me or the app) has drafted or sent anything since."""
    msgs = messages_of(conv_id)
    last = next((m for m in reversed(msgs) if m.direction in ("in", "out")), None)
    if last is None or last.direction != "in" or last.created_at is None:
        return False
    since = _aware(last.created_at)
    if utcnow() - since > AUTO_MAX_AGE:
        return False
    with session() as db:
        later = db.exec(select(Draft).where(Draft.conversation_id == conv_id, Draft.status != "discarded")).all()
    alive = conv_id in _humanized
    # "pending" = a human-like answer still "typing"; after a restart its task is gone and it must not block
    return not any(_aware(d.created_at) >= since for d in later if d.status != "pending" or alive)


# ------------------------------------------------------------------ live chat events
LIVE_MIN_GAP_S = 15          # at most one sync per 15 s, whatever the socket pushes (willhaben budget)
_live = {"pending": False, "task": None, "last": 0.0}
last_sync_at: list = [None]  # set by sync(); the poll job skips while live is healthy and this is fresh


async def on_live_event(conversation_id: str, frame: dict[str, Any]) -> None:
    """Called for every chat frame the live WebSocket pushes; coalesces bursts into one sync."""
    _live["pending"] = True
    if _live["task"] is None or _live["task"].done():
        _live["task"] = asyncio.create_task(_live_sync())


async def _live_sync() -> None:
    loop = asyncio.get_running_loop()
    while _live["pending"]:
        wait = LIVE_MIN_GAP_S - (loop.time() - _live["last"])
        await asyncio.sleep(max(wait, 1.5))       # also lets a burst of frames settle
        _live["pending"] = False
        _live["last"] = loop.time()
        try:
            res = await sync()
            log.info("live chat sync: %s", res)
        except Exception as e:  # noqa: BLE001 - keep listening
            log.warning("live chat sync failed: %s", e)


_auto_lock = asyncio.Lock()   # scheduled sync, manual sync and the toggle may overlap: never answer twice


async def process_pending_auto(ad_id: str | None = None) -> int:
    """Answer every waiting seller conversation of ads with auto reply on (optionally just one ad)."""
    async with _auto_lock:
        return await _process_pending_auto(ad_id)


async def _process_pending_auto(ad_id: str | None) -> int:
    with session() as db:
        q = select(Conversation).where(Conversation.role == "seller")
        if ad_id:
            q = q.where(Conversation.ad_id == ad_id)
        convs = list(db.exec(q))
    done = 0
    for conv in convs:
        if _auto_ad(conv) is None or not _waiting_for_answer(conv.id):
            continue
        if humanize.enabled():
            _schedule_humanized(conv.id)   # answers later in the background, sync goes on
            continue
        try:
            d = await auto_reply(conv.id)
        except (LLMError, WillhabenError, NotLoggedIn, SessionUnavailable) as e:
            log.warning("auto reply failed for %s: %s", conv.id, e)
            continue
        done += bool(d and d.status == "sent")
    return done


# ------------------------------------------------------------------ human-like timing
_humanized: dict[str, asyncio.Task] = {}   # conversation id -> pending delayed answer


def _schedule_humanized(conversation_id: str) -> None:
    task = _humanized.get(conversation_id)
    if task is not None and not task.done():
        return
    _humanized[conversation_id] = asyncio.create_task(_humanized_auto(conversation_id))


def _last_incoming_msg(conversation_id: str) -> Message | None:
    msgs = [m for m in messages_of(conversation_id) if m.direction == "in"]
    return msgs[-1] if msgs else None


async def _sleep_until(when) -> None:
    wait = (_aware(when) - utcnow()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)


async def _humanized_auto(conversation_id: str) -> Draft | None:
    """Notice the message after a while, read it (receipt), think, type, then send - re-checking in between."""
    with session() as db:
        for orphan in db.exec(select(Draft).where(Draft.conversation_id == conversation_id, Draft.status == "pending")):
            orphan.status = "discarded"
            db.add(orphan)
        db.commit()
    try:
        for _ in range(3):   # a new buyer message while "typing" means: read again and answer that
            last = _last_incoming_msg(conversation_id)
            if last is None:
                return None
            warm = any(m.direction == "out" for m in messages_of(conversation_id))   # I answered before
            plan = humanize.plan(_aware(last.created_at), warm=warm)
            await _sleep_until(plan.read_at)
            await sync_messages(conversation_id)
            if not _waiting_for_answer(conversation_id):
                return None            # answered meanwhile (manually, on the phone, ...)
            newest = _last_incoming_msg(conversation_id)
            if newest is not None and newest.ext_id != last.ext_id:
                continue               # more came in before we "looked": plan from the newest one
            await mark_read(conversation_id)
            outcome: dict[str, Any] = {}

            async def before_send(d: Draft, plan=plan, last=last, outcome=outcome, warm=warm) -> bool:
                typing_done = max(_aware(plan.send_after), utcnow())
                await _sleep_until(typing_done + timedelta(seconds=humanize.typing_seconds(d.text, warm=warm)))
                await sync_messages(conversation_id)
                newest = _last_incoming_msg(conversation_id)
                if newest is not None and newest.ext_id != last.ext_id:
                    outcome["redo"] = True       # buyer wrote again while we were typing
                    return False
                msgs = messages_of(conversation_id)
                last_any = next((m for m in reversed(msgs) if m.direction in ("in", "out")), None)
                if last_any is None or last_any.direction != "in":
                    return False               # answered by hand / on willhaben meanwhile
                with session() as db:
                    others = db.exec(select(Draft).where(Draft.conversation_id == conversation_id,
                                                         Draft.id != d.id, Draft.status != "discarded")).all()
                since = _aware(newest.created_at) if newest and newest.created_at else None
                # another draft or answer (push button, Telegram, dashboard) since the buyer's message: stay out
                return not (since and any(_aware(o.created_at) >= since for o in others))

            d = await auto_reply(conversation_id, before_send=before_send)
            if outcome.get("redo"):
                continue
            return d
        return None
    except Exception:  # noqa: BLE001 - a background answer must never take the app down
        log.exception("humanized auto reply failed for %s", conversation_id)
        return None
    finally:
        _humanized.pop(conversation_id, None)


async def auto_reply(conversation_id: str, before_send=None) -> Draft | None:
    """Draft and (if nothing blocks it) send an automatic answer.

    `before_send(draft)` runs right before sending (human-like typing delay + re-check); False drops the draft.
    """
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None or _auto_ad(conv) is None:
        return None
    d = await draft_reply(conversation_id, auto=True)
    blockers = _auto_blockers(conversation_id, d, messages_of(conversation_id))
    title = conv.ad_title or ""
    if not blockers and before_send is not None:
        _set_status(d.id, "pending")
        go = await before_send(d)
        _set_status(d.id, "draft")
        if not go:
            discard_draft(d.id)
            return None
        async with _send_lock:   # limits and checks count at the moment of sending, one send at a time
            if not _still_auto(conversation_id):   # auto answers turned off / ad sold while "typing"
                discard_draft(d.id)
                return None
            blockers = _auto_blockers(conversation_id, d, messages_of(conversation_id))
            if not blockers:
                return await _send_auto(conv, d, conversation_id, title)
    if not blockers and not _still_auto(conversation_id):   # changed while Claude was drafting
        discard_draft(d.id)
        return None
    if not blockers:
        sent = await _send_auto(conv, d, conversation_id, title)
        if sent.status == "sent":
            return sent
        blockers = [sent.error or t("Sending failed")]
    with session() as db:
        row = db.get(Draft, d.id)
        row.auto_reason = f"{row.auto_reason or ''} · {t('not sent: {reasons}', reasons='; '.join(blockers))}".strip(" ·")
        db.add(row)
        db.commit()
        db.refresh(row)
    await offer_draft(conv, row, t("not sent automatically: {reason}", reason=blockers[0]))
    return row


_send_lock = asyncio.Lock()


def _still_auto(conversation_id: str) -> bool:
    """Re-check right before an automatic send: the owner may have switched auto answers off or sold the ad."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    return conv is not None and _auto_ad(conv) is not None


def _set_status(draft_id: int, status: str) -> None:
    with session() as db:
        row = db.get(Draft, draft_id)
        if row is not None and row.status in ("draft", "pending"):
            row.status = status
            db.add(row)
            db.commit()


async def _send_auto(conv: Conversation, d: Draft, conversation_id: str, title: str) -> Draft:
    """Send an automatic answer and push "answered"; on failure the draft keeps the error (status draft)."""
    try:
        d = await send_draft(d.id, d.text)
    except (EndpointUnknown, SendFailed, NotLoggedIn, SessionUnavailable) as e:
        with session() as db:
            row = db.get(Draft, d.id)
            row.error = t("Sending failed: {error}", error=e)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
    q = _last_incoming(conversation_id)
    await notify("auto_reply", t("Answered automatically: {name}", name=conv.partner_name or "").strip(),
                 (t("“{text}” → ", text=q[:120]) if q else "") + f"{d.text[:160]}\n{title}",
                 url="https://www.willhaben.at/iad/myprofile/chat",
                 data={"conversation_id": conversation_id, "ad_id": conv.ad_id, "draft_id": d.id})
    return d


def _last_incoming(conversation_id: str) -> str | None:
    msgs = [m for m in messages_of(conversation_id) if m.direction == "in"]
    return msgs[-1].text if msgs else None


async def offer_draft(conv: Conversation, d: Draft, reason: str) -> None:
    """Push the draft to the phone with buttons: send, direct answer, let Claude revise, discard."""
    from app.config import get_settings
    from app.services import ha_actions

    can_send = not ("[" in d.text and "]" in d.text) and d.intent != "scam_suspect" and bool(d.text.strip())
    q = _last_incoming(conv.id)
    await notify("auto_reply_pending", t("Approve answer? {name}", name=conv.partner_name or "").strip(),
                 (t("Question: “{text}”", text=q[:120]) + "\n" if q else "")
                 + t("Suggestion: “{text}”", text=d.text[:300]) + f"\n{conv.ad_title or ''} · {reason}",
                 url=f"{get_settings().public_url.rstrip('/')}/chats/{conv.id}",
                 data={"conversation_id": conv.id, "ad_id": conv.ad_id, "draft_id": d.id},
                 actions=ha_actions.draft_actions(d.id, can_send), tag=ha_actions.tag_for(d.id))


async def revise_and_offer(draft_id: int, instruction: str) -> Draft | None:
    """"Revise with Claude" from the push: new draft from the instruction, old one dropped, new push."""
    with session() as db:
        old = db.get(Draft, draft_id)
        conv = db.get(Conversation, old.conversation_id) if old else None
    if old is None or conv is None or old.status != "draft":
        return None
    # German on purpose: both texts go into the prompt
    instruction = instruction.strip() or "Formuliere die Antwort neu."
    hint = f"Mein bisheriger Entwurf war: „{old.text}“. Überarbeite ihn so: {instruction}"
    new = await draft_reply(conv.id, hint=hint, auto=bool(old.auto))
    discard_draft(draft_id)
    with session() as db:
        row = db.get(Draft, new.id)
        row.auto_reason = t("revised as asked: “{instruction}”", instruction=instruction[:80])
        db.add(row)
        db.commit()
        db.refresh(row)
    await offer_draft(conv, row, t("revised: {instruction}", instruction=instruction[:80]))
    return row


def mark_sent(draft_id: int, text: str | None = None) -> Draft:
    """Mark a draft as sent (also used when the user sent it manually on willhaben)."""
    with session() as db:
        draft = db.get(Draft, draft_id)
        if text:
            draft.text = text.strip()
        draft.status, draft.sent_at, draft.error = "sent", utcnow(), None
        db.add(draft)
        ext = f"draft-{draft.id}"
        _upsert_message(db, draft.conversation_id, ext, "out", draft.text, draft.sent_at)
        conv = db.get(Conversation, draft.conversation_id)
        if conv:
            conv.unread_count = 0
            db.add(conv)
        db.commit()
        db.refresh(draft)
    if draft.meetup:                           # the answer fixed an appointment: the chat page offers it
        meetups.remember(draft.conversation_id, draft.meetup)
    return draft


def discard_draft(draft_id: int) -> None:
    with session() as db:
        draft = db.get(Draft, draft_id)
        if draft:
            draft.status = "discarded"
            db.add(draft)
            db.commit()
