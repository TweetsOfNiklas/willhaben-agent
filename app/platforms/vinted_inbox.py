"""Vinted messages: first step, finding out how Vinted's web app reads its inbox (not seen in the app's recordings yet).

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
        return t("Vinted: not logged in in the browser window – log in there, then press again right away")
    calls = sum(1 for r in inbox["requests"] if _vinted_data(r["url"]) and not r.get("blocked"))
    conv = t("conversation read") if res.get("conversation") else t("no conversation found")
    return t("Vinted inbox read ({n} data calls), {conv}", n=calls, conv=conv)
