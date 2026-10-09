"""Parsing of willhaben JSON (search results, ad detail) and URL conversion.

All shapes verified against live responses on 2026-10-09 (see tests/fixtures).
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.i18n import t

BASE = "https://www.willhaben.at"
SEARCH_PREFIX = "/webapi/ad-search/search/atz/seo/"
# Query params the web app adds for its own bookkeeping; dropped so the agent URL is stable.
_VOLATILE_PARAMS = {"page", "sfId", "isNavigation", "publishedDateTo", "rows", "sort", "isLog"}


class InvalidSearchUrl(ValueError):
    pass


def attrs(obj: dict[str, Any]) -> dict[str, list[str]]:
    """Flatten willhaben's `attributes.attribute[{name, values}]` list into a dict."""
    out: dict[str, list[str]] = {}
    for a in (obj.get("attributes") or {}).get("attribute") or []:
        out[a.get("name", "")] = list(a.get("values") or [])
    return out


def _first(a: dict[str, list[str]], key: str) -> str | None:
    v = a.get(key)
    return v[0] if v else None


def _float(v: str | None) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _ms(v: str | None) -> datetime | None:
    """willhaben epoch-millisecond strings -> aware UTC datetime."""
    f = _float(v)
    if f is None:
        return None
    return datetime.fromtimestamp(f / 1000, tz=timezone.utc)


def _iso(v: str | None) -> datetime | None:
    """ISO strings like 2026-11-23T10:26:53+0100 -> aware UTC datetime (naive input is taken as UTC)."""
    if not v:
        return None
    try:
        dt = datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_text(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)                      # dealers format with lists ...
    s = re.sub(r"</(p|div|ul|ol|h[1-6]|tr)>", "\n", s, flags=re.I)       # ... and paragraphs
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[ \t]+\n", "\n", html.unescape(s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def ad_url(seo_url: str | None, ad_id: str) -> str:
    if seo_url:
        return f"{BASE}/iad/{seo_url.lstrip('/')}"
    return f"{BASE}/iad/object?adId={ad_id}"


@dataclass
class AdSummary:
    ad_id: str
    heading: str
    body: str | None
    price: float | None
    price_display: str | None
    old_price: float | None
    location: str | None
    postcode: str | None
    state: str | None
    published_at: datetime | None
    changed_at: datetime | None
    end_date: datetime | None
    url: str
    image_url: str | None
    is_private: bool | None
    is_bumped: bool
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ad_id": self.ad_id,
            "title": self.heading,
            "price": self.price,
            "price_display": self.price_display,
            "old_price": self.old_price,
            "location": self.location,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "url": self.url,
            "image_url": self.image_url,
            "is_private": self.is_private,
        }


@dataclass
class SearchPage:
    rows_found: int
    ads: list[AdSummary]
    title: str | None = None


def parse_summary(ad: dict[str, Any]) -> AdSummary:
    a = attrs(ad)
    ad_id = str(ad.get("id") or _first(a, "ADID") or "")
    images = (ad.get("advertImageList") or {}).get("advertImage") or []
    image = images[0].get("mainImageUrl") if images else None
    if not image and _first(a, "MMO"):
        image = f"https://cache.willhaben.at/mmo/{_first(a, 'MMO')}"
    price_raw = _first(a, "PRICE/AMOUNT") or _first(a, "PRICE")
    private = _first(a, "ISPRIVATE")
    return AdSummary(
        ad_id=ad_id,
        heading=_first(a, "HEADING") or ad.get("description") or "",
        body=clean_text(_first(a, "BODY_DYN")),
        price=_float(price_raw),
        price_display=_first(a, "PRICE_FOR_DISPLAY"),
        old_price=_float(_first(a, "OLD_PRICE")),
        location=_first(a, "LOCATION"),
        postcode=_first(a, "POSTCODE"),
        state=_first(a, "STATE"),
        published_at=_ms(_first(a, "PUBLISHED")),
        changed_at=_ms(_first(a, "CHANGED")),
        end_date=_ms(_first(a, "ENDDATE")),
        url=ad_url(_first(a, "SEO_URL"), ad_id),
        image_url=image,
        is_private=None if private is None else private == "1",
        is_bumped=_first(a, "IS_BUMPED") == "1",
        status=((ad.get("advertStatus") or {}).get("id")) or "unknown",
    )


def parse_search(data: dict[str, Any]) -> SearchPage:
    items = ((data.get("advertSummaryList") or {}).get("advertSummary")) or []
    return SearchPage(
        rows_found=int(data.get("rowsFound") or 0),
        ads=[parse_summary(x) for x in items],
        title=data.get("searchTitle") or data.get("heading"),
    )


