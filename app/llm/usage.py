"""Token usage of every Claude call, per purpose (chat, ads, screen, …) and model.

`claude -p --output-format json` reports the exact usage of a call: `modelUsage` per model (a call can use more than
one model internally) and an overall `usage` block. One LLMCall row is stored per model and call; failed calls are
stored too (without tokens), so the dashboard can show error counts. Recording never breaks the call itself.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlmodel import select

from app.db import LLMCall, session, utcnow

log = logging.getLogger(__name__)


def _rows_from(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Per-model token counts from the CLI result (camelCase modelUsage, else the snake_case usage block)."""
    data = data or {}
    out = []
    for model, u in (data.get("modelUsage") or {}).items():
        if isinstance(u, dict):
            out.append({"model": model, "input": int(u.get("inputTokens") or 0), "output": int(u.get("outputTokens") or 0),
                        "cache_read": int(u.get("cacheReadInputTokens") or 0),
                        "cache_write": int(u.get("cacheCreationInputTokens") or 0),
                        "cost_usd": float(u.get("costUSD") or 0)})
    if not out and isinstance(data.get("usage"), dict):
        u = data["usage"]
        out.append({"model": None, "input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
                    "cache_read": int(u.get("cache_read_input_tokens") or 0),
                    "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                    "cost_usd": float(data.get("total_cost_usd") or 0)})
    return out


def record(purpose: str | None, model: str, data: dict[str, Any] | None, *, ok: bool,
           duration_ms: int | None = None, error: str | None = None) -> None:
    try:
        rows = _rows_from(data) if ok else []
        with session() as db:
            if not rows:
                db.add(LLMCall(purpose=purpose or "other", model=model, ok=ok, duration_ms=duration_ms,
                               error=(error or "")[:300] or None))
            for i, r in enumerate(rows):
                db.add(LLMCall(purpose=purpose or "other", model=r["model"] or model, ok=ok,
                               duration_ms=duration_ms if i == 0 else None, input_tokens=r["input"],
                               output_tokens=r["output"], cache_read_tokens=r["cache_read"],
                               cache_write_tokens=r["cache_write"], cost_usd=r["cost_usd"], call_part=i))
            db.commit()
    except Exception as e:  # noqa: BLE001 - statistics must never break a Claude call
        log.warning("could not record LLM usage: %s", e)


def summary() -> dict[str, Any]:
    """Totals per (purpose, model) for today, the last 7 days and overall, plus a grand total."""
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total = {"calls": 0, "errors": 0, "today": 0, "week": 0, "all": 0, "cache_read": 0, "cost_usd": 0.0}
    with session() as db:
        rows = list(db.exec(select(LLMCall)))
    for r in rows:
        g = groups.setdefault((r.purpose, r.model), {"purpose": r.purpose, "model": r.model, "calls": 0, "errors": 0,
                                                     "today": 0, "week": 0, "all": 0, "input": 0, "output": 0,
                                                     "cache_read": 0, "cost_usd": 0.0, "last": None})
        ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=now.tzinfo)
        if not r.call_part:                     # count each call once, even when it used several models
            g["calls"] += 1
            total["calls"] += 1
            if not r.ok:
                g["errors"] += 1
                total["errors"] += 1
        tokens = (r.input_tokens or 0) + (r.output_tokens or 0)
        g["input"] += r.input_tokens or 0
        g["output"] += r.output_tokens or 0
        g["cache_read"] += r.cache_read_tokens or 0
        g["cost_usd"] += r.cost_usd or 0
        g["all"] += tokens
        total["all"] += tokens
        total["cache_read"] += r.cache_read_tokens or 0
        total["cost_usd"] += r.cost_usd or 0
        if ts >= week:
            g["week"] += tokens
            total["week"] += tokens
        if ts >= today:
            g["today"] += tokens
            total["today"] += tokens
        g["last"] = max(g["last"], ts) if g["last"] else ts
    order = {"chat": 0, "ads": 1, "screen": 2}
    return {"groups": sorted(groups.values(), key=lambda g: (order.get(g["purpose"], 9), g["purpose"], -g["all"])),
            "total": total}
