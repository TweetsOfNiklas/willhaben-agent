"""Adaptive polling: search agents check less often in the hours that rarely bring new ads.

Each agent learns, from its hits of the last WINDOW_DAYS days, how many new ads each hour of the day (Vienna time)
brings: the ad's publication time where the site states one, otherwise when the agent first saw it. An hour with
half the average share of new ads gets a factor of 2, a quarter 4, and so on, up to the configured maximum; hours at
or above the average keep the agent's own interval: polling never gets faster than configured. An agent with fewer
than MIN_SAMPLES hits uses the profile of all agents together; with too few of those, nothing changes.

A scheduled check in a slow hour is skipped at random with the chance 1 - 1/factor, so the average pace follows the
factor and the timing stays irregular; once an agent has waited FORCE_AFTER times its stretched interval, it runs.
Quiet hours (app/services/quiet_hours.py) apply before this, "Check now" is never affected.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import col, select

from app.db import Hit, SearchAgent, get_setting, session, set_setting, utcnow

VIENNA = ZoneInfo("Europe/Vienna")
WINDOW_DAYS = 28
MIN_SAMPLES = 50
FORCE_AFTER = 2.0
CACHE_S = 3600
MAX_FACTORS = (2, 3, 4, 6, 8)
_cache: dict[int | None, tuple[float, "Profile"]] = {}


@dataclass
class Profile:
    factors: list[float]         # per hour of the day (Vienna), 1.0 = the agent's own interval
    shares: list[float]          # the hour's share of new ads, 1.0 = average
    samples: int
    pooled: bool                 # learned from all agents (this one has too few hits)


def enabled() -> bool:
    return get_setting("adaptive_enabled") != "0"


def max_factor() -> int:
    try:
        n = int(get_setting("adaptive_max_factor") or 4)
    except ValueError:
        n = 4
    return max(MAX_FACTORS[0], min(MAX_FACTORS[-1], n))


def save(on: bool, factor: int) -> None:
    set_setting("adaptive_enabled", "1" if on else "0")
    set_setting("adaptive_max_factor", str(max(MAX_FACTORS[0], min(MAX_FACTORS[-1], factor))))
    _cache.clear()


def _hours(agent_id: int | None) -> list[int]:
    """The local hour of every new ad of the window (one agent's, or all)."""
    since = utcnow() - timedelta(days=WINDOW_DAYS)
    with session() as db:
        q = select(Hit.published_at, Hit.first_seen_at).where(Hit.first_seen_at >= since)
        if agent_id is not None:
            q = q.where(Hit.agent_id == agent_id)
        rows = db.exec(q).all()
    out = []
    for published, first_seen in rows:
        published, first_seen = _aware(published), _aware(first_seen)
        # an ad published long before the agent saw it (a new agent's first check) says nothing about the hour
        ts = published if published and first_seen and abs(first_seen - published) < timedelta(days=1) else first_seen
        if ts is not None:
            out.append(ts.astimezone(VIENNA).hour)
    return out


def _aware(ts: datetime | None) -> datetime | None:
    return ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts


def _profile(hours: list[int], top: int, pooled: bool) -> Profile:
    counts = [0] * 24
    for h in hours:
        counts[h] += 1
    smooth = [(counts[(h - 1) % 24] + 2 * counts[h] + counts[(h + 1) % 24]) / 4 for h in range(24)]
    mean = sum(smooth) / 24 or 1
    shares = [s / mean for s in smooth]
    factors = [round(min(float(top), max(1.0, 1 / s if s > 0 else float(top))), 2) for s in shares]
    return Profile(factors=factors, shares=[round(s, 2) for s in shares], samples=len(hours), pooled=pooled)


FLAT = Profile(factors=[1.0] * 24, shares=[1.0] * 24, samples=0, pooled=True)


def profile(agent_id: int | None) -> Profile:
    """The agent's hourly profile (None: all agents), learned anew at most once an hour."""
    hit = _cache.get(agent_id)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    top = max_factor()
    hours = _hours(agent_id)
    if len(hours) >= MIN_SAMPLES:
        p = _profile(hours, top, pooled=agent_id is None)
    elif agent_id is not None:
        p = profile(None)
    else:
        p = FLAT
    _cache[agent_id] = (time.monotonic(), p)
    return p


def factor(agent: SearchAgent, now: datetime | None = None) -> float:
    if not enabled():
        return 1.0
    return profile(agent.id).factors[(now or utcnow()).astimezone(VIENNA).hour]


def should_skip(agent: SearchAgent, now: datetime | None = None, rng: random.Random | None = None) -> bool:
    """True if this scheduled check of the agent is left out for the hour's slow pace."""
    now = now or utcnow()
    f = factor(agent, now)
    if f <= 1.0:
        return False
    last = _aware(agent.last_run_at)
    if last is None:
        return False
    if (now - last).total_seconds() >= agent.interval_s * f * FORCE_AFTER:
        return False
    return (rng or random).random() < 1 - 1 / f


def reset() -> None:
    """Tests, and after hits changed a lot: learn again on the next check."""
    _cache.clear()