@dataclass
class AdDetail:
    ad_id: str
    heading: str
    description: str | None
    price: float | None
    price_display: str | None
    status: str
    published_at: datetime | None
    first_published_at: datetime | None
    end_date: datetime | None
    url: str
    images: list[str] = field(default_factory=list)
    location: str | None = None
    seller_name: str | None = None
    seller_login_id: str | None = None
    seller_uuid: str | None = None
    is_private: bool | None = None
    chat_enabled: bool | None = None
    category_path: list[str] = field(default_factory=list)
    edit_actions: Any = None

    def as_context(self) -> dict[str, Any]:
        return {
            "ad_id": self.ad_id,
            "title": self.heading,
            "price": self.price_display or (f"€ {self.price:g}" if self.price is not None else None),
            "description": self.description,
            "location": self.location,
            "category": " > ".join(self.category_path),
            "status": self.status,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "seller_name": self.seller_name,
            "seller_login_id": self.seller_login_id,
            "seller_uuid": self.seller_uuid,
            "url": self.url,
            "images": self.images[:5],
        }


def parse_detail(page_props: dict[str, Any]) -> AdDetail | None:
    ad = page_props.get("advertDetails")
    if not ad:
        return None
    a = attrs(ad)
    images = [
        i.get("mainImageUrl")
        for i in ((ad.get("advertImageList") or {}).get("advertImage") or [])
        if i.get("mainImageUrl")
    ]
    addr = ad.get("advertAddressDetails") or {}
    seller = ad.get("sellerProfileUserData") or {}
    crumbs = [c.get("displayName") for c in (ad.get("breadcrumbs") or [])][2:]
    price_raw = _first(a, "PRICE/AMOUNT") or _first(a, "PRICE")
    canonical = (ad.get("seoMetaData") or {}).get("canonicalUrl")
    private = _first(a, "ISPRIVATE")
    location = ", ".join(x for x in [addr.get("postCode"), addr.get("postalName"), addr.get("province")] if x)
    return AdDetail(
        ad_id=str(ad.get("id")),
        heading=ad.get("description") or "",
        description=clean_text(_first(a, "DESCRIPTION")),
        price=_float(price_raw),
        price_display=_first(a, "PRICE_FOR_DISPLAY"),
        status=((ad.get("advertStatus") or {}).get("id")) or "unknown",
        published_at=_iso(ad.get("publishedDate")),
        first_published_at=_iso(ad.get("firstPublishedDate")),
        end_date=_iso(ad.get("endDate")),
        url=canonical or ad_url(None, str(ad.get("id"))),
        images=images,
        location=location or None,
        seller_name=seller.get("name"),
        seller_login_id=str(ad.get("loginId")) if ad.get("loginId") is not None else None,
        seller_uuid=ad.get("loginUUid"),
        is_private=None if private is None else private == "1",
        chat_enabled=ad.get("chatEnabled"),
        category_path=[c for c in crumbs if c],
        edit_actions=ad.get("advertEditStatusActionsList"),
    )


_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def extract_next_data(html_text: str) -> dict[str, Any] | None:
    m = _NEXT_DATA_RE.search(html_text)
    return json.loads(m.group(1)) if m else None


def to_webapi_url(user_url: str, rows: int = 30) -> str:
    """Turn a willhaben result-list URL from the browser into the JSON search URL.

    https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300
    -> https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300&sort=1&rows=30
    """
    u = urlsplit(user_url.strip())
    if u.scheme not in ("http", "https") or not u.netloc.endswith("willhaben.at"):
        raise InvalidSearchUrl(t("Not a willhaben.at URL"))
    path = u.path
    if path.startswith(SEARCH_PREFIX):
        rest = path[len(SEARCH_PREFIX):]
    elif path.startswith("/iad/"):
        rest = path[len("/iad/"):]
    else:
        raise InvalidSearchUrl(t("Path must start with /iad/ (copy a result list from willhaben)"))
    rest = rest.strip("/")
    if "/d/" in f"/{rest}/" or rest.startswith("object"):
        raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
    if not rest:
        raise InvalidSearchUrl(t("Empty search path"))
    params = [(k, v) for k, v in parse_qsl(u.query) if k not in _VOLATILE_PARAMS]
    params += [("sort", "1"), ("rows", str(rows))]
    return f"{BASE}{SEARCH_PREFIX}{rest}?{urlencode(params)}"


def keyword_search_url(keyword: str, rows: int = 30, price_from: float | None = None,
                       price_to: float | None = None, sort: int = 1,
                       path: str = "kaufen-und-verkaufen/marktplatz") -> str:
    params: list[tuple[str, str]] = [("keyword", keyword)]
    if price_from is not None:
        params.append(("PRICE_FROM", f"{price_from:g}"))
    if price_to is not None:
        params.append(("PRICE_TO", f"{price_to:g}"))
    params += [("sort", str(sort)), ("rows", str(rows))]
    return f"{BASE}{SEARCH_PREFIX}{path.strip('/')}?{urlencode(params)}"


