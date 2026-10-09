"""Home Assistant notifications: a bus event per hit/message plus optional notify service."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.db import log_event

log = logging.getLogger(__name__)

_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport


PUSH_CHANNELS = ("auto", "ha", "telegram", "both")


def push_channel() -> str:
    """Where phone pushes go. "auto": Telegram once a chat is linked, otherwise Home Assistant – never both."""
    from app.db import get_setting
    from app.services import telegram

    v = get_setting("push_channel") or "auto"
    if v == "auto":
        return "telegram" if telegram.active() else "ha"
    return v if v in PUSH_CHANNELS else "ha"


async def notify(kind: str, title: str, message: str, *, url: str | None = None,
                 image: str | None = None, data: dict[str, Any] | None = None,
                 actions: list[dict[str, Any]] | None = None, tag: str | None = None, push: bool = True,
                 channel: str | None = None) -> bool:
    """Fire `willhaben_<kind>` on the HA event bus and, if configured, a push via notify.<service>.

    Always records the event in the dashboard log and, if linked, posts it to Telegram (with buttons).
    Returns True if HA accepted the event. `channel` overrides the push channel setting (the connection
    watchdog uses "both" when one channel is broken).
    """
    log_event(kind, f"{title}: {message}", url)
    from app.services import telegram
    from app.services.ha_auth import ha_token, ha_url, notify_service

    channel = channel if channel in ("ha", "telegram", "both") else push_channel()
    # push=False: log + HA event only (e.g. a hit the Claude screening rejected)
    if push and channel in ("telegram", "both"):
        await telegram.send(kind, title, message, url=url, image=image, data=data, actions=actions, tag=tag)

    base, token, service = ha_url(), ha_token(), notify_service()
    if channel not in ("ha", "both"):
        service = None          # HA still gets the event (automations), just no phone push
    if not (base and token):
        return False
    payload = {"title": title, "message": message, "url": url, "image": image, **(data or {})}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=15, transport=_transport) as c:
            r = await c.post(f"{base}/api/events/willhaben_{kind}", json=payload, headers=headers)
            ok = r.status_code == 200
            if not ok:
                log.warning("HA event %s failed: %s %s", kind, r.status_code, r.text[:200])
            if service and push:
                push_data: dict[str, Any] = {}
                if url:
                    push_data.update({"url": url, "clickAction": url})
                if image:
                    push_data["image"] = image
                if actions:          # buttons in the HA companion app push
                    push_data["actions"] = actions
                if tag:              # same tag replaces / clears the push later
                    push_data["tag"] = tag
                await c.post(
                    f"{base}/api/services/notify/{service}",
                    json={"title": title, "message": message, "data": push_data},
                    headers=headers,
                )
            return ok
    except httpx.HTTPError as e:
        log.warning("HA notify failed: %s", e)
        return False


async def clear_push(tag: str) -> None:
    """Remove a push from the phone (HA companion app: message "clear_notification" with the tag)."""
    from app.services import telegram
    from app.services.ha_auth import ha_token, ha_url, notify_service

    await telegram.clear(tag)      # Telegram keeps the message, only its buttons go

    base, token, service = ha_url(), ha_token(), notify_service()
    if not (base and token and service):
        return
    try:
        async with httpx.AsyncClient(timeout=15, transport=_transport) as c:
            await c.post(f"{base}/api/services/notify/{service}", headers={"Authorization": f"Bearer {token}"},
                         json={"message": "clear_notification", "data": {"tag": tag}})
    except httpx.HTTPError as e:
        log.info("clear push failed: %s", e)
