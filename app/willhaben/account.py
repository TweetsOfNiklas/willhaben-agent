"""Account-level willhaben endpoints (need the logged-in BrowserSession).

Verification status (see docs/willhaben-api.md):
  VERIFIED   userprofile/me, chat-api/v1/counter, chat-api/v1/conversations (list),
             chat-api/v1/conversations/{id}/messages (first MESSAGE_CANDIDATES entry),
             my adverts (server-rendered in /iad/myprofile/myadverts __NEXT_DATA__)
  FROM BUNDLE send message (chat WebSocket send_message; confirmed per send via the REST history)
  MISSING    reactivate, ad insertion -> need a request capture
Unverified write endpoints are never guessed: they raise EndpointUnknown until captured.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.i18n import t
from app.willhaben.parse import BASE, _iso, _ms, attrs, extract_next_data
from app.willhaben.session import BrowserSession, NotLoggedIn

log = logging.getLogger(__name__)

CHAT = "/webapi/chat-api/v1"
MYADVERTS_PAGE = "/iad/myprofile/myadverts"
CHAT_WS = "wss://chat-realtime-web.willhaben.at/webapi/chat-ws/v1/ws/"
MESSAGE_CANDIDATES = [
    CHAT + "/conversations/{id}/messages?limit=100&offset=0",
    CHAT + "/conversations/{id}/messages",
    CHAT + "/conversations/{id}",
]


class EndpointUnknown(RuntimeError):
    """The willhaben endpoint for this action has not been captured/verified yet."""


class SendFailed(RuntimeError):
    """willhaben did not take the message."""


@dataclass
class ConversationSummary:
    id: str
    ad_id: str | None
    ad_title: str | None
    partner_name: str | None
    unread_count: int
    last_text: str | None
    last_at: datetime | None
    raw: dict[str, Any]
    role: str | None = None  # seller | buyer, when the response says who posted the ad
    last_is_mine: bool | None = None  # the newest message was written by me (not news for a push)


@dataclass
class ChatMessage:
    ext_id: str
    text: str
    created_at: datetime | None
    direction: str  # in | out | unknown
    read: bool | None = None  # willhaben's read flag (for incoming messages: did I read it)


def _pick(d: dict[str, Any] | None, *keys: str) -> Any:
    """First non-empty value among dotted keys, e.g. _pick(x, 'lastMessage.text', 'lastMessageText')."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        cur: Any = d
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", []):
            return cur
    return None


