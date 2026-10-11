"""The HTTP client every platform adapter builds on: one anonymous visitor per site, paced, cached, recorded.

`HttpPlatformClient._get()` does what every site needs the same way: the platform's rate limiter, a short response
cache (404s only briefly), the offline replay (WH_REPLAY=1), the access log and the request recorder (with the
platform), a fresh cookie jar every few hours and after a block, and the 403/429 handling (new visitor, back-off,
`Blocked`). A site adds its headers, its request paths and, if its answers need it, its own `_check_status`.

The wire is behind `Transport`: `HttpxTransport` (willhaben) or `CurlTransport` (sites whose bot protection checks
the TLS fingerprint; curl_cffi impersonates a browser). Tests hand in a fake transport or an httpx MockTransport.
Adapters never import the logged-in willhaben session (tests/test_anonymous_public.py).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.i18n import t
from app.platforms.base import Blocked, PlatformError
from app.platforms.ratelimit import RateLimiter
from app.willhaben import access_log, recorder, replay

log = logging.getLogger(__name__)

NOT_FOUND_CACHE_S = 30
ANON_SESSION_S = 6 * 3600   # fresh anonymous visitor every 6 h
TIMEOUT_S = 30


@dataclass
class FetchResponse:
    """What a transport returns; the same attributes as an httpx.Response the adapters read."""
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str
    extensions: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text)


class TransportError(Exception):
    """The request could not be sent or answered (network, timeout, TLS)."""


class Transport(Protocol):
    async def send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> FetchResponse: ...

    def clear_cookies(self) -> None: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    """httpx with its own cookie jar; `transport` lets tests serve canned answers (httpx.MockTransport)."""

    def __init__(self, headers: dict[str, str], transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client = httpx.AsyncClient(headers=headers, timeout=TIMEOUT_S, follow_redirects=True, transport=transport,
                                        cookies=httpx.Cookies())

    async def send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> FetchResponse:
        try:
            r = await self.client.request(method, url, params=params or None, headers=headers)
        except httpx.HTTPError as e:
            raise TransportError(str(e)) from e
        return FetchResponse(r.status_code, dict(r.headers), r.content, str(r.url))

    def clear_cookies(self) -> None:
        self.client.cookies.clear()

    async def aclose(self) -> None:
        await self.client.aclose()


class CurlTransport:
    """curl_cffi impersonating a current Chrome (TLS fingerprint and HTTP/2 settings), for sites behind Akamai and
    the like. Imported lazily: willhaben-only installs never load it."""

    def __init__(self, headers: dict[str, str], impersonate: str = "chrome") -> None:
        from curl_cffi.requests import AsyncSession

        self.session = AsyncSession(impersonate=impersonate, headers=headers, timeout=TIMEOUT_S, allow_redirects=True)

    async def send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> FetchResponse:
        from curl_cffi import CurlError

        try:
            r = await self.session.request(method, url, params=params or None, headers=headers)
        except CurlError as e:
            raise TransportError(str(e)) from e
        return FetchResponse(r.status_code, {k: v for k, v in r.headers.items()}, r.content, str(r.url))

    def clear_cookies(self) -> None:
        self.session.cookies.clear()

    async def aclose(self) -> None:
        await self.session.close()


class HttpPlatformClient:
    def __init__(self, platform_id: str, limiter: RateLimiter, *, name: str | None = None,
                 headers: dict[str, str] | None = None, transport: Transport | None = None,
                 httpx_transport: httpx.AsyncBaseTransport | None = None, backend: str = "httpx",
                 channel: str = "anonymous", session_s: float = ANON_SESSION_S) -> None:
        self.platform = platform_id
        self.name = name or platform_id
        self.channel = channel
        self.limiter = limiter
        self.session_s = session_s
        self._cache: dict[Any, tuple[float, FetchResponse]] = {}
        headers = headers or {}
        if transport is not None:
            self._transport: Transport = transport
        elif backend == "curl":
            self._transport = CurlTransport(headers)
        else:
            self._transport = HttpxTransport(headers, httpx_transport)
        self._jar_since = time.monotonic()

    @property
    def _client(self) -> httpx.AsyncClient | None:
        """The httpx client of the default transport (tests look at its cookie jar)."""
        return getattr(self._transport, "client", None)

    def classify(self, url: str) -> str:
        """Access-log kind of a request; willhaben's path rules by default, a site overrides."""
        return access_log.classify(url)

    def new_anonymous_session(self) -> None:
        """Forget the site's cookies; the next request starts as a new visitor."""
        self._transport.clear_cookies()
        self._jar_since = time.monotonic()

    async def aclose(self) -> None:
        await self._transport.aclose()

    def _check_status(self, r: FetchResponse) -> None:
        """403/429 = the site throttles or blocks this IP: new visitor, back-off, Blocked. A site may treat some
        codes differently (e.g. 401 = rotated app credentials)."""
        if r.status_code in (403, 429):
            self.new_anonymous_session()        # don't keep presenting the visitor that just got blocked
            delay = self.limiter.penalize()
            blocked = Blocked(t("{name} responded {status}; pausing {s}s", name=self.name, status=r.status_code,
                                s=f"{delay:.0f}"))
            blocked.sent = True
            raise blocked

    async def _get(self, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
                   cache_s: float = 0, kind: str | None = None) -> FetchResponse:
        """GET through the platform's limiter; `cache_s` > 0 reuses a recent 200/404 answer for the same request.

        Every call is counted in the access log and, when switched on, recorded; `kind` defaults to classify(url).
        """
        kind = kind or self.classify(url)
        full = str(httpx.URL(url).copy_merge_params(params)) if params else url   # the request as one URL
        key = (full, (headers or {}).get("accept"))
        hit = self._cache.get(key)
        # a 404 can turn into a live ad within minutes (freshly published or reactivated): keep it only briefly
        if cache_s and hit and time.monotonic() - hit[0] < (min(cache_s, NOT_FOUND_CACHE_S) if hit[1].status_code == 404 else cache_s):
            access_log.record(self.channel, kind, "cached", platform=self.platform)
            recorder.record_http(self.channel, kind, "GET", full, status=hit[1].status_code, resp_headers=hit[1].headers,
                                 resp_body=hit[1].content, duration_ms=0, outcome="cached", cached=True,
                                 final_url=hit[1].url, store_response=False, platform=self.platform)   # body stored once already
            return hit[1]
        if replay.enabled() and (rec := replay.find("GET", full)):     # WH_REPLAY=1: no request, no waiting
            replay.note(rec, kind)
            return replay.as_fetch_response(rec, full)
        if time.monotonic() - self._jar_since > self.session_s:
            self.new_anonymous_session()
        async with self.limiter:
            with access_log.track(self.channel, kind, platform=self.platform) as tr:
                started = time.monotonic()
                try:
                    r = await self._transport.send("GET", full, headers=headers)
                except TransportError as e:
                    recorder.record_http(self.channel, kind, "GET", full, req_headers=headers, outcome="error", error=str(e),
                                         duration_ms=int((time.monotonic() - started) * 1000), platform=self.platform)
                    raise PlatformError(t("Network error: {error}", error=e)) from e
                tr.status = r.status_code
        recorder.record_http(self.channel, kind, "GET", full, req_headers=headers, status=r.status_code,
                             resp_headers=r.headers, resp_body=r.content, final_url=r.url,
                             duration_ms=int((time.monotonic() - started) * 1000), platform=self.platform)
        self._check_status(r)
        self.limiter.reward()
        if cache_s and r.status_code in (200, 404):
            now = time.monotonic()
            if len(self._cache) > 200:
                self._cache = {k: v for k, v in self._cache.items() if now - v[0] < 600}
            self._cache[key] = (now, r)
        return r
