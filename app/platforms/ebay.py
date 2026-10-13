"""ebay.at (Austria) as a platform adapter, through the website's result list: Buy It Now offers located in Austria.

A search is the result page www.ebay.at/sch/i.html?_nkw=…&_sop=10 (newest first) with LH_PrefLoc=1 (items located in
Austria) and LH_BIN=1 (Buy It Now). Auctions are left out, also those with a Buy It Now price once they carry bids:
their price is only the current bid. Each result is an `li.s-card` (data-listingid) with title, condition, private or
commercial seller, price, "oder Preisvorschlag", shipping, the listing time (Vienna time, without a year) and the
seller's name and rating. The page has no machine-readable data, so the adapter reads the card texts. Cards name no
place (items from abroad say "aus <country>"), and there is no ad-detail call: the viewer shows the card.

eBay sits behind Akamai Bot Manager. Requests go through curl_cffi impersonating Chrome, and a new visitor's first
requests are often refused (403) while the bot manager sets its cookies. Until a search of this visitor went through, a
403 is that warm-up: the client keeps its cookies and asks again (paced by the limiter, at most WARMUP_RETRIES times);
after that a 403 is a block as on every other site. No login, no API keys.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from app.i18n import t
from app.platforms.base import InvalidSearchUrl, Platform, PlatformError
from app.platforms.httpclient import FetchResponse, HttpPlatformClient, Transport
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import euro, one_line
from app.platforms.types import Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

WEB = "https://www.ebay.at"
SEARCH = f"{WEB}/sch/i.html"
PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "de-AT,de;q=0.9", "Referer": f"{WEB}/"}
PAGE_SIZE = "60"
WARMUP_RETRIES = 2
VIENNA = ZoneInfo("Europe/Vienna")
DEFAULT_LOCATION = "1"                  # LH_PrefLoc: 1 = Austria, 3 = EU, 2 = worldwide
SORTS = {"newest": "10", "relevance": "12", "price_asc": "15", "price_desc": "16"}
_SORT_OF = {v: k for k, v in SORTS.items()}
_MONTHS = {m: i for i, names in enumerate((("jan",), ("feb",), ("mar", "mär"), ("apr",), ("may", "mai"), ("jun",),
                                           ("jul",), ("aug",), ("sep",), ("oct", "okt"), ("nov",), ("dec", "dez")), 1)
           for m in names}
_LISTING_ID = re.compile(r'data-listingid="?(\d+)')
_CARD = re.compile(r'<li\b[^>]*class="?s-card[ "]')
_LI = re.compile(r"<(/?)li\b", re.I)
_REWRITE = "srp-river-answer--REWRITE_START"          # "results for fewer words": not what was searched
_COUNT = re.compile(r"srp-controls__count-heading[^>]*>(?:\s|<[^>]+>)*([\d.]+)")
_EUR = re.compile(r"(?:EUR|€)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)")
_BIDS = re.compile(r"^\d+ Gebote?$")
_WHEN = re.compile(r"^([A-Za-zä]{3})-(\d{1,2}) (\d{1,2}):(\d{2})$")
_RATING = re.compile(r"^[\d.,]+\s?% positiv")
_SHIPPING = re.compile(r"Versand|Abholung|Lieferung", re.I)
_SELLER_TYPES = {"Privat": True, "Gewerblich": False}
_VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "col", "area"}


# ------------------------------------------------------------------ parsing
class _CardParser(HTMLParser):
    """One result card: its texts with the classes of the elements around them, its links and its photo."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.texts: list[tuple[str, str]] = []
        self.links: list[str] = []
        self.image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"] or "")
        if tag == "img" and self.image is None and "s-card__image" in (a.get("class") or ""):
            self.image = a.get("data-defer-load") or a.get("src")
        if tag not in _VOID:
            self.stack.append((tag, a.get("class") or ""))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        text = one_line(data)
        if text and not any(tag in ("script", "style") for tag, _ in self.stack):
            self.texts.append((" ".join(c for _, c in self.stack if c), text))


def _cards(page: str) -> list[str]:
    """The result cards in page order, up to eBay's "results for fewer words"."""
    cut = page.find(_REWRITE)
    page = page[:cut] if cut >= 0 else page
    out = []
    for m in _CARD.finditer(page):
        depth = 0
        for li in _LI.finditer(page, m.start()):
            depth += -1 if li.group(1) else 1
            if depth == 0:
                out.append(page[m.start():page.find(">", li.end()) + 1])
                break
    return out


def _amounts(text: str) -> list[float]:
    """"EUR 1.234,50" -> [1234.5]; a range ("EUR 10,00 bis EUR 20,00") gives both ends."""
    return [float(a.replace(".", "").replace(",", ".")) for a in _EUR.findall(text or "")]


