"""Offline replay: answer willhaben requests from the stored recordings (WH_REPLAY=1).

Every request the app sends is looked up by its canonical key (app/willhaben/recorder.py: method, URL without
volatile parameters, body without request ids and timestamps). The newest usable recording answers it: the request
never reaches willhaben, the rate limiter does not wait and nothing is counted as an access. A request that was never
recorded goes to willhaben as usual and is recorded, so the set of recordings grows while you use the app.

Usable means: a stored, complete answer (not cut off, not pruned, not a cache entry) that is not a block (403/429) or a
network error. Writes (POST/PUT/DELETE, chat messages) replay only the exact same request, so a different chat text
or a different ad edit is never answered with an old result.

With BROWSER_ENABLED=0 the account side has no browser at all (session.ReplaySession): recorded account calls work,
anything else fails with "not recorded". Pushed live-chat frames are recorded but not played back.

Anonymous modules import this file: it must never import the logged-in session (tests/test_anonymous_public.py).
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.willhaben import recorder

log = logging.getLogger(__name__)

_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "set-cookie"}


def enabled() -> bool:
    return bool(get_settings().wh_replay)


def find(method: str, url: str, body: Any = None, *, upload: tuple[str, str] | None = None):
    """The newest usable recording of this request, or None."""
    from sqlalchemy import or_
    from sqlmodel import col, select

    from app.db import WhRecording, session

    key = recorder.canonical_key(method, url, body, upload=upload)
    q = (select(WhRecording)
         .where(WhRecording.canonical_key == key, WhRecording.method == method.upper(),
                col(WhRecording.resp_body).is_not(None), col(WhRecording.resp_truncated).is_(False),
                col(WhRecording.pruned).is_(False), col(WhRecording.cached).is_(False),
                col(WhRecording.outcome).in_(("ok", "client_error")),
                or_(col(WhRecording.status).is_(None), col(WhRecording.status).not_in((403, 429))))
         .order_by(col(WhRecording.id).desc()).limit(1))
    try:
        with session() as db:
            return db.exec(q).first()
    except Exception as e:  # noqa: BLE001 - replay lookups never break a request: it goes to willhaben instead
        log.warning("replay lookup failed for %s %s: %s", method, url, e)
        return None


def note(rec, kind: str) -> None:
    """A timeline entry for the replayed request (no new recording, no access count)."""
    from app.services import timeline

    try:
        path = urlsplit(rec.url).path
    except ValueError:
        path = rec.url
    status = rec.status if rec.status is not None else rec.ws_status
    prefix = f"[{rec.platform}] " if rec.platform and rec.platform != "willhaben" else ""
    timeline.record("wh", f"{prefix}{rec.method} {path[:160]} → {status} (replay)", name=kind,
                    payload={k: v for k, v in {"replayed_from": rec.id, "status": status, "channel": rec.channel,
                                               "replay": True, "platform": rec.platform}.items() if v is not None})


def body_text(rec) -> str:
    raw = recorder.decode_body(rec.resp_body)
    return raw.decode("utf-8", "replace") if raw is not None else ""


def as_httpx_response(rec, url: str) -> httpx.Response:
    headers = {k: v for k, v in (json.loads(rec.resp_headers) if rec.resp_headers else {}).items()
               if k.lower() not in _DROP_HEADERS and v != "<redacted>"}
    return httpx.Response(rec.status or 200, headers=headers, content=recorder.decode_body(rec.resp_body) or b"",
                          request=httpx.Request("GET", rec.final_url or url), extensions={"wh_replayed": rec.id})


def as_fetch_response(rec, url: str):
    """The recording as what app.platforms.httpclient transports return."""
    from app.platforms.httpclient import FetchResponse

    headers = {k: v for k, v in (json.loads(rec.resp_headers) if rec.resp_headers else {}).items()
               if k.lower() not in _DROP_HEADERS and v != "<redacted>"}
    return FetchResponse(rec.status or 200, headers, recorder.decode_body(rec.resp_body) or b"", rec.final_url or url,
                         extensions={"wh_replayed": rec.id})


def as_api_response(rec) -> tuple[int, str, str]:
    """(status, text, url) for session.ApiResponse."""
    return rec.status or 200, body_text(rec), rec.final_url or rec.url


def as_ws_result(rec) -> dict[str, Any]:
    try:
        data = json.loads(body_text(rec))
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {"status": rec.ws_status or "ok"}
