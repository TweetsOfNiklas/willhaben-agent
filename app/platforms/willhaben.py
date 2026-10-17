"""willhaben.at as a platform adapter: a thin layer over app/willhaben/parse.py and the anonymous PublicClient.

The client is fetched with get_public() on every call, so tests keep injecting it with set_public().
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from app.config import get_settings
from app.platforms.base import Platform
from app.platforms.types import AdDetail, SearchFilters, SearchPage, SearchSpec
from app.willhaben import access_log
from app.willhaben.parse import (
    BASE,
    describe_search,
    extract_ad_id,
    keyword_search_url,
    to_webapi_url,
    url_param,
    web_url,
)

SORTS = {"newest": 1, "relevance": 7, "price_asc": 3, "price_desc": 4}


MAX_ROWS = 200      # willhaben's largest result page (10/2026: rows=1000 still gives 200); about 1.2 MB of JSON

class WillhabenPlatform(Platform):
    id = "willhaben"
    name = "willhaben"
    host = "willhaben.at"
    api_host = "www.willhaben.at"
    home_country = "AT"
    hosts = ("willhaben.at",)
    image_hosts = ("willhaben.at",)
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "filters", "detail", "images"})

    @property
    def limiter(self):
        """The anonymous client's limiter, which is the shared willhaben limiter (tests may give the client its own)."""
        from app.willhaben.public import get_public

        return get_public().limiter

    def parse_search_url(self, url: str) -> SearchSpec:
        api = to_webapi_url(url, rows=get_settings().search_rows)   # raises InvalidSearchUrl
        browser = web_url(api)
        q = dict(parse_qsl(urlsplit(browser).query))

        def _num(v: str | None) -> float | None:
            try:
                return float(v) if v not in (None, "") else None
            except ValueError:
                return None
        path = urlsplit(browser).path[len("/iad/"):].strip("/") or None
        return SearchSpec(platform=self.id, url=browser, api=api, keyword=q.get("keyword") or None,
                          price_from=_num(q.get("PRICE_FROM")), price_to=_num(q.get("PRICE_TO")),
                          category=path if path and path != "kaufen-und-verkaufen/marktplatz" else None)

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or describe_search(spec.url)

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        from app.services.product_search import DEFAULT_PATH

        api = keyword_search_url(keyword, get_settings().search_rows, price_from, price_to,
                                 sort=SORTS.get(sort, 1), path=category or DEFAULT_PATH)
        return SearchSpec(platform=self.id, url=web_url(api), api=api, keyword=keyword, price_from=price_from,
                          price_to=price_to, category=category, sort=sort)

    def former_rows(self, spec: SearchSpec) -> int | None:
        raw = url_param(spec.api or "", "rows") if spec.api else None
        rows = int(raw) if raw and raw.isdigit() else get_settings().search_rows
        return rows if rows < MAX_ROWS else None

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0) -> SearchPage:
        """One result page. Without `rows` (the search agents) the largest page willhaben gives, MAX_ROWS: one request
        covers everything new since the last check, also after quiet hours or on a busy search."""
        from app.willhaben.public import get_public

        rows = rows or MAX_ROWS
        api = spec.api or to_webapi_url(spec.url, rows=rows)
        if url_param(api, "rows") != str(rows):
            api = re.sub(r"([?&])rows=\d+", rf"\g<1>rows={rows}", api) if url_param(api, "rows") \
                else api + ("&" if "?" in api else "?") + f"rows={rows}"
        return await get_public().search(api, cache_s=cache_s)

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        from app.willhaben.public import get_public

        return await get_public().get_detail(ad_id)

    def ad_url(self, ad_id: str) -> str:
        return f"{BASE}/iad/object?adId={ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        return extract_ad_id(url) if "adId=" in url or "/d/" in url or re.search(r"-\d{6,}/?(?:\?|$)", url) else None

    async def filters(self, url: str) -> SearchFilters | None:
        from app.willhaben.public import get_public

        return await get_public().search_filters(url)

    def classify(self, url: str) -> str:
        return access_log.classify(url)
