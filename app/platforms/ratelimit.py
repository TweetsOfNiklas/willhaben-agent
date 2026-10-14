"""One rate limiter per platform: a minimum gap with jitter, an hourly budget and a cool-down after blocks."""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Any

from app.i18n import t
from app.platforms.base import Blocked


class RateLimiter:
    """Serialises requests with a minimum gap (plus jitter), an hourly budget and a global cool-down.

    Waits that would block a caller for longer than `max_wait_s` (cool-down after 403/429 or an
    exhausted budget) raise Blocked instead, so dashboard requests fail fast and scheduled jobs
    simply retry on their next run. `name` is the platform shown in those messages.
    """

    def __init__(self, min_gap_s: float, per_hour: int = 0, max_wait_s: float = 45.0, *, name: str = "willhaben") -> None:
        self.min_gap_s = min_gap_s
        self.per_hour = per_hour
        self.max_wait_s = max_wait_s
        self.name = name
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cooldown_until = 0.0
        self._strikes = 0
        self._recent: deque[float] = deque()   # monotonic start times of the last hour

    def _budget_wait(self, now: float) -> float:
        while self._recent and now - self._recent[0] >= 3600:
            self._recent.popleft()
        if not self.per_hour or len(self._recent) < self.per_hour:
            return 0.0
        return self._recent[0] + 3600 - now

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        try:
            now = time.monotonic()
            if self._cooldown_until - now > self.max_wait_s:
                raise Blocked(t("{name} pause after a block, {s} s left", name=self.name,
                                s=f"{self._cooldown_until - now:.0f}"))
            budget = self._budget_wait(now)
            if budget > self.max_wait_s:
                raise Blocked(t("{name}: request limit ({n}/h) reached, free again in {m} min", name=self.name,
                                n=self.per_hour, m=f"{budget / 60:.0f}"))
            wait = max(self._cooldown_until - now, self._last + self.min_gap_s - now, budget, 0.0)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, self.min_gap_s / 2))
            self._recent.append(time.monotonic())
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, *exc: Any) -> None:
        self._last = time.monotonic()
        self._lock.release()

    def penalize(self) -> float:
        """Exponential back-off after 403/429; returns the cool-down in seconds."""
        self._strikes += 1
        delay = min(60 * 2 ** (self._strikes - 1), 3600)
        self._cooldown_until = time.monotonic() + delay
        return delay

    def reward(self) -> None:
        self._strikes = 0

    def refund(self) -> None:
        """The request just let through was not sent after all (the VPN test's exit moved): it costs no budget."""
        if self._recent:
            self._recent.pop()

    def free_in(self, k: int) -> float:
        """Seconds until the hour's `k` oldest requests have left it (0 for k <= 0)."""
        now = time.monotonic()
        self._budget_wait(now)
        if k <= 0 or not self._recent:
            return 0.0
        return max(0.0, self._recent[min(k, len(self._recent)) - 1] + 3600 - now)

    def reset(self) -> None:
        """Forget the pause, the pacing and the hour's requests (tests: each one starts like a fresh site)."""
        self._lock = asyncio.Lock()
        self._last = self._cooldown_until = 0.0
        self._strikes = 0
        self._recent.clear()

    @property
    def cooling_down_s(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def last_hour(self) -> int:
        self._budget_wait(time.monotonic())
        return len(self._recent)
