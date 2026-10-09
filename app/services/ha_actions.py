"""Buttons on Home Assistant pushes: approve, rewrite or discard a held-back automatic answer.

The HA companion app fires `mobile_app_notification_action` when a button is tapped. The app keeps a
websocket subscription to that event (long-lived token from ha_auth) and acts on our own action ids:

  WH_SEND_<draft>_<sig>      send the draft as it is
  WH_REPLY_<draft>_<sig>     "Direct answer": send the text typed into the push (behavior: textInput)
  WH_REVISE_<draft>_<sig>    "Revise with Claude": typed text is an instruction, a corrected draft comes as new push
  WH_DISCARD_<draft>_<sig>   drop the draft
  WH_CAL_<draft>_<sig>       add the appointment the (sent) answer agreed to Google Calendar

<sig> is an HMAC over verb+draft with a secret in DATA_DIR, so no other event source can make the app
send. A draft is only acted on while it is still a draft, so a second tap does nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Any

from app.config import get_settings
from app.i18n import t

log = logging.getLogger(__name__)

EVENT = "mobile_app_notification_action"
_ACTION_RE = re.compile(r"WH_(SEND|REPLY|REVISE|DISCARD|CAL)_(\d+)_([0-9a-f]{12})")


def _secret() -> bytes:
    p = get_settings().data_dir / "action-secret"
    try:
        return p.read_bytes()
    except OSError:
        key = secrets.token_bytes(32)
        p.write_bytes(key)
        p.chmod(0o600)
        return key


def _sig(verb: str, draft_id: int) -> str:
    return hmac.new(_secret(), f"{verb}:{draft_id}".encode(), hashlib.sha256).hexdigest()[:12]


def action_id(verb: str, draft_id: int) -> str:
    return f"WH_{verb}_{draft_id}_{_sig(verb, draft_id)}"


def tag_for(draft_id: int) -> str:
    return f"wh-draft-{draft_id}"


def draft_actions(draft_id: int, can_send: bool) -> list[dict[str, Any]]:
    """Buttons for a held-back answer. No plain "send" when the text still has [placeholders].

    Titles follow the ui_lang setting (pushes are built outside requests); the action ids never change.
    """
    actions = []
    if can_send:
        actions.append({"action": action_id("SEND", draft_id), "title": t("Send")})
    actions.append({"action": action_id("REPLY", draft_id), "title": t("Direct answer"), "behavior": "textInput",
                    "textInputButtonTitle": t("Send"), "textInputPlaceholder": t("Sent exactly as typed")})
    actions.append({"action": action_id("REVISE", draft_id), "title": t("Revise with Claude"), "behavior": "textInput",
                    "textInputButtonTitle": t("Revise"), "textInputPlaceholder": t("e.g. friendlier, offer €18")})
    actions.append({"action": action_id("DISCARD", draft_id), "title": t("Discard"), "destructive": True})
    return actions


def meetup_tag(draft_id: int) -> str:
    return f"wh-meetup-{draft_id}"


def meetup_actions(draft_id: int) -> list[dict[str, Any]]:
    return [{"action": action_id("CAL", draft_id), "title": t("Add to calendar")}]


def parse_action(action: str) -> tuple[str, int] | None:
    m = _ACTION_RE.fullmatch(action or "")
    if not m:
        return None
    verb, draft_id, sig = m.group(1), int(m.group(2)), m.group(3)
    if not hmac.compare_digest(sig, _sig(verb, draft_id)):
        log.warning("ignored push action with bad signature: %s", action)
        return None
    return verb, draft_id


async def handle(event_data: dict[str, Any]) -> str | None:
    """Act on one tapped button; returns a short result text for the log."""
    from app.db import Draft, session
    from app.services import chats
    from app.services.notify import clear_push, notify

    parsed = parse_action(str(event_data.get("action") or ""))
    if not parsed:
        return None
    verb, draft_id = parsed
    if verb == "CAL":
        return await _add_meetup(draft_id)
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is None or d.status != "draft":
        return None            # already handled (second tap, or done in the dashboard)
    await clear_push(tag_for(draft_id))
    if verb == "DISCARD":
        chats.discard_draft(draft_id)
        return "discarded"
    if verb == "REVISE":
        new = await chats.revise_and_offer(draft_id, str(event_data.get("reply_text") or ""))
        return f"revised -> draft {new.id}" if new else None
    text = d.text if verb == "SEND" else str(event_data.get("reply_text") or "").strip()
    if not text:
        return None
    try:
        await chats.send_draft(draft_id, text)
    except Exception as e:  # noqa: BLE001 - report every failure back to the phone
        await notify("auto_reply", t("Sending failed"), str(e)[:200],
                     url=f"{get_settings().public_url.rstrip('/')}/chats/{d.conversation_id}")
        return f"error: {e}"
    await notify("auto_reply", t("Sent"), text[:200], tag=tag_for(draft_id))
    return "sent"


async def _add_meetup(draft_id: int) -> str | None:
    from app.services import gcal, meetups
    from app.services.notify import clear_push, notify

    await clear_push(meetup_tag(draft_id))
    try:
        m = await meetups.add_from_draft(draft_id)
    except gcal.CalendarError as e:
        await notify("meetup", t("Calendar entry failed"), str(e)[:200])
        return f"error: {e}"
    if m is None:
        return None            # already added (second tap) or nothing to add
    await notify("meetup", t("In your calendar"), f"{meetups.format_when(m)} · {m['summary']}", url=m.get("event_link"))
    return "calendar"


async def listen_forever() -> None:
    """Keep a websocket subscription to HA push-button events; reconnect with backoff."""
    import websockets

    from app.services.ha_auth import ha_token, ha_url

    delay = 5
    while True:
        base, token = ha_url(), ha_token()
        if not (base and token):
            await asyncio.sleep(60)
            continue
        ws_url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
        try:
            async with websockets.connect(ws_url, open_timeout=15, ping_interval=30) as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    log.warning("HA push-button listener: auth failed")
                    await asyncio.sleep(300)
                    continue
                await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": EVENT}))
                log.info("HA push-button listener connected")
                delay = 5
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "event":
                        continue
                    data = (msg.get("event") or {}).get("data") or {}
                    try:
                        result = await handle(data)
                        if result:
                            log.info("push action %s: %s", str(data.get("action"))[:20], result)
                    except Exception:  # noqa: BLE001 - one bad event must not kill the listener
                        log.exception("push action failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - network, HA restart, token revoked
            log.info("HA push-button listener disconnected: %s (retry in %ss)", e, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)
