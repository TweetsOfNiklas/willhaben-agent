"""Anonymous willhaben access: search JSON and ad detail pages.

Every request goes through one shared RateLimiter so all jobs together stay polite.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.i18n import t
from app.platforms import httpclient
from app.platforms.base import Blocked as PlatformBlocked
from app.platforms.base import PlatformError
from app.platforms.ratelimit import RateLimiter  # noqa: F401 - re-exported, tests and geizhals import it from here
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
        from app.db import platform_min_gap_s, platform_per_hour   # changed in Settings (own or all platforms)

        _limiter = RateLimiter(platform_min_gap_s("willhaben"), platform_per_hour("willhaben"), name="willhaben")
    return _limiter


NOT_FOUND_CACHE_S = httpclient.NOT_FOUND_CACHE_S
ANON_SESSION_S = httpclient.ANON_SESSION_S


class PublicClient(httpclient.HttpPlatformClient):
    """The anonymous willhaben visitor: search JSON, filters and ad detail pages (app/platforms/httpclient.py does
    the pacing, caching, recording and block handling).

    Deliberately separate from the logged-in browser session (own cookie jar, never the account's cookies): search
    agents, screening, price checks and the ad viewer must not be linkable to the owner's willhaben account. The jar
    is reset regularly and after a block -> a fresh anonymous visitor.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        s = get_settings()
        super().__init__("willhaben", willhaben_limiter(), name="willhaben",
                         headers={"user-agent": s.user_agent, "accept-language": "de-AT,de;q=0.9,en;q=0.5"},
                         httpx_transport=transport)

    async def _get(self, url: str, *, json_api: bool, cache_s: float = 0, kind: str | None = None,  # type: ignore[override]
                   refresh: bool = False) -> httpclient.FetchResponse:
        """GET through the shared limiter; `cache_s` > 0 reuses a recent 200/404 answer for the same URL (not with
        `refresh`)."""
        headers = {"accept": "application/json", "x-wh-client": WEB_CLIENT} if json_api else {"accept": "text/html"}
        return await super()._get(url, headers=headers, cache_s=cache_s, kind=kind, refresh=refresh)

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

    async def get_detail(self, ad_id: str, refresh: bool = False) -> AdDetail | None:
        """Full ad detail; None if the ad is gone (expired, deleted, sold). refresh: not the copy kept for 5 minutes."""
        r = await self._get(f"https://www.willhaben.at/iad/object?adId={ad_id}", json_api=False, cache_s=300,
                            refresh=refresh)
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
