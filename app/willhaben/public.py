"""Anonymous willhaben access: search JSON and ad detail pages.

Every request goes through one shared RateLimiter so all jobs together stay polite.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any

import httpx

from app.config import get_settings
from app.i18n import t
from app.willhaben.parse import (
    AdDetail,
    SearchFilters,
    SearchPage,
    extract_next_data,
    keyword_search_url,
    parse_detail,
    parse_filters,
    parse_search,
    to_webapi_url,
)

log = logging.getLogger(__name__)

WEB_CLIENT = "api@willhaben.at;responsive_web;server;1.0.0;desktop"


class WillhabenError(RuntimeError):
    pass


class Blocked(WillhabenError):
    """403/429: willhaben is throttling or blocking this IP."""


class RateLimiter:
    """Serialises requests with a minimum gap (plus jitter), an hourly budget and a global cool-down.

    Waits that would block a caller for longer than `max_wait_s` (cool-down after 403/429 or an
    exhausted budget) raise Blocked instead, so dashboard requests fail fast and scheduled jobs
    simply retry on their next run.
    """

    def __init__(self, min_gap_s: float, per_hour: int = 0, max_wait_s: float = 45.0) -> None:
        self.min_gap_s = min_gap_s
        self.per_hour = per_hour
        self.max_wait_s = max_wait_s
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
                raise Blocked(t("willhaben pause after a block, {s} s left", s=f"{self._cooldown_until - now:.0f}"))
            budget = self._budget_wait(now)
            if budget > self.max_wait_s:
                raise Blocked(t("Request limit ({n}/h) reached, free again in {m} min", n=self.per_hour, m=f"{budget / 60:.0f}"))
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

    @property
    def cooling_down_s(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def last_hour(self) -> int:
        self._budget_wait(time.monotonic())
        return len(self._recent)


_limiter: RateLimiter | None = None


def willhaben_limiter() -> RateLimiter:
    """The one limiter for every willhaben request, anonymous or via the logged-in browser (same home IP)."""
    global _limiter
    if _limiter is None:
        s = get_settings()
        _limiter = RateLimiter(s.request_min_gap_s, s.requests_per_hour)
    return _limiter


NOT_FOUND_CACHE_S = 30


class PublicClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        s = get_settings()
        self.limiter = willhaben_limiter()
        self._cache: dict[str, tuple[float, httpx.Response]] = {}
        self._client = httpx.AsyncClient(
            headers={"user-agent": s.user_agent, "accept-language": "de-AT,de;q=0.9,en;q=0.5"},
            timeout=30,
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, *, json_api: bool, cache_s: float = 0) -> httpx.Response:
        """GET through the shared limiter; `cache_s` > 0 reuses a recent 200/404 answer for the same URL."""
        key = f"{json_api}:{url}"
        hit = self._cache.get(key)
        # a 404 can turn into a live ad within minutes (freshly published or reactivated): keep it only briefly
        if cache_s and hit and time.monotonic() - hit[0] < (min(cache_s, NOT_FOUND_CACHE_S) if hit[1].status_code == 404 else cache_s):
            return hit[1]
        headers = {"accept": "application/json", "x-wh-client": WEB_CLIENT} if json_api else {"accept": "text/html"}
        async with self.limiter:
            try:
                r = await self._client.get(url, headers=headers)
            except httpx.HTTPError as e:
                raise WillhabenError(t("Network error: {error}", error=e)) from e
        if r.status_code in (403, 429):
            delay = self.limiter.penalize()
            raise Blocked(t("willhaben responded {status}; pausing {s}s", status=r.status_code, s=f"{delay:.0f}"))
        self.limiter.reward()
        if cache_s and r.status_code in (200, 404):
            now = time.monotonic()
            if len(self._cache) > 200:
                self._cache = {k: v for k, v in self._cache.items() if now - v[0] < 600}
            self._cache[key] = (now, r)
        return r

    async def search(self, webapi_url: str, cache_s: float = 0) -> SearchPage:
        """Search results; agents poll uncached, one-off lookups may reuse a recent answer."""
        r = await self._get(webapi_url, json_api=True, cache_s=cache_s)
        if r.status_code != 200:
            raise WillhabenError(t("Search returned HTTP {status}", status=r.status_code))
        try:
            return parse_search(r.json())
        except ValueError as e:
            raise WillhabenError(t("Search returned no JSON")) from e

    async def search_filters(self, browser_url: str, rows: int = 6) -> SearchFilters:
        """Current search plus its available filters (willhaben "navigators") for the agent builder."""
        r = await self._get(to_webapi_url(browser_url, rows=rows), json_api=True, cache_s=120)
        if r.status_code != 200:
            raise WillhabenError(t("Search returned HTTP {status}", status=r.status_code))
        try:
            return parse_filters(r.json(), browser_url)
        except ValueError as e:
            raise WillhabenError(t("Search returned no JSON")) from e

    async def search_keyword(self, keyword: str, rows: int = 30, price_from: float | None = None,
                             price_to: float | None = None, sort: int = 1) -> SearchPage:
        return await self.search(keyword_search_url(keyword, rows, price_from, price_to, sort), cache_s=300)

    async def get_detail(self, ad_id: str) -> AdDetail | None:
        """Full ad detail; None if the ad is gone (expired, deleted, sold)."""
        r = await self._get(f"https://www.willhaben.at/iad/object?adId={ad_id}", json_api=False, cache_s=300)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise WillhabenError(t("Ad {id}: HTTP {status}", id=ad_id, status=r.status_code))
        nd = extract_next_data(r.text)
        if not nd:
            raise WillhabenError(t("Ad {id}: no __NEXT_DATA__ found", id=ad_id))
        if nd.get("page") == "/404":
            return None
        return parse_detail((nd.get("props") or {}).get("pageProps") or {})


_client: PublicClient | None = None


def get_public() -> PublicClient:
    global _client
    if _client is None:
        _client = PublicClient()
    return _client


def set_public(client: PublicClient | None) -> None:
    global _client
    _client = client