def web_url(path_or_url: str) -> str:
    """Browser result-list URL (https://www.willhaben.at/iad/...) for any search link willhaben hands out.

    Accepts restapi links (`/search/atz/seo/...`, `ad-search.willhaben.at/restapi/v2/...`), webapi links,
    SEO links without `/iad` (`/kaufen-und-verkaufen/...`) and browser links; drops bookkeeping params.
    """
    u = urlsplit(path_or_url.strip())
    path = u.path
    for prefix in ("/restapi/v2/search/atz/seo/", "/search/atz/seo/", SEARCH_PREFIX, "/iad/", "/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    params = [(k, v) for k, v in parse_qsl(u.query) if k not in _VOLATILE_PARAMS]
    return f"{BASE}/iad/{path.strip('/')}" + (f"?{urlencode(params)}" if params else "")


def with_params(url: str, changes: dict[str, str | None]) -> str:
    """Set (or, for empty values, remove) single-valued query params of a browser search URL."""
    u = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(u.query) if k not in changes and k not in _VOLATILE_PARAMS]
    params += [(k, v) for k, v in changes.items() if v]
    return f"{BASE}{u.path}" + (f"?{urlencode(params)}" if params else "")


def url_param(url: str, name: str) -> str | None:
    return dict(parse_qsl(urlsplit(url).query)).get(name)


@dataclass
class FilterValue:
    label: str
    url: str                 # browser URL with this value applied (selected values: with it removed)
    hits: int | None = None


@dataclass
class SearchFilter:
    id: str
    label: str
    kind: str                # HIERARCHICAL | RANGE | STANDARD
    multi: bool
    values: list[FilterValue]
    selected: list[FilterValue]
    range_params: list[str] = field(default_factory=list)   # e.g. [PRICE_FROM, PRICE_TO]


@dataclass
class SearchFilters:
    """One step of the search-agent builder: the current search plus everything that can be added to it."""
    url: str
    rows_found: int
    groups: list[tuple[str, list[SearchFilter]]]
    ads: list[AdSummary]
    keyword: str | None = None

    @property
    def selected(self) -> list[tuple[SearchFilter, FilterValue]]:
        return [(f, v) for _, fs in self.groups for f in fs for v in f.selected]

    def suggested_name(self) -> str:
        parts = [self.keyword] if self.keyword else []
        for f, v in self.selected:
            # Category shows its whole path; the deepest level is enough for a name.
            if f.kind == "HIERARCHICAL" and v is not f.selected[-1]:
                continue
            parts.append(v.label)
        return " · ".join(parts)[:80] or t("All ads")


def _link(d: Any) -> str | None:
    if not isinstance(d, dict):
        return None
    return d.get("relativePath") or d.get("uri")


def parse_filters(data: dict[str, Any], fallback_url: str) -> SearchFilters:
    links = {c.get("id"): c for c in ((data.get("searchContextLinks") or {}).get("contextLink") or [])}
    seo = _link(links.get("resultListSeoNavigatorLink"))
    groups: list[tuple[str, list[SearchFilter]]] = []
    keyword = None
    for g in data.get("navigatorGroups") or []:
        filters = []
        for n in g.get("navigatorList") or []:
            kind = n.get("navigatorType") or "STANDARD"
            selected = []
            for v in n.get("selectedValues") or []:
                reset = _link(v.get("resetLink"))
                if reset:
                    selected.append(FilterValue(label=v.get("label") or "", url=web_url(reset)))
            if kind == "TEXT_SEARCH":   # the keyword has its own input field
                keyword = (n.get("selectedValues") or [{}])[0].get("label") if n.get("selectedValues") else None
                continue
            values = []
            for grp in n.get("groupedPossibleValues") or []:
                for v in grp.get("possibleValues") or []:
                    link = _link(v.get("webLink")) or _link(v.get("searchLink"))
                    if link:
                        values.append(FilterValue(label=v.get("label") or "", url=web_url(link), hits=v.get("hits")))
            info = n.get("urlConstructionInformation") or {}
            range_params = [p["urlParameterName"] for p in info.get("urlParams") or [] if p.get("urlParameterName")]
            if kind == "RANGE" and not range_params:
                continue
            if kind != "RANGE" and not values and not selected:
                continue
            filters.append(SearchFilter(
                id=str(n.get("id")), label=n.get("label") or str(n.get("id")), kind=kind,
                multi=n.get("navigatorSelectionType") == "MULTI_SELECT", values=values, selected=selected,
                range_params=range_params if kind == "RANGE" else []))
        if filters:
            groups.append((g.get("label") or "", filters))
    page = parse_search(data)
    return SearchFilters(url=web_url(seo) if seo else web_url(fallback_url), rows_found=page.rows_found,
                         groups=groups, ads=page.ads, keyword=keyword)


def describe_search(user_url: str) -> str:
    """Best-effort human name for a search URL (keyword or last path segment)."""
    u = urlsplit(user_url)
    q = dict(parse_qsl(u.query))
    if q.get("keyword"):
        return q["keyword"]
    seg = [s for s in u.path.split("/") if s][-1:] or [t("search")]
    return re.sub(r"-\d+$", "", seg[0]).replace("-", " ").title()


def extract_ad_id(text: str) -> str | None:
    """Accept a bare id, an /iad/object?adId= URL or an SEO URL ending in -<id>/."""
    text = text.strip()
    if text.isdigit():
        return text
    m = re.search(r"adId=(\d+)", text) or re.search(r"-(\d{6,})/?(?:\?|$)", text)
    return m.group(1) if m else None
