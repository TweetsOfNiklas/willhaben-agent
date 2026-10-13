"""The adapter contract every second-hand platform implements (willhaben, kleinanzeigen, …).

A platform turns a pasted search link or a keyword into a `SearchSpec`, runs it (`search`), optionally loads one
ad (`detail`) and the site's own filters (`filters`, willhaben's builder), and paces itself with its own rate
limiter. Search agents, the product search, the ad viewer and the price check only talk to this interface
(app/platforms/registry.py hands out the instances). Adapters never import the logged-in willhaben session.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app.platforms.types import DEFAULT_PLATFORM, AdDetail, SearchFilters, SearchPage, SearchSpec, ad_ref

if TYPE_CHECKING:
    from app.platforms.ratelimit import RateLimiter

CAPABILITIES = ("search_url", "keyword_search", "filters", "detail", "images")


class PlatformError(RuntimeError):
    """A platform answered with an error, could not be reached, or refused the request."""


class Blocked(PlatformError):
    """403/429: the site is throttling or blocking this IP. `sent` is True when the site answered (the request went
    out), False when the limiter refused it before sending."""

    sent = False


class InvalidSearchUrl(ValueError):
    """Not a result-list link of a supported site."""


class InvalidRef(ValueError):
    """An ad ref ("<platform>:<id>") naming an unknown platform."""


class UnknownPlatform(PlatformError, KeyError):
    pass


def split_ref(ref: str) -> tuple[str, str]:
    """"kleinanzeigen:123" -> ("kleinanzeigen", "123"); a bare id is a willhaben id (old links and pushes)."""
    ref = str(ref or "").strip()
    if ":" not in ref:
        return DEFAULT_PLATFORM, ref
    platform, _, ad_id = ref.partition(":")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", platform) or not ad_id:
        raise InvalidRef(ref)
    return platform, ad_id


class Platform(ABC):
    id: str = ""
    name: str = ""                       # shown in the UI ("willhaben", "Kleinanzeigen")
    host: str = ""                       # the site's main host, for texts ("kleinanzeigen.de")
    api_host: str = ""                   # the host its requests go to (a VPN exit is probed against it); "" = host
    home_country: str = "AT"             # ISO code: IBAN rule, prompt wording
    currency: str = "€"
    hosts: tuple[str, ...] = ()          # host suffixes a pasted link may have
    image_hosts: tuple[str, ...] = ()    # CDN host suffixes of ad photos (counted as this platform's accesses)
    locale: str = "de-AT"
    capabilities: frozenset[str] = frozenset()
    access: str = "http"                 # http | browser (how the site is reached; its settings page shows it)
    min_interval_s: int = 0              # a floor for agent intervals (0 = the global minimum)

    def can(self, cap: str) -> bool:
        return cap in self.capabilities

    def matches(self, url: str) -> bool:
        try:
            host = (urlsplit(url.strip()).hostname or "").lower()
        except ValueError:
            return False
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def matches_host(self, host: str | None) -> bool:
        host = (host or "").lower()
        return any(host == h or host.endswith("." + h) for h in (*self.hosts, *self.image_hosts))

    def ref(self, ad_id: str) -> str:
        return ad_ref(self.id, ad_id)

    @property
    @abstractmethod
    def limiter(self) -> RateLimiter: ...

    @abstractmethod
    def parse_search_url(self, url: str) -> SearchSpec:
        """A pasted result-list link -> SearchSpec (canonical url, volatile params dropped). Raises InvalidSearchUrl."""

    @abstractmethod
    def describe(self, spec: SearchSpec) -> str:
        """A short human name for the search (the agent's default name)."""

    @abstractmethod
    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec: ...

    @abstractmethod
    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0) -> SearchPage:
        """Run the search. Raises PlatformError (Blocked when the site throttles)."""

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        """One ad in full; None when it is gone. Only when can("detail")."""
        raise PlatformError(f"{self.name}: no ad details")

    @abstractmethod
    def ad_url(self, ad_id: str) -> str: ...

    def parse_ad_url(self, url: str) -> str | None:
        """The ad id in a link to one of this site's ads, else None."""
        return None

    def extract_ad_id(self, text: str) -> str | None:
        """A bare id or an ad link of this site -> id."""
        text = (text or "").strip()
        if text.isdigit():
            return text
        return self.parse_ad_url(text) if self.matches(text) else None

    async def filters(self, url: str) -> SearchFilters | None:
        """The site's own filters for the agent builder; None unless can("filters")."""
        return None

    def classify(self, url: str) -> str:
        """Coarse kind of a request to this site for the access log (search | detail | filters | other)."""
        return "other"

    def about(self) -> str:
        """A sentence or two for the site's settings page: how it is reached and what its listings lack."""
        return ""

    def status(self) -> dict[str, Any]:
        lim = self.limiter
        return {"id": self.id, "name": self.name, "access": self.access, "last_hour": lim.last_hour,
                "per_hour": lim.per_hour, "min_gap_s": lim.min_gap_s, "cooling_down_s": lim.cooling_down_s}

    async def aclose(self) -> None:
        return None
