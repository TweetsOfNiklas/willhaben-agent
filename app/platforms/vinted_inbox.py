"""Vinted messages: first step, finding out how Vinted's web app reads its inbox (not seen in the app's recordings yet).

explore_once() runs once after a start with the browser logged in to Vinted: it opens the inbox and its newest
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
from typing import Any

from app.db import get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

SETTING = "vinted_inbox_explored"       # the version of the exploration that ran (once per version)
VERSION = "2"          # 1 blocked the token refresh: Vinted then expired its cookies (logged out)
ORIGIN = "https://www.vinted.at"
_CONV_ID = re.compile(r"/inbox/(\d{5,})")
_ID_IN_JSON = re.compile(r'"(?:conversation_id|id)"\s*:\s*(\d{6,})')


def _vinted_data(url: str) -> bool:
    return "vinted." in url and "/api/" in url


# Vinted's page renews its access token with a POST before the inbox loads (/session-refresh); blocked, it calls
# /web/api/auth/expire-cookies and the browser is logged out (seen 2026-10-21). The refresh only renews the login.
_REFRESH = re.compile(r"^https://www\.vinted\.[a-z.]+/web/api/auth/refresh(?:\?|$)")
_EXPIRE = re.compile(r"^https://www\.vinted\.[a-z.]+/web/api/auth/expire-cookies")


def _allow(method: str, url: str) -> bool:
    return method == "POST" and bool(_REFRESH.match(url))


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


async def explore_once(delay_s: float = 60) -> None:
    """Startup: the exploration once per VERSION (a failure is logged and tried again at the next start)."""
    if get_setting(SETTING) == VERSION:
        return
    try:
        if await explore(delay_s) is not None:
            set_setting(SETTING, VERSION)
    except Exception as e:  # noqa: BLE001 - only an exploration
        log.warning("vinted inbox exploration failed: %s", e)
