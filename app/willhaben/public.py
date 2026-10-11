"""Anonymous willhaben access: search JSON and ad detail pages.

Every request goes through one shared RateLimiter so all jobs together stay polite.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import get_settings
from app.i18n import t
from app.platforms.base import Blocked as PlatformBlocked
from app.platforms.base import PlatformError
from app.platforms.ratelimit import RateLimiter  # noqa: F401 - re-exported, tests and geizhals import it from here
from app.willhaben import access_log, recorder, replay
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


# The errors are the neutral platform errors (app/platforms/base.py): every `except WillhabenError` also catches
# what the shared limiter and the other adapters raise.
WillhabenError = PlatformError
Blocked = PlatformBlocked


_limiter: RateLimiter | None = None


def willhaben_limiter() -> RateLimiter:
    """The one limiter for every willhaben request, anonymous or via the logged-in browser (same home IP)."""
    global _limiter
    if _limiter is None:
        s = get_settings()
        from app.db import requests_per_hour   # the budget may be changed in Settings

        _limiter = RateLimiter(s.request_min_gap_s, requests_per_hour(), name="willhaben")
    return _limiter


NOT_FOUND_CACHE_S = 30
ANON_SESSION_S = 6 * 3600   # fresh anonymous visitor every 6 h


class PublicClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        s = get_settings()
        self.limiter = willhaben_limiter()
        self._cache: dict[str, tuple[float, httpx.Response]] = {}
        # Anonymous visitor, deliberately separate from the logged-in browser session (own cookie jar, never the
        # account's cookies): search agents, screening, price checks and the ad viewer must not be linkable to the
        # owner's willhaben account. The jar is reset regularly and after a block -> a fresh anonymous visitor.
        self._client = httpx.AsyncClient(
            headers={"user-agent": s.user_agent, "accept-language": "de-AT,de;q=0.9,en;q=0.5"},
            timeout=30,
            follow_redirects=True,
            transport=transport,
            cookies=httpx.Cookies(),
        )
        self._jar_since = time.monotonic()

    def new_anonymous_session(self) -> None:
        """Forget willhaben's anonymous cookies; the next request starts as a new visitor."""
        self._client.cookies.clear()
        self._jar_since = time.monotonic()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, *, json_api: bool, cache_s: float = 0, kind: str | None = None) -> httpx.Response:
        """GET through the shared limiter; `cache_s` > 0 reuses a recent 200/404 answer for the same URL.

        Every call is counted in the access log (channel anonymous; `kind` defaults to one derived from the path).
        """
        kind = kind or access_log.classify(url)
        key = f"{json_api}:{url}"
        hit = self._cache.get(key)
        # a 404 can turn into a live ad within minutes (freshly published or reactivated): keep it only briefly
        if cache_s and hit and time.monotonic() - hit[0] < (min(cache_s, NOT_FOUND_CACHE_S) if hit[1].status_code == 404 else cache_s):
            access_log.record("anonymous", kind, "cached")
            recorder.record_http("anonymous", kind, "GET", url, status=hit[1].status_code, resp_headers=dict(hit[1].headers),
                                 resp_body=hit[1].content, duration_ms=0, outcome="cached", cached=True,
                                 final_url=str(hit[1].url), store_response=False)   # the body is stored once already
            return hit[1]
        if replay.enabled() and (rec := replay.find("GET", url)):     # WH_REPLAY=1: no request, no waiting
            replay.note(rec, kind)
            return replay.as_httpx_response(rec, url)
        headers = {"accept": "application/json", "x-wh-client": WEB_CLIENT} if json_api else {"accept": "text/html"}
        if time.monotonic() - self._jar_since > ANON_SESSION_S:
            self.new_anonymous_session()
        async with self.limiter:
            with access_log.track("anonymous", kind) as tr:
                started = time.monotonic()
                try:
                    r = await self._client.get(url, headers=headers)
                except httpx.HTTPError as e:
                    recorder.record_http("anonymous", kind, "GET", url, req_headers=headers, outcome="error", error=str(e),
                                         duration_ms=int((time.monotonic() - started) * 1000))
                    raise WillhabenError(t("Network error: {error}", error=e)) from e
                tr.status = r.status_code
        recorder.record_http("anonymous", kind, "GET", url, req_headers=headers, status=r.status_code,
                             resp_headers=dict(r.headers), resp_body=r.content, final_url=str(r.url),
                             duration_ms=int((time.monotonic() - started) * 1000))
        if r.status_code in (403, 429):
            self.new_anonymous_session()        # don't keep presenting the visitor that just got blocked
            delay = self.limiter.penalize()
            blocked = Blocked(t("willhaben responded {status}; pausing {s}s", status=r.status_code, s=f"{delay:.0f}"))
            blocked.sent = True
            raise blocked
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
        r = await self._get(to_webapi_url(browser_url, rows=rows), json_api=True, cache_s=120, kind="filters")
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