def listed_at(text: str, now: datetime | None = None) -> datetime | None:
    """"Oct-9 23:33" (Vienna time, no year) -> aware datetime; a date after today belongs to last year."""
    m = _WHEN.match(text or "")
    month = _MONTHS.get(m.group(1).lower()) if m else None
    if not m or not month:
        return None
    now = (now or datetime.now(VIENNA)).astimezone(VIENNA)
    try:
        dt = datetime(now.year, month, int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=VIENNA)
    except ValueError:
        return None
    if dt > now + timedelta(days=1):
        dt = dt.replace(year=now.year - 1)
    return dt


def parse_card(card: str, now: datetime | None = None) -> Listing | None:
    """One result card -> Listing; None for eBay's own placeholder cards and for auctions."""
    m = _LISTING_ID.search(card)
    if not m:
        return None
    ad_id = m.group(1)
    p = _CardParser()
    p.feed(card)
    if not any(f"/itm/{ad_id}" in href for href in p.links):
        return None                            # "Shop on eBay" and other cards that are no listing
    title = " ".join(x for c, x in p.texts if "s-card__title" in c and "new-listing" not in c and "clipped" not in c)
    subtitle = [x.strip(" |") for c, x in p.texts if "s-card__subtitle" in c]
    rows = [(c, x) for c, x in p.texts if "s-card__attribute-row" in c]
    other = [x for c, x in rows if "s-card__price" not in c]
    if not title or any(_BIDS.match(x) for x in other):
        return None
    current = [v for c, x in rows if "s-card__price" in c and "strikethrough" not in c for v in _amounts(x)]
    before = next((v for c, x in rows if "s-card__price" in c and "strikethrough" in c for v in _amounts(x)), None)
    price = min(current) if current else None
    display = None
    if current:
        display = euro(price) if len(set(current)) == 1 else f"{euro(min(current))} – {euro(max(current))}"
    is_private = next((_SELLER_TYPES[x] for x in subtitle if x in _SELLER_TYPES), None)
    condition = [x for x in subtitle if x and x not in _SELLER_TYPES]
    abroad = next((x[4:] for x in other if x.startswith("aus ")), None)
    when = next((d for x in other if (d := listed_at(x, now))), None)
    rating_at = next((i for i, x in enumerate(other) if _RATING.match(x)), None)
    seller = other[rating_at - 1] if rating_at else None
    facts = [*condition,
             next((x for x in other if x in ("oder Preisvorschlag", "Preisvorschlag")), None),
             next((x for x in other if _SHIPPING.search(x) and "Rückversand" not in x), None),
             t("Seller {name}, {rating}", name=seller, rating=other[rating_at]) if seller and rating_at else None]
    image = p.image if p.image and p.image.startswith("https://") else None
    return Listing(ad_id=ad_id, heading=title, body=" · ".join(f for f in facts if f) or None, price=price,
                   price_display=display, old_price=before if before and price is not None and before > price else None,
                   location=abroad, postcode=None, state=None, published_at=when, changed_at=None, end_date=None,
                   url=f"{WEB}/itm/{ad_id}", image_url=re.sub(r"/s-l\d+\.", "/s-l500.", image) if image else None,
                   is_private=is_private, is_bumped=None, status="active", platform="ebay")


def parse_search(page: str, now: datetime | None = None) -> SearchPage:
    """The result page -> SearchPage. "0 Ergebnisse" gives an empty page (the cards below are only suggestions)."""
    m = _COUNT.search(page or "")
    if not m and "srp-results" not in (page or ""):
        raise PlatformError(t("eBay answered without a result list"))
    total = int(m.group(1).replace(".", "")) if m else None
    if total == 0:
        return SearchPage(rows_found=0, ads=[])
    ads, seen = [], set()
    for card in _cards(page):
        ad = parse_card(card, now)
        if ad and ad.ad_id not in seen:
            seen.add(ad.ad_id)
            ads.append(ad)
    return SearchPage(rows_found=total if total is not None else len(ads), ads=ads)


# ------------------------------------------------------------------ the client
def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.startswith("/sch/"):
        return "search"
    if path.startswith("/itm/"):
        return "detail"
    return "other"


_limiter: RateLimiter | None = None


def ebay_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("ebay"), platform_per_hour("ebay"), name="eBay")
    return _limiter


class _WarmingUp(PlatformError):
    """403 before this visitor's first answer: eBay's bot manager is still setting its cookies."""


class EbayClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__("ebay", ebay_limiter(), name="eBay", headers={"Accept-Language": "de-AT,de;q=0.9"},
                         transport=transport, backend="curl")
        self._warm, self._warmups = False, WARMUP_RETRIES

    def classify(self, url: str) -> str:
        return classify(url)

    def new_anonymous_session(self) -> None:
        super().new_anonymous_session()
        self._warm, self._warmups = False, WARMUP_RETRIES

    def _check_status(self, r: FetchResponse) -> None:
        if r.status_code == 403 and not self._warm and self._warmups > 0:
            self._warmups -= 1
            raise _WarmingUp("eBay: warming up")
        super()._check_status(r)

    async def search_page(self, params: dict[str, str], *, cache_s: float = 0) -> SearchPage:
        while True:
            try:
                r = await self._get(SEARCH, params=params, headers=PAGE_HEADERS, cache_s=cache_s, kind="search")
                break
            except _WarmingUp:
                log.info("eBay refused a new visitor while its bot check sets the cookies; asking again")
        if r.status_code != 200:
            raise PlatformError(t("eBay search returned HTTP {status}", status=r.status_code))
        page = parse_search(r.text)
        self._warm = True
        return page


_client: EbayClient | None = None


def get_client() -> EbayClient:
    global _client
    if _client is None:
        _client = EbayClient()
    return _client


def set_client(client: EbayClient | None) -> None:
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


class EbayPlatform(Platform):
    id = "ebay"
    name = "eBay"
    host = "ebay.at"
    home_country = "AT"
    hosts = ("ebay.at",)
    image_hosts = ("ebayimg.com", "ebaystatic.com")
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "images"})
    access = "http"
    min_interval_s = 300                      # every search is a whole result page (about 1.5 MB)

    @property
    def limiter(self) -> RateLimiter:
        return ebay_limiter()

    @property
    def client(self) -> EbayClient:
        return get_client()

    def about(self) -> str:
        return t("Reads the result pages of ebay.at: only Buy It Now offers located in Austria, no auctions. Listings "
                 "show condition, private or commercial seller, shipping and the listing time but no place; there is "
                 "no details page.")

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not an ebay.at link"))
        if u.path.startswith(("/itm/", "/p/")):
            raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
        m = re.fullmatch(r"/sch/(?:[^/]+/(\d+)/)?i\.html", u.path)
        if not m:
            raise InvalidSearchUrl(t("Not an eBay result list (copy the link of a search: …/sch/i.html?_nkw=…)"))
        qs = {k: v[0] for k, v in parse_qs(u.query).items() if v}
        keyword = one_line(qs.get("_nkw")) or ""
        category = m.group(1) or (qs.get("_sacat") if qs.get("_sacat", "0") != "0" else None)
        if not keyword and not category:
            raise InvalidSearchUrl(t("The eBay link has no search term (_nkw=…)"))
        extra = {k: qs[k] for k in ("LH_ItemCondition",) if qs.get(k)}
        if qs.get("LH_PrefLoc", DEFAULT_LOCATION) != DEFAULT_LOCATION:
            extra["LH_PrefLoc"] = qs["LH_PrefLoc"]
        spec = SearchSpec(platform=self.id, url="", keyword=keyword or None, price_from=_num(qs.get("_udlo")),
                          price_to=_num(qs.get("_udhi")), category=category, sort=_SORT_OF.get(qs.get("_sop", ""), "newest"),
                          extra=extra)
        spec.url = self._url(spec)
        return spec

    def _params(self, spec: SearchSpec) -> dict[str, str]:
        params = {"_nkw": spec.keyword or "", "_sacat": spec.category or "0", "_sop": SORTS.get(spec.sort, SORTS["newest"]),
                  "LH_BIN": "1", "LH_PrefLoc": spec.extra.get("LH_PrefLoc", DEFAULT_LOCATION)}
        if spec.price_from is not None:
            params["_udlo"] = f"{spec.price_from:g}"
        if spec.price_to is not None:
            params["_udhi"] = f"{spec.price_to:g}"
        if spec.extra.get("LH_ItemCondition"):
            params["LH_ItemCondition"] = spec.extra["LH_ItemCondition"]
        return params

    def _url(self, spec: SearchSpec) -> str:
        return f"{SEARCH}?{urlencode(self._params(spec))}"

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          category=category, sort=sort)
        spec.url = self._url(spec)
        return spec

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0) -> SearchPage:
        from app.config import get_settings

        page = await self.client.search_page({**self._params(spec), "_ipg": PAGE_SIZE}, cache_s=cache_s)
        page.ads = page.ads[:rows or get_settings().search_rows]
        page.title = spec.keyword
        return page

    def ad_url(self, ad_id: str) -> str:
        return f"{WEB}/itm/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,15})(?:[/?#]|$)", url)
        return m.group(1) if m else None

    def classify(self, url: str) -> str:
        return classify(url)

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


__all__ = ["EbayClient", "EbayPlatform", "get_client", "set_client", "parse_search", "parse_card", "listed_at"]