def _when(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        return _ms(str(n if n > 1e11 else n * 1000))
    return _iso(str(v))


def _list(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    found = _pick(data, *keys)
    return [x for x in found if isinstance(x, dict)] if isinstance(found, list) else []


def _role(c: dict[str, Any]) -> str | None:
    me, seller = c.get("user_uuid"), _pick(c, "ad_info.user_uuid")
    if not me or not seller:
        return None
    return "seller" if me == seller else "buyer"


def parse_conversations(data: Any) -> list[ConversationSummary]:
    out = []
    for c in _list(data, "conversations", "data", "items", "content"):
        cid = _pick(c, "conversation_id", "id", "conversationId", "uuid", "conversationUuid")
        if cid is None:
            continue
        out.append(ConversationSummary(
            id=str(cid),
            ad_id=str(_pick(c, "ad_info.id", "adId", "advertId", "ad.id", "advert.id") or "") or None,
            ad_title=_pick(c, "ad_info.subject", "adTitle", "advertTitle", "ad.title", "advert.title", "subject"),
            partner_name=_pick(c, "participant_info.name", "partnerName", "partner.name", "partner.nickname", "otherParticipant.name", "contactName"),
            unread_count=int(_pick(c, "unseen", "unreadCount", "unreadMessages", "unread") or 0),
            last_text=_pick(c, "last_message.preview", "lastMessage.text", "lastMessage.body", "lastMessage.message", "lastMessageText", "preview"),
            last_at=_when(_pick(c, "last_message.timestamp", "updated_at", "lastMessage.createdAt", "lastMessage.timestamp", "lastMessageDate", "updatedAt", "lastActivity")),
            raw=c,
            role=_role(c),
            last_is_mine=_pick(c, "last_message.is_mine") if isinstance(_pick(c, "last_message.is_mine"), bool) else None,
        ))
    return out


def parse_messages(data: Any, my_ids: set[str]) -> list[ChatMessage]:
    out = []
    for m in _list(data, "messages", "data", "items", "content", "conversation.messages"):
        text = _pick(m, "text", "body", "message", "content", "messageText")
        if not isinstance(text, str):
            continue
        created = _when(_pick(m, "created_at", "createdAt", "timestamp", "sentAt", "date", "created"))
        ext = _pick(m, "message_id", "id", "messageId", "uuid")
        if ext is None:
            ext = hashlib.sha1(f"{created}|{text}".encode()).hexdigest()[:16]
        own = _pick(m, "is_mine", "isOwnMessage", "ownMessage", "own", "fromMe", "isMine")
        sender = _pick(m, "sender_id", "sender_uuid", "user_uuid", "senderId", "sender.id", "sender.uuid", "senderUuid", "userId", "from")
        direction = "unknown"
        kind = m.get("type")
        if isinstance(kind, str) and kind.endswith(("system_message", "integration_message")):
            pass  # willhaben notices (PayLivery, safety tips, status) - neither side wrote them
        elif isinstance(own, bool):
            direction = "out" if own else "in"
        elif sender is not None and my_ids:
            direction = "out" if str(sender) in my_ids else "in"
        elif isinstance(_pick(m, "direction", "type"), str):
            d = _pick(m, "direction", "type").lower()
            direction = "out" if d in ("out", "outgoing", "sent") else "in" if d in ("in", "incoming", "received") else "unknown"
        read = m.get("read")
        out.append(ChatMessage(ext_id=str(ext), text=text, created_at=created, direction=direction,
                               read=read if isinstance(read, bool) else None))
    out.sort(key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc))
    return out


@dataclass
class OwnAdvert:
    ad_id: str
    heading: str
    status: str            # willhaben advertStatus.id: active, expired, deactivated, sold, ...
    status_label: str | None
    price_display: str | None
    start: datetime | None
    end: datetime | None
    url: str
    image_url: str | None


def parse_my_adverts(next_data: dict[str, Any]) -> list[OwnAdvert]:
    """Own ads from the `__NEXT_DATA__` of /iad/myprofile/myadverts (all verticals, first 50)."""
    try:
        lst = next_data["props"]["pageProps"]["initialUserAdsList"]
    except (KeyError, TypeError) as e:
        raise ValueError(t("Page contains no ad list")) from e
    out = []
    for vertical in lst.get("verticalList") or []:
        for ad in vertical.get("advertDetails") or []:
            ad_id = str(ad.get("id") or "")
            if not ad_id:
                continue
            a = attrs(ad)
            status = ad.get("advertStatus") or {}
            images = (ad.get("advertImageList") or {}).get("advertImage") or []
            out.append(OwnAdvert(
                ad_id=ad_id,
                heading=ad.get("description") or "",
                status=status.get("id") or "unknown",
                status_label=status.get("description"),
                price_display=(a.get("PRICE_FOR_DISPLAY") or [None])[0],
                start=_iso(ad.get("startDate")),
                end=_iso(ad.get("endDate")),
                url=f"{BASE}/iad/object?adId={ad_id}",
                image_url=(images[0].get("mainImageUrl") or images[0].get("thumbnailImageUrl")) if images else None,
            ))
    return out


class AccountClient:
    def __init__(self, session: BrowserSession) -> None:
        self.s = session
        self._messages_path: str | None = None

    async def me(self) -> dict[str, Any]:
        return await self.s.api_json("GET", "/webapi/userprofile-service/userprofile/me")

    async def unread_count(self) -> int:
        data = await self.s.api_json("GET", CHAT + "/counter")
        if isinstance(data, (int, float)):
            return int(data)
        v = _pick(data, "count", "unreadCount", "unread", "total", "unreadConversations")
        return int(v or 0)

    async def conversations(self, limit: int = 50, offset: int = 0) -> list[ConversationSummary]:
        data = await self.s.api_json("GET", f"{CHAT}/conversations?limit={limit}&offset={offset}")
        return parse_conversations(data)

    def _my_ids(self) -> set[str]:
        u = self.s.user or {}
        return {str(v) for v in (u.get("id"), u.get("uuid")) if v}

    async def messages(self, conversation_id: str) -> list[ChatMessage]:
        """Messages of one conversation. Endpoint not yet verified: tries read-only candidates."""
        paths = [self._messages_path] if self._messages_path else MESSAGE_CANDIDATES
        for tmpl in paths:
            r = await self.s.api("GET", tmpl.format(id=conversation_id))
            if not r.ok:
                continue
            try:
                msgs = parse_messages(r.json(), self._my_ids())
            except ValueError:
                continue
            if msgs:
                if self._messages_path != tmpl:
                    log.info("chat messages endpoint works: %s", tmpl)
                self._messages_path = tmpl
                return msgs
        raise EndpointUnknown(t(
            "Messages endpoint not verified yet. Start a recording named 'chat' under 'Session' "
            "and open a conversation in the browser window."
        ))

    async def mark_read(self, conversation_id: str, msgs: list[ChatMessage] | None = None) -> int:
        """Send read receipts for unread incoming messages, like the web app does when they scroll into view.

        Frame from the web app bundle (2026-10-09): {req_type: "mark_message_as_read",
        payload: {conversation_id, message_id}}, no request_id, fire-and-forget. Returns how many were marked.
        """
        if msgs is None:
            msgs = await self.messages(conversation_id)
        unread = [m for m in msgs if m.direction == "in" and m.read is False]
        if not unread:
            return 0
        frames = [{"payload": {"conversation_id": conversation_id, "message_id": m.ext_id},
                   "req_type": "mark_message_as_read"} for m in unread]
        await self.s.ws_send(CHAT_WS, frames)
        return len(unread)

    async def ping_chat(self) -> dict[str, Any]:
        """Health check of the chat WebSocket (the web app's own ping); sends no message."""
        return await self.s.ws_request(CHAT_WS, {"req_type": "ping"}, None, req_type="ping", timeout_ms=10000)

    async def send_message(self, conversation_id: str, text: str) -> str:
        """Send a text message the way the chat web app does (WebSocket `send_message`).

        Taken from the web app's bundle (2026-10-09); afterwards the message is looked up via the
        REST history, so a send only counts once willhaben shows it. Returns willhaben's message_id.
        """
        text = text.strip()
        if not text:
            raise ValueError(t("Empty message"))
        client_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
        frame = {"payload": {"conversation_id": conversation_id, "client_message_id": client_id,
                             "text": text, "type": "text"},
                 "req_type": "send_message", "request_id": request_id}
        res = await self.s.ws_request(CHAT_WS, frame, request_id, req_type="send_message")
        if res.get("status") == "error":
            raise SendFailed(t("willhaben rejected the message: {detail}", detail=res.get('response') or res.get('error') or res))
        if res.get("status") in ("ws_error", "closed") and not res.get("acked"):
            raise SendFailed(t("Chat connection failed ({status})", status=f"{res.get('status')} {res.get('code') or ''}".strip()))
        # success, or only an ack before the timeout: the history decides
        for attempt in range(3):
            await asyncio.sleep(1.5 * (attempt + 1))
            data = await self.s.api_json("GET", f"{CHAT}/conversations/{conversation_id}/messages?limit=10&offset=0")
            for m in (data or {}).get("messages") or []:
                if m.get("client_message_id") == client_id or (m.get("is_mine") and (m.get("text") or "").strip() == text):
                    return str(m.get("message_id") or client_id)
        raise SendFailed(t("Message did not show up in the history (response: {status}) – check on willhaben",
                           status=res.get('status')))

    async def my_adverts(self) -> list[OwnAdvert]:
        """The account's ads; willhaben renders them server-side into the my-adverts page."""
        r = await self.s.api("GET", MYADVERTS_PAGE, accept="text/html")
        nd = extract_next_data(r.text) if r.ok else None
        if not nd or nd.get("page") != MYADVERTS_PAGE:   # anonymous users land on the login page
            raise NotLoggedIn(t("{page}: no ad list (HTTP {status}, logged in?)", page=MYADVERTS_PAGE, status=r.status))
        return parse_my_adverts(nd)

    async def reactivate(self, ad_id: str) -> None:
        raise EndpointUnknown(t(
            "Reactivation endpoint not captured yet (record 'reaktivieren' while clicking "
            "'Wieder aktivieren' on an expired ad)."
        ))

    async def publish_ad(self, draft: dict[str, Any]) -> str:
        raise EndpointUnknown(t("Ad posting not captured yet (recording 'anzeige-aufgeben')."))
