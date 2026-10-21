"""Vinted messages: reading the inbox and its conversations into the chat list (sync), and the read-only
exploration that found how (explore).

Vinted's web app reads them from api.vinted.at/messaging/main/ (seen 2026-10-21 after logging in with Chrome):
  GET /messaging/main/inbox                   {conversations: [{id (uuid), conversation_type, created_at,
      is_unread_by_current_user, data.inventory_items[{id, title, photos}], last_message {id, created_at, sender_id,
      message_type, data.content}, opposite_users[{type, data{id, login_name, photo}}]}], pagination_temp}
  GET /messaging/main/conversations/<uuid>    {allow_reply, data{order_id, escrow_order_id}, messages[{id, created_at,
      sender_id, message_type, data}], opposite_users}; message_type "legacy_action": an event (data.entity title,
      subtitle); written messages carry their text in data (content/body)
They need Vinted's cookie, CSRF token and anon id, which only its own web app has. So the app does not call them: a
hidden tab of the app's browser (Google Chrome, logged in) opens /inbox or /inbox/<uuid> as a person would, and the
JSON answers of those calls are read on the way. Nothing is sent or marked from here.

explore() (the session page's "Explore the Vinted inbox", pressed right after logging in) opens the inbox and its newest
conversation in a hidden tab of the app's browser that is strictly read-only (session.read_hidden blocks every request
but GET/HEAD/OPTIONS, so nothing can be sent or changed), and keeps Vinted's own data calls in a capture file in
data/captures (local, like every capture: it holds chat content). The log names only the calls and their status.
Reading the chats into the chat list is built on what this finds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from app.db import utcnow

log = logging.getLogger(__name__)

ORIGIN = "https://www.vinted.at"
_CONV_ID = re.compile(r"/inbox/(\d{5,})")
_ID_IN_JSON = re.compile(r'"(?:conversation_id|id)"\s*:\s*(\d{6,})')


def _vinted_data(url: str) -> bool:
    return "vinted." in url and "/api/" in url


# What the read-only tab lets through besides GET (seen 2026-10-21):
# - the token refresh before the inbox loads (/web/api/auth/refresh): blocked, Vinted calls expire-cookies and the
#   browser is logged out
# - the bot protection's report (dd.vinted.lt/js), metrics, logs and tracking: blocked, the browser looks like a bot
#   and Vinted blocks the connection for a while ("Deine Sitzung wurde blockiert")
# Never through: writes to Vinted's own data API (/api/v2/…: messages, offers, purchases) and anything else.
_REFRESH = re.compile(r"^https://www\.vinted\.[a-z.]+/web/api/auth/refresh(?:\?|$)")
_EXPIRE = re.compile(r"^https://www\.vinted\.[a-z.]+/web/api/auth/expire-cookies")
_TELEMETRY = re.compile(r"^https://(?:dd\.vinted\.lt/|metrics\.vinted\.lt/|logs-ingress\.svc\.vinted\.com/|"
                        r"api\.vinted\.[a-z.]+/(?:j3r4zw/|tracking-events/))")


def _allow(method: str, url: str) -> bool:
    return method == "POST" and bool(_REFRESH.match(url) or _TELEMETRY.match(url))


def _deny(method: str, url: str) -> bool:
    return bool(_EXPIRE.match(url))


async def explore(delay_s: float = 0) -> dict[str, Any] | None:
    from app.willhaben.session import get_session

    if delay_s:
        await asyncio.sleep(delay_s)
    sess = get_session()
    if not sess.started:
        return None
    inbox = await sess.read_hidden(f"{ORIGIN}/inbox", r"Posteingang|Nachrichten|Inbox",
                                   timeout_ms=30000, keep=_vinted_data,
                                   allow=_allow, deny=_deny)
    conv_id = next(iter(_CONV_ID.findall(inbox["text"] or "")), None)
    if conv_id is None:
        for url, body in inbox["bodies"].items():
            if "inbox" in url or "conversation" in url:
                conv_id = next(iter(_ID_IN_JSON.findall(body)), None)
                if conv_id:
                    break
    conv = None
    if conv_id:
        conv = await sess.read_hidden(f"{ORIGIN}/inbox/{conv_id}", r"Nachricht|Artikel|Bestellung",
                                      timeout_ms=30000, keep=_vinted_data,
                                   allow=_allow, deny=_deny)
    out = {"label": "vinted-inbox-explore", "started_at": utcnow().isoformat(), "inbox": inbox, "conversation": conv,
           "conversation_id": conv_id}
    path = sess.settings.captures_dir / f"{utcnow():%Y%m%d-%H%M%S}-vinted-inbox-explore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    for name, res in (("inbox", inbox), ("conversation", conv)):
        if res is None:
            continue
        calls = [f"{r.get('method')} {re.sub(r'[?].*', '', r['url'])} {r.get('status', 'blocked' if r.get('blocked') else '')}"
                 for r in res["requests"] if _vinted_data(r["url"])]
        log.info("vinted %s: found=%s, %d data calls: %s", name, res["found"], len(calls),
                 "; ".join(re.sub(r"\d{5,}", "N", c) for c in calls[:25]))
    log.info("vinted inbox exploration saved as %s", path.name)
    return out


def summary(res: dict[str, Any] | None) -> str:
    """One line for the page: logged in or not, the inbox and the conversation read."""
    from app.i18n import t
    if res is None:
        return t("Browser session is not running")
    inbox = res["inbox"]
    if any("/member/" in r["url"] or "signup" in r["url"] for r in inbox["requests"] if r.get("type") == "document"):
        from app.services import site_accounts
        site_accounts._save("vinted", False)          # the stored state follows what the inbox showed
        return t("Vinted: not logged in in the browser window – log in there, then press again right away")
    calls = sum(1 for r in inbox["requests"] if _vinted_data(r["url"]) and not r.get("blocked"))
    conv = t("conversation read") if res.get("conversation") else t("no conversation found")
    return t("Vinted inbox read ({n} data calls), {conv}", n=calls, conv=conv)


# ------------------------------------------------------------------ sync into the chat list
SYNC_EVERY_S = 600            # a hidden tab of Vinted's inbox at most every 10 minutes (bot protection, not a poller)
MAX_DETAILS = 5               # conversations opened per sync (those with something new)
_last_sync = [0.0]


def _messaging(url: str) -> bool:
    return "/messaging/main/" in url


def _parse(body: str | None) -> dict[str, Any] | None:
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


def _when(raw: Any):
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def message_text(m: dict[str, Any]) -> tuple[str, str]:
    """(text, kind) of one Vinted message: a written one ("text"), or an event of the order ("integration")."""
    d = m.get("data") or {}
    if m.get("message_type") == "legacy_action" or "entity" in d:
        e = d.get("entity") or {}
        text = " – ".join(x for x in (e.get("title"), e.get("subtitle")) if x) or str(d.get("event_type") or "")
        return text, "integration"
    for key in ("body", "content", "text", "message"):
        if isinstance(d.get(key), str) and d[key].strip():
            return d[key].strip(), "text"
    return "", "text"


def _partner(c: dict[str, Any]) -> dict[str, Any]:
    users = c.get("opposite_users") or [{}]
    return (users[0] or {}).get("data") or {}


def apply_inbox(data: dict[str, Any]) -> list[str]:
    """The inbox list into Conversation rows (platform vinted). Returns the ids whose last message is new here."""
    from app.db import Conversation, session

    changed = []
    with session() as db:
        for c in data.get("conversations") or []:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            row = db.get(Conversation, cid) or Conversation(id=cid, platform="vinted")
            if row.removed:
                continue
            items = (c.get("data") or {}).get("inventory_items") or [{}]
            item = items[0] or {}
            last = c.get("last_message") or {}
            text, _ = message_text(last)
            at = _when(last.get("created_at") or c.get("created_at"))
            newer = at is not None and (row.last_message_at is None or at > row.last_message_at)
            partner = _partner(c)
            row.platform = "vinted"
            row.ad_id = str(item.get("id")) if item.get("id") else row.ad_id
            row.ad_title = item.get("title") or row.ad_title
            row.partner_name = partner.get("login_name") or row.partner_name
            row.unread_count = 1 if c.get("is_unread_by_current_user") else 0
            row.last_message_text = text or row.last_message_text
            row.last_message_at = at or row.last_message_at
            if row.ad_id:
                row.ad_context = {**(row.ad_context or {}), "title": row.ad_title,
                                  "url": f"{ORIGIN}/items/{row.ad_id}", "platform": "vinted"}
            row.synced_at = utcnow()
            db.add(row)
            if newer or row.messages_synced_at is None:
                changed.append(cid)
        db.commit()
    return changed


def apply_conversation(cid: str, data: dict[str, Any]) -> int:
    """One conversation's messages into Message rows; returns how many it holds."""
    from app.db import Conversation, session
    from app.services import chats

    partner_id = str(_partner(data).get("id") or "")
    msgs = data.get("messages") or []
    with session() as db:
        for m in msgs:
            text, kind = message_text(m)
            if not text:
                continue
            sender = str(m.get("sender_id") or "")
            direction = "unknown" if kind == "integration" else ("in" if sender and sender == partner_id else "out")
            chats._upsert_message(db, cid, f"vinted-{m.get('id')}", direction, text, _when(m.get("created_at")),
                                  authoritative=True, kind=kind)
        row = db.get(Conversation, cid)
        if row is not None:
            row.messages_synced_at = utcnow()
            side = (data.get("data") or {}).get("current_user_side")   # an item's conversation: buyer | seller
            if side in ("buyer", "seller"):
                row.role = side
            order = (data.get("data") or {}).get("order_id")
            if order:
                row.ad_context = {**(row.ad_context or {}), "order_id": str(order)}
            db.add(row)
        db.commit()
    return len(msgs)


