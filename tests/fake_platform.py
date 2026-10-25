"""A fake second-hand platform for tests: answers searches from a queue, no HTTP (app/platforms contract)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.platforms import AdDetail, InvalidSearchUrl, Listing, Platform, PlatformError, SearchPage, SearchSpec
from app.platforms.ratelimit import RateLimiter


def listing(ad_id: str, heading: str = "Rad", price: float | None = 100.0, *, platform: str = "fake",
            published: datetime | None = None, is_bumped: bool | None = None, **kw: Any) -> Listing:
    fields = {"body": None, "price_display": f"{price:g} €" if price is not None else None, "old_price": None,
              "location": "10115 Berlin", "postcode": "10115", "state": None,
              "published_at": published or datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc), "changed_at": None,
              "end_date": None, "url": f"https://fake.example/ad/{ad_id}", "image_url": None, "is_private": True}
    fields.update(kw)
    return Listing(ad_id=ad_id, heading=heading, price=price, is_bumped=is_bumped, platform=platform, **fields)


class FakePlatform(Platform):
    """`pages` is a queue of SearchPage | Exception | list[Listing]; every search() pops one (the last one stays)."""
    id = "fake"
    name = "Fake"
    host = "fake.example"
    home_country = "DE"
    hosts = ("fake.example",)
    image_hosts = ("img.fake.example",)
    capabilities = frozenset({"search_url", "keyword_search", "detail", "images"})

    def __init__(self, pages: list | None = None, details: dict[str, AdDetail | None] | None = None) -> None:
        self.pages = list(pages or [])
        self.details = details or {}
        self.searched: list[SearchSpec] = []
        self.detail_calls: list[str] = []
        self._limiter = RateLimiter(0, name="Fake")

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def parse_search_url(self, url: str) -> SearchSpec:
        if not self.matches(url) or "/s/" not in url:
            raise InvalidSearchUrl("not a fake search")
        kw = url.split("/s/", 1)[1].split("?")[0].split("/")[0]
        return SearchSpec(platform=self.id, url=f"https://fake.example/s/{kw}", keyword=kw or None)

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or "fake search"

    def keyword_search(self, keyword, price_from=None, price_to=None, *, category=None, sort="newest") -> SearchSpec:
        return SearchSpec(platform=self.id, url=f"https://fake.example/s/{keyword}", keyword=keyword,
                          price_from=price_from, price_to=price_to, sort=sort)

    async def search(self, spec: SearchSpec, *, rows=None, cache_s=0, page: int = 1) -> SearchPage:
        self.searched.append(spec)
        if not self.pages:
            return SearchPage(rows_found=0, ads=[])
        item = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, list):
            item = SearchPage(rows_found=len(item), ads=item)
        return item

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        self.detail_calls.append(ad_id)
        if ad_id not in self.details:
            raise PlatformError("fake: no such ad")
        return self.details[ad_id]

    def ad_url(self, ad_id: str) -> str:
        return f"https://fake.example/ad/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        return url.rsplit("/ad/", 1)[1].split("?")[0] if "/ad/" in url else None

    def classify(self, url: str) -> str:
        return "detail" if "/ad/" in url else "search"


def detail_of(ad: Listing, description: str = "Beschreibung") -> AdDetail:
    return AdDetail(ad_id=ad.ad_id, heading=ad.heading, description=description, price=ad.price,
                    price_display=ad.price_display, status="active", published_at=ad.published_at,
                    first_published_at=ad.published_at, end_date=None, url=ad.url,
                    images=[ad.image_url] if ad.image_url else [], location=ad.location, is_private=ad.is_private,
                    platform=ad.platform)
