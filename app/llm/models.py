"""The selectable Claude models, fetched from the API (GET /v1/models) with the subscription token.

The list is fetched whenever Settings → Claude is opened (a short in-memory cache keeps quick reloads from hitting
the API twice), so a new model can be chosen without a release of this app. The last good list is stored in the
`claude_models` setting for when the API is unreachable; before the first fetch the static list in app/llm/base.py
is used. Prices are not part of the API, so the cost factors (base.PRICES) stay a table.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.db import get_setting, set_setting, utcnow
from app.llm.base import MODELS
from app.llm.claude_login import claude_token

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/models"
MAX_AGE_S = 30            # a second opening of the page within this time reuses the fetched list
LINE_ORDER = {"opus": 0, "fable": 1, "sonnet": 2, "haiku": 3}   # the selector's order, most capable first

_cache: dict[str, Any] = {"at": 0.0, "models": None}
last_fetch: dict[str, Any] = {"ok": None, "at": None, "error": None}


def line_of(model: str | None) -> str:
    """"claude-sonnet-5-5" / "sonnet" / "claude-sonnet-4-5-20250929" -> "sonnet" (the model family)."""
    parts = (model or "").split("-")
    return parts[1] if len(parts) > 1 and parts[0] == "claude" else parts[0]


def _parse(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The API's model objects -> [{id, name, line, created_at}], retired models and non-Claude ids left out,
    sorted by line (Opus first) and then newest first."""
    out = []
    for m in data.get("data") or []:
        mid = str(m.get("id") or "")
        if not mid.startswith("claude-") or m.get("lifecycle") == "retired":
            continue
        name = str(m.get("display_name") or mid)
        out.append({"id": mid, "name": name[7:] if name.startswith("Claude ") else name,
                    "line": str(m.get("line") or line_of(mid)), "created_at": str(m.get("created_at") or "")})
    return sorted(out, key=lambda m: (LINE_ORDER.get(m["line"], 9), m["line"]))   # stable: API order (newest first) within a line


def stored() -> list[dict[str, Any]]:
    """The last list fetched from the API (persisted), [] before the first fetch."""
    try:
        return [m for m in json.loads(get_setting("claude_models") or "[]") if isinstance(m, dict) and m.get("id")]
    except (ValueError, TypeError):
        return []


def available() -> list[dict[str, Any]]:
    """[{id, name, line}] without a network call: the fetched list, else the stored one, else the static fallback."""
    if not _cache["models"]:
        _cache["models"] = stored() or None         # kept in memory ("at" stays 0, so refresh() still fetches)
    return _cache["models"] or [{"id": m, "name": n, "line": line_of(m)} for m, n in MODELS]


def choices() -> list[tuple[str, str]]:
    """(id, name) pairs for the selector."""
    return [(m["id"], m["name"]) for m in available()]


def ids() -> set[str]:
    return {m["id"] for m in available()}


def label(model: str | None) -> str:
    """The short name of a model id ("Sonnet 5.5"); an alias such as "opus" is shown as its line ("Opus"),
    anything unknown as the raw id."""
    for m in available():
        if m["id"] == model:
            return m["name"]
    line = line_of(model)
    return line.capitalize() if line in LINE_ORDER else (model or "?")


def newest(line: str) -> str | None:
    """The newest model of a line ("haiku" -> "claude-haiku-5-5"), None if the line is unknown."""
    return next((m["id"] for m in available() if m["line"] == line), None)


def resolve_alias(model: str) -> str:
    """"haiku" / "sonnet" / "opus" (LLM_MODEL from .env, the CLI's aliases) -> the newest model of that line;
    a full id is returned as is."""
    return model if model.startswith("claude-") else (newest(model) or model)


async def refresh(max_age_s: float = MAX_AGE_S) -> list[dict[str, Any]]:
    """Fetch the list from the API unless it was fetched less than max_age_s ago; on any error the list from
    `available()` is returned and last_fetch says what went wrong. Never raises."""
    if _cache["models"] and time.monotonic() - _cache["at"] < max_age_s:
        return _cache["models"]
    token = claude_token()
    if not token:
        last_fetch.update(ok=False, at=utcnow(), error="not signed in")
        return available()
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as c:
            r = await c.get(API_URL, params={"limit": 100},
                            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                                     "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            models = _parse(r.json())
        if not models:
            raise ValueError("no models in the response")
    except Exception as e:  # noqa: BLE001 - the settings page must open without the API
        last_fetch.update(ok=False, at=utcnow(), error=f"{type(e).__name__}: {e}"[:200])
        log.warning("could not fetch the Claude model list: %s", last_fetch["error"])
        return available()
    _cache.update(at=time.monotonic(), models=models)
    last_fetch.update(ok=True, at=utcnow(), error=None)
    if models != stored():
        set_setting("claude_models", json.dumps(models, ensure_ascii=False))
    return models
