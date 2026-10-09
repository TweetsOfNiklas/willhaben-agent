"""Human-like timing for automatic chat replies.

A person doesn't answer within two seconds: they notice the message after a while, read it (the buyer sees
"read"), think, type at a few characters per second, and send. plan() spreads one answer over that pattern;
the window comes from the settings (reply_delay_min_s / reply_delay_max_s) and every value is randomized.
In an ongoing chat (I have answered at least once) a person replies within seconds: the "warm" window
(reply_fast_min_s / reply_fast_max_s) applies, with quicker thinking and typing capped to that window.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import get_settings
from app.db import get_setting, utcnow

TYPING_CPS = (3.5, 7.0)     # characters per second while "typing"
THINK_S = (4.0, 15.0)       # pause between reading and starting to type
LATE_READ_S = (8.0, 40.0)   # message is already older than planned (app was busy/offline): read soon
WARM_THINK_S = (1.0, 4.0)   # ongoing chat: hardly any pause


@dataclass
class Plan:
    read_at: datetime       # when the read receipt goes out
    send_after: datetime    # earliest send time; typing time is added once the text is known


def enabled() -> bool:
    return get_settings().humanize_chat and get_setting("reply_humanize") == "1"


def window(warm: bool = False) -> tuple[float, float]:
    keys, default = (("reply_fast_min_s", "reply_fast_max_s"), (5.0, 20.0)) if warm else \
        (("reply_delay_min_s", "reply_delay_max_s"), (40.0, 180.0))
    try:
        lo = max(0.0, float(get_setting(keys[0]) or default[0]))
        hi = max(lo, float(get_setting(keys[1]) or default[1]))
    except ValueError:
        lo, hi = default
    return lo, hi


def plan(arrived_at: datetime | None, now: datetime | None = None, rng: random.Random | None = None,
         warm: bool = False) -> Plan:
    """Timing for answering a message that arrived at `arrived_at`; skewed towards the quicker end."""
    rng = rng or random
    now = now or utcnow()
    lo, hi = window(warm)
    total = rng.triangular(lo, hi, lo + (hi - lo) * 0.3)
    arrived = arrived_at or now
    read_at = arrived + timedelta(seconds=total * rng.uniform(0.3, 0.6))
    if read_at < now:
        late = LATE_READ_S if hi >= 60 else (0.0, hi * 0.3)
        read_at = now + timedelta(seconds=rng.uniform(*late))
    think = WARM_THINK_S if warm else THINK_S
    send_after = max(arrived + timedelta(seconds=total), read_at + timedelta(seconds=rng.uniform(*think)))
    return Plan(read_at=read_at, send_after=send_after)


def typing_seconds(text: str, rng: random.Random | None = None, warm: bool = False) -> float:
    rng = rng or random
    secs = len(text) / rng.uniform(*TYPING_CPS)
    return min(secs, window(True)[1]) if warm else secs   # ongoing chat: the whole answer stays within seconds
