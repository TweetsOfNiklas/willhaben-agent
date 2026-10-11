"""preisjaeger.at (Austria), the deal community, as a platform adapter: deals from shops, not private listings.

The search page (www.preisjaeger.at/search?q=…&sortBy=new&hide_expired=true) renders each deal as a Vue component
whose props are JSON in a data-vue3 attribute ("ThreadMainListItemNormalizer" → thread): title, price, the
comparison price ("statt"), shop, temperature, voucher code, posting time and the main image. No login; the site
sits behind Cloudflare, so requests go through curl_cffi impersonating Chrome.

A search without results shows "Die heißesten Deals von heute" instead: those deals never count as hits. The deal
pages carry no machine-readable description, so the viewer shows the listing (no `detail` capability). The shop
fills the location field (it is what a deal has instead of a place), the comparison price the struck-out price.
The website ignores price limits in the link; the adapter keeps them (priceFrom/priceTo) and filters itself.
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from app.i18n import t
from app.platforms.base import InvalidSearchUrl, Platform, PlatformError
from app.platforms.httpclient import HttpPlatformClient, Transport
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import euro, from_epoch, one_line, to_float
from app.platforms.types import Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

WEB = "https://www.preisjaeger.at"
STATIC = "https://static.preisjaeger.at"
PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "de-AT"}
_VUE = re.compile(r"""data-vue3=(?:'([^']*)'|"([^"]*)")""")
_FALLBACK = re.compile(r"heißesten Deals|hottest deals", re.I)


# ------------------------------------------------------------------ parsing
def _threads(page: str) -> list[dict[str, Any]]:
    out = []
    for m in _VUE.finditer(page or ""):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        if "ThreadMainListItemNormalizer" not in raw:
            continue
        try:
            obj = json.loads(html.unescape(raw))
        except ValueError:
            continue
        thread = (obj.get("props") or {}).get("thread")
        if isinstance(thread, dict) and thread.get("threadId"):
            out.append(thread)
    return out


def _is_fallback(page: str) -> bool:
    """No results: the page lists today's hottest deals under its own heading before the first deal."""
    first = page.find("ThreadMainListItemNormalizer")
    if first < 0:
        return False
    start = page.rfind("<h1", 0, first)
    return bool(_FALLBACK.search(page[start if start >= 0 else max(0, first - 6000):first]))


def image_url(img: dict[str, Any] | None, size: str = "300x300") -> str | None:
    if not isinstance(img, dict) or not img.get("path") or not img.get("name"):
        return None
    uid = img.get("uid") or f"{img['name']}.{img.get('ext') or 'jpg'}"
    return f"{STATIC}/{img['path']}/{img['name']}/re/{size}/qt/60/{uid}"


def parse_listing(th: dict[str, Any]) -> Listing:
    price = to_float(th.get("price")) or None          # 0: a voucher or percentage deal without a price
    before = to_float(th.get("nextBestPrice"))
    pct = to_float(th.get("percentage"))
    merchant = (th.get("merchant") or {}).get("merchantName") or th.get("linkHost")
    shipping = to_float((th.get("shipping") or {}).get("price"))
    temp = to_float(th.get("temperature"))
    facts = [f"{temp:.0f}°" if temp is not None else None,
             t("Voucher code {code}", code=th["voucherCode"]) if th.get("voucherCode") else None,
             t("Shipping {price}", price=euro(shipping)) if shipping else None,
             one_line((th.get("mainGroup") or {}).get("threadGroupName")),
             t("local deal") if th.get("isLocal") else None]
    tid = str(th.get("threadId"))
    slug = th.get("titleSlug")
    return Listing(ad_id=tid, heading=one_line(th.get("title")) or "", body=" · ".join(f for f in facts if f) or None,
                   price=price, price_display=euro(price) if price else (f"−{pct:g} %" if pct else None),
                   old_price=before if before and price is not None and before > price else None,
                   location=one_line(merchant), postcode=None, state=None, published_at=from_epoch(th.get("publishedAt")),
                   changed_at=None, end_date=from_epoch((th.get("endDate") or {}).get("timestamp")),
                   url=f"{WEB}/deals/{slug}-{tid}" if slug else f"{WEB}/share-deal/{tid}",
                   image_url=image_url(th.get("mainImage")), is_private=None, is_bumped=None, status="active",
                   platform="preisjaeger")


def parse_search(page: str, price_from: float | None = None, price_to: float | None = None) -> SearchPage:
    """The search page -> SearchPage: live deals only, within the price limits (deals without a price drop out of a
    limited search). The fallback list of a search without results gives an empty page."""
    if "ThreadMainListItemNormalizer" not in (page or "") and "Ergebnisse" not in (page or ""):
        raise PlatformError(t("Preisjäger answered without a result list"))
    if _is_fallback(page):
        return SearchPage(rows_found=0, ads=[])
    ads = []
    for th in _threads(page):
        if th.get("isExpired") or th.get("type") == "Discussion" or th.get("deletedAt"):
            continue
        ad = parse_listing(th)
        if price_from is not None and (ad.price is None or ad.price < price_from):
            continue
        if price_to is not None and (ad.price is None or ad.price > price_to):
            continue
        ads.append(ad)
    return SearchPage(rows_found=len(ads), ads=ads)