async def sync_one(cid: str) -> int:
    """One conversation again (the chat's Reload): its page in a hidden tab."""
    from app.willhaben.session import get_session

    sess = get_session()
    if not sess.started:
        return 0
    r = await sess.read_hidden(f"{ORIGIN}/inbox/{cid}", r"Nachricht|Artikel|Bestellung|Zahlung", timeout_ms=30000,
                               keep=_messaging, allow=lambda method, url: True)
    conv = next((_parse(b) for u, b in r["bodies"].items() if f"/conversations/{cid}" in u), None)
    return apply_conversation(cid, conv) if conv is not None else 0


def is_vinted(conversation_id: str) -> bool:
    from app.db import Conversation, session

    with session() as db:
        c = db.get(Conversation, conversation_id)
    return bool(c and c.platform == "vinted")


async def sync(force: bool = False) -> dict[str, Any]:
    """The inbox and its changed conversations, through hidden tabs of the logged-in browser (at most every
    SYNC_EVERY_S unless forced). Skipped when the browser is not running or Vinted is known to be logged out."""
    from app.services import site_accounts
    from app.willhaben.session import get_session

    sess = get_session()
    if not sess.started or site_accounts.state("vinted")["logged_in"] is False:
        return {"skipped": "not logged in"}
    if not force and time.monotonic() - _last_sync[0] < SYNC_EVERY_S:
        return {"skipped": "recently"}
    _last_sync[0] = time.monotonic()
    every = lambda method, url: True  # noqa: E731 - Vinted's own page, as a person opens it: nothing blocked
    res = await sess.read_hidden(f"{ORIGIN}/inbox", r"Posteingang|Nachrichten|Inbox", timeout_ms=30000,
                                 keep=_messaging, allow=every)
    inbox = next((_parse(b) for u, b in res["bodies"].items() if "/messaging/main/inbox" in u), None)
    if inbox is None:
        if any("/member/" in r["url"] for r in res["requests"] if r.get("type") == "document"):
            site_accounts._save("vinted", False)
            return {"skipped": "not logged in"}
        return {"error": "no inbox answer"}
    changed = apply_inbox(inbox)
    read = 0
    for cid in changed[:MAX_DETAILS]:
        r = await sess.read_hidden(f"{ORIGIN}/inbox/{cid}", r"Nachricht|Artikel|Bestellung|Zahlung", timeout_ms=30000,
                                   keep=_messaging, allow=every)
        conv = next((_parse(b) for u, b in r["bodies"].items() if f"/conversations/{cid}" in u), None)
        if conv is not None:
            read += apply_conversation(cid, conv)
    log.info("vinted chats: %d conversations, %d opened, %d messages", len(inbox.get("conversations") or []),
             min(len(changed), MAX_DETAILS), read)
    return {"conversations": len(inbox.get("conversations") or []), "opened": min(len(changed), MAX_DETAILS)}

