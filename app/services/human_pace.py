"""Human-like timing for search agents: irregular intervals and the occasional skipped check.

A person refreshing a search does not do it every 300.0 seconds. Each agent's interval varies by
±variance/2 around the configured value (same average), and with breaks on, about one scheduled check in
BREAK_EVERY is left out. Manual "check now" runs are never affected; quiet hours apply on top.
"""
from __future__ import annotations

import random

from app.db import get_setting, set_setting

BREAK_EVERY = 12            # ~1 in 12 scheduled checks skipped when breaks are on
MAX_VARIANCE = 80


def config() -> dict:
    try:
        pct = int(get_setting("agent_variance_pct") or 30)
    except ValueError:
        pct = 30
    return {"variance_pct": max(0, min(MAX_VARIANCE, pct)), "breaks": get_setting("agent_breaks") != "0"}


def save(variance_pct: int, breaks: bool) -> None:
    set_setting("agent_variance_pct", str(max(0, min(MAX_VARIANCE, variance_pct))))
    set_setting("agent_breaks", "1" if breaks else "0")


def trigger(interval_s: int, min_interval_s: int) -> tuple[int, int]:
    """(base seconds, jitter seconds) for APScheduler: each run lands in [base, base + jitter], centred on interval."""
    spread = int(interval_s * config()["variance_pct"] / 100)
    if spread <= 0:
        return interval_s, min(60, interval_s // 5)
    base = max(interval_s - spread // 2, int(min_interval_s * 0.85))
    return base, spread


def take_break(rng: random.Random | None = None) -> bool:
    return config()["breaks"] and (rng or random).random() < 1 / BREAK_EVERY
