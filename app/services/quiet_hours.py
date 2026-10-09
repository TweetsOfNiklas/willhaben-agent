"""Quiet hours: a nightly window in which search agents pause or poll much less, like a person asleep.

Start and end move by up to ±JITTER_MIN minutes each night (fixed per date), so the pattern is not
a sharp, machine-like edge at the same minute every day. Manual checks from the dashboard are not affected.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import SearchAgent, get_setting, set_setting, utcnow

VIENNA = ZoneInfo("Europe/Vienna")
JITTER_MIN = 20
KEYS = ("quiet_enabled", "quiet_start", "quiet_end", "quiet_mode", "quiet_interval_min")


@dataclass
class Config:
    enabled: bool
    start: time
    end: time
    mode: str           # "slow" (poll every interval_min) or "pause"
    interval_min: int


@dataclass
class State:
    active: bool
    start: datetime     # current window if active, otherwise the next one (local time)
    end: datetime


def _parse(hhmm: str, fallback: time) -> time:
    try:
        h, m = hhmm.split(":")
        return time(int(h) % 24, int(m) % 60)
    except ValueError:
        return fallback


def config() -> Config:
    return Config(
        enabled=get_setting("quiet_enabled") == "1",
        start=_parse(get_setting("quiet_start"), time(1, 0)),
        end=_parse(get_setting("quiet_end"), time(7, 0)),
        mode="pause" if get_setting("quiet_mode") == "pause" else "slow",
        interval_min=max(15, int(get_setting("quiet_interval_min") or 60)),
    )


def save(enabled: bool, start: str, end: str, mode: str, interval_min: int) -> None:
    set_setting("quiet_enabled", "1" if enabled else "0")
    set_setting("quiet_start", _parse(start, time(1, 0)).strftime("%H:%M"))
    set_setting("quiet_end", _parse(end, time(7, 0)).strftime("%H:%M"))
    set_setting("quiet_mode", "pause" if mode == "pause" else "slow")
    set_setting("quiet_interval_min", str(max(15, interval_min)))


def _window(cfg: Config, day: date) -> tuple[datetime, datetime]:
    """The window that starts on `day`, with this night's jitter applied."""
    rng = random.Random(f"quiet-{day.isoformat()}")
    start = datetime.combine(day, cfg.start, VIENNA) + timedelta(minutes=rng.randint(-JITTER_MIN, JITTER_MIN))
    end_day = day if cfg.end > cfg.start else day + timedelta(days=1)
    end = datetime.combine(end_day, cfg.end, VIENNA) + timedelta(minutes=rng.randint(-JITTER_MIN, JITTER_MIN))
    return start, end


def state(cfg: Config | None = None, now: datetime | None = None) -> State | None:
    cfg = cfg or config()
    if not cfg.enabled or cfg.start == cfg.end:
        return None
    now = (now or utcnow()).astimezone(VIENNA)
    windows = [_window(cfg, now.date() + timedelta(days=d)) for d in (-1, 0, 1)]
    for s, e in windows:
        if s <= now < e:
            return State(True, s, e)
    s, e = min((w for w in windows if w[0] > now), key=lambda w: w[0])
    return State(False, s, e)


def should_skip(agent: SearchAgent, now: datetime | None = None) -> bool:
    """True if a scheduled run of this agent should not hit willhaben right now."""
    cfg = config()
    st = state(cfg, now)
    if not st or not st.active:
        return False
    if cfg.mode == "pause":
        return True
    last = agent.last_run_at
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    # small slack so the regular trigger's jitter does not push a run into the next slot
    return ((now or utcnow()) - last).total_seconds() < cfg.interval_min * 60 - 90