# ------------------------------------------------------------------ the client
def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.startswith("/search"):
        return "search"
    if path.startswith(("/deals/", "/share-deal/")):
        return "detail"
    return "other"


_limiter: RateLimiter | None = None


def preisjaeger_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("preisjaeger"), platform_per_hour("preisjaeger"), name="Preisjäger")
    return _limiter


class PreisjaegerClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__("preisjaeger", preisjaeger_limiter(), name="Preisjäger", headers={"Accept-Language": "de-AT"},
                         transport=transport, backend="curl")

    def classify(self, url: str) -> str:
        return classify(url)

    async def search_page(self, params: dict[str, str], price_from: float | None, price_to: float | None, *,
                          cache_s: float = 0) -> SearchPage:
        r = await self._get(f"{WEB}/search", params=params, headers=PAGE_HEADERS, cache_s=cache_s, kind="search")
        if r.status_code != 200:
            raise PlatformError(t("Preisjäger search returned HTTP {status}", status=r.status_code))
        return parse_search(r.text, price_from, price_to)


_client: PreisjaegerClient | None = None


def get_client() -> PreisjaegerClient:
    global _client
    if _client is None:
        _client = PreisjaegerClient()
    return _client


def set_client(client: PreisjaegerClient | None) -> None:
    """Tests: a client with a fake transport."""
    global _client
    _client = client


# ------------------------------------------------------------------ the adapter
def _num(v: str | None) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise InvalidSearchUrl(t("Price in the link is not a number")) from None


class PreisjaegerPlatform(Platform):
    id = "preisjaeger"
    name = "Preisjäger"
    host = "preisjaeger.at"
    home_country = "AT"
    hosts = ("preisjaeger.at",)
    image_hosts = ("static.preisjaeger.at",)
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "images"})
    access = "http"
    min_interval_s = 300                      # deals come in slowly; the community needs minutes to post them

    @property
    def limiter(self) -> RateLimiter:
        return preisjaeger_limiter()

    @property
    def client(self) -> PreisjaegerClient:
        return get_client()

    def about(self) -> str:
        return t("Deals from shops that the Preisjäger community posts: new goods with price, comparison price, shop "
                 "and temperature. No private sellers and no details page; the shop is shown where other sites show "
                 "the place.")

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not a preisjaeger.at link"))
        if u.path.startswith(("/deals/", "/share-deal/")):
            raise InvalidSearchUrl(t("That is a deal link, not a search"))
        if u.path.rstrip("/") != "/search":
            raise InvalidSearchUrl(t("Not a Preisjäger search (copy the link of a search: …/search?q=…)"))
        qs = parse_qs(u.query)
        keyword = one_line((qs.get("q") or [""])[0])
        if not keyword:
            raise InvalidSearchUrl(t("The Preisjäger link has no search term (q=…)"))
        return self.keyword_search(keyword, _num((qs.get("priceFrom") or [None])[0]), _num((qs.get("priceTo") or [None])[0]))

    def _params(self, spec: SearchSpec, sort: str = "newest") -> dict[str, str]:
        params = {"q": spec.keyword or ""}
        if sort != "relevance":
            params["sortBy"] = "new"
        params["hide_expired"] = "true"
        return params

    def _url(self, spec: SearchSpec) -> str:
        params = list(self._params(spec).items())
        if spec.price_from is not None:
            params.append(("priceFrom", f"{spec.price_from:g}"))
        if spec.price_to is not None:
            params.append(("priceTo", f"{spec.price_to:g}"))
        return f"{WEB}/search?{urlencode(params)}"

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          sort=sort)
        spec.url = self._url(spec)
        return spec

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0) -> SearchPage:
        from app.config import get_settings

        page = await self.client.search_page(self._params(spec, spec.sort), spec.price_from, spec.price_to,
                                             cache_s=cache_s)
        page.ads = page.ads[:rows or get_settings().search_rows]
        page.title = spec.keyword
        return page

    def ad_url(self, ad_id: str) -> str:
        return f"{WEB}/share-deal/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        m = re.search(r"/deals/[^/?#]*?-(\d{3,})/?(?:[?#]|$)", url) or re.search(r"/share-deal/(\d{3,})", url)
        return m.group(1) if m else None

    def classify(self, url: str) -> str:
        return classify(url)

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


__all__ = ["PreisjaegerClient", "PreisjaegerPlatform", "get_client", "set_client", "parse_search", "parse_listing"]
