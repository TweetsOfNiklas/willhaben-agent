"""Counts of willhaben requests per minute (table AccessCount), for the footer and /stats/willhaben.

Only aggregated, non-personal metadata is kept: (UTC minute, channel, kind, outcome) -> count. The path is reduced to a
coarse kind on the spot and thrown away, so no URL, query value, ad id, conversation id or message text is ever stored.

Recording is a dict increment in memory (never raises, never touches the disk in the request path); the scheduler job
`access-log` writes the buffer every minute in a worker thread and prunes rows older than RETENTION_DAYS, and the app
flushes once more on shutdown. A crash loses at most the last minute of counts. A synchronous SQLite write per request
would be simpler but a commit can stall the event loop for tens of milliseconds on a busy disk.

Channels: anonymous = PublicClient (search agents, screening, Preischeck, ad viewer); account = the logged-in browser's
REST calls, uploads and page navigations; chat = chat WebSocket calls (send, read receipts, ping) plus every connect /
reconnect of the live chat socket (one long connection: its frames and pings are not counted). Image downloads from
willhaben's CDN for hit screening count as anonymous/other. Clicks, typing and scrolling in the remote view are not
counted (the page loads they cause happen inside the browser); its back / reload buttons are.

A 403 counts as `blocked`. For the account channel it is often just an expired login or a refused CSRF token rather than
anti-bot blocking.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

CHANNELS = ("anonymous", "account", "chat")
KINDS = ("search", "detail", "filters", "categories", "myads", "chat", "send", "publish", "upload", "login",
         "navigation", "other")
OUTCOMES = ("ok", "client_error", "blocked", "error", "cached")   # cached = served from PublicClient's cache, no access
RETENTION_DAYS = 8
FLUSH_INTERVAL_S = 60
_MAX_BUFFER_KEYS = 5000            # if the DB stays unwritable: drop counts rather than grow without bound

_buffer: dict[tuple[int, str, str, str], int] = {}
_last_prune = [0.0]

# first match wins; paths are lowercased and stripped of host and query before matching
_RULES: list[tuple[re.Pattern[str], str]] = [(re.compile(p), k) for p, k in (
    (r"/iad-upload/", "upload"),
    (r"/iad-messaging/sendrequest", "send"),
    (r"/chat-api/|/iad-messaging/|/chat-ws/", "chat"),
    (r"/userprofile|/sso/|/myprofile/login", "login"),
    (r"/bap/categorysuggestion|/anzeigenaufgabe", "categories"),
    (r"^/webapi/myatz/", "myads"),                     # own ads: delete, deactivate, ...
    (r"/bap/(presave|save|edit)$|/bap/\d+$|/iad/products/|/p2p-aggregator/", "publish"),
    (r"/myprofile/|/soldreasons|/bap/", "myads"),
    (r"/ad-search/|^/iad/(kaufen-und-verkaufen|immobilien|gebrauchtwagen|jobs)/(?!d/)", "search"),
    (r"^/iad/object$|/d/", "detail"),
    (r"^/robots\.txt$|^/?$", "navigation"),
)]


def classify(url: str) -> str:
    """Coarse kind of a willhaben URL or path; the path itself is never kept."""
    try:
        path = urlsplit(url).path.lower().rstrip("/") or "/"
    except ValueError:
        return "other"
    for rx, kind in _RULES:
        if rx.search(path):
            return kind
    return "other"


def outcome_for_status(status: int | None) -> str:
    if status is None or status < 400:
        return "ok"
    if status in (403, 429):
        return "blocked"
    return "client_error" if status < 500 else "error"


def ws_outcome(status: Any) -> str:
    """Result of a one-shot chat WebSocket call ({status: ok|success|error|timeout|ws_error|closed})."""
    if status in ("ok", "success"):
        return "ok"
    return "client_error" if status == "error" else "error"


def now_minute() -> int:
    return int(time.time() // 60)


def record(channel: str, kind: str, outcome: str = "ok", n: int = 1) -> None:
    """Count one willhaben access. Never raises; unknown values collapse to fixed words (nothing free-form is stored)."""
    try:
        key = (now_minute(), channel if channel in CHANNELS else "account", kind if kind in KINDS else "other",
               outcome if outcome in OUTCOMES else "error")
        if key in _buffer or len(_buffer) < _MAX_BUFFER_KEYS:
            _buffer[key] = _buffer.get(key, 0) + n
    except Exception:  # noqa: BLE001 - statistics must never break a request
        pass


class track:
    """`with track(channel, kind) as tr: ...; tr.status = 200` records the outcome, or `error` on an exception."""

    def __init__(self, channel: str, kind: str) -> None:
        self.channel, self.kind = channel, kind
        self.status: int | None = None
        self.outcome: str | None = None

    def __enter__(self) -> track:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        record(self.channel, self.kind, "error" if exc_type else (self.outcome or outcome_for_status(self.status)))
        return False


def pending() -> dict[tuple[int, str, str, str], int]:
    """Counts not yet written to the database (readers add them so the numbers are current)."""
    return dict(_buffer)


def reset() -> None:
    """Tests: forget unflushed counts."""
    _buffer.clear()


def _write(items: dict[tuple[int, str, str, str], int]) -> None:
    from sqlalchemy.dialects.sqlite import insert

    from app.db import AccessCount, get_engine

    rows = [{"minute": m, "channel": c, "kind": k, "outcome": o, "count": n} for (m, c, k, o), n in items.items()]
    stmt = insert(AccessCount.__table__)
    stmt = stmt.on_conflict_do_update(index_elements=["minute", "channel", "kind", "outcome"],
                                      set_={"count": AccessCount.__table__.c.count + stmt.excluded.count})
    with get_engine().begin() as conn:
        conn.execute(stmt, rows)


def _take() -> dict[tuple[int, str, str, str], int]:
    items = dict(_buffer)
    _buffer.clear()
    return items


def _restore(items: dict[tuple[int, str, str, str], int]) -> None:
    for key, n in items.items():
        if key in _buffer or len(_buffer) < _MAX_BUFFER_KEYS:
            _buffer[key] = _buffer.get(key, 0) + n


def flush() -> None:
    """Write the buffer synchronously (shutdown, tests)."""
    items = _take()
    if not items:
        return
    try:
        _write(items)
    except Exception as e:  # noqa: BLE001
        log.warning("access log flush failed: %s", e)
        _restore(items)


def prune(now_min: int | None = None) -> int:
    """Delete rows older than RETENTION_DAYS; returns the number of rows removed."""
    from sqlalchemy import delete

    from app.db import AccessCount, get_engine

    cutoff = (now_min if now_min is not None else now_minute()) - RETENTION_DAYS * 1440
    with get_engine().begin() as conn:
        return conn.execute(delete(AccessCount.__table__).where(AccessCount.__table__.c.minute < cutoff)).rowcount or 0


async def flush_job() -> None:
    """Scheduler job: write the buffer off the event loop, prune once an hour."""
    items = _take()
    try:
        if items:
            await asyncio.to_thread(_write, items)
    except Exception as e:  # noqa: BLE001 - keep the job alive, retry next minute
        log.warning("access log flush failed: %s", e)
        _restore(items)
    if time.monotonic() - _last_prune[0] > 3600:
        _last_prune[0] = time.monotonic()
        try:
            await asyncio.to_thread(prune)
        except Exception as e:  # noqa: BLE001
            log.warning("access log prune failed: %s", e)
