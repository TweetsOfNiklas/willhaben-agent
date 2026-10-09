"""Geizhals.at lookup for ad drafts: find the product, read its spec sheet and lowest new price.

Plain HTML pages (no API): search results are `galleryview__item` cards, an article page
(`...-a<id>.html`) lists its properties as `<dl class="specs-grid">` with dt/dd pairs and the
lowest offer as `lowPrice: <n>`. Requests are few (one search + one product page per draft) and
spaced like the willhaben ones.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings
from app.i18n import t
from app.willhaben.public import RateLimiter

log = logging.getLogger(__name__)

BASE = "https://geizhals.at"
_limiter = RateLimiter(2.0)
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport

_CARD_RE = re.compile(r'<article class="galleryview__item[^"]*"(.*?)</article>', re.S)
_NAME_RE = re.compile(r'class="galleryview__name-link"[^>]*>(.*?)</a>', re.S)
_HREF_RE = re.compile(r'href="(https://geizhals\.at/[^"#]+-a\d+\.html)"')
_PRICE_RE = re.compile(r'<span class="price">\s*€\s*([\d.]+,\d{2})')
_IMG_RE = re.compile(r'<img src="(https://gzhls\.at/[^"]+)"')
_SPECS_RE = re.compile(r'<dl\s+class="specs-grid[^"]*"[^>]*>(.*?)</dl>', re.S)
_ITEM_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_LOW_RE = re.compile(r"lowPrice:\s*([\d.]+)")


class GeizhalsError(RuntimeError):
    pass


@dataclass
class Hit:
    name: str
    url: str
    price: float | None = None
    image: str | None = None


@dataclass
class Product:
    name: str
    url: str
    price_from: float | None
    specs: list[tuple[str, str]] = field(default_factory=list)


def _text(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    t = html.unescape(t).replace("​", "")
    return re.sub(r"\s+", " ", t).strip()


def _eur(v: str) -> float | None:
    try:
        return float(v.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_search(page: str) -> list[Hit]:
    hits = []
    for card in _CARD_RE.findall(page):
        href, name = _HREF_RE.search(card), _NAME_RE.search(card)
        if not href or not name:      # variant families (-v<id>) have no single spec sheet
            continue
        price, img = _PRICE_RE.search(card), _IMG_RE.search(card)
        hits.append(Hit(name=_text(name.group(1)), url=href.group(1),
                        price=_eur(price.group(1)) if price else None, image=img.group(1) if img else None))
    seen, out = set(), []
    for h in hits:
        if h.url not in seen:
            seen.add(h.url)
            out.append(h)
    return out


def parse_product(page: str, url: str) -> Product:
    h1 = _H1_RE.search(page)
    specs: list[tuple[str, str]] = []
    grid = _SPECS_RE.search(page)
    if grid:
        specs = [(_text(k), _text(v)) for k, v in _ITEM_RE.findall(grid.group(1)) if _text(k) and _text(v)]
    low = _LOW_RE.search(page)
    return Product(name=_text(h1.group(1)) if h1 else "", url=url,
                   price_from=float(low.group(1)) if low else None, specs=specs)


async def _get(url: str) -> str:
    headers = {"user-agent": get_settings().user_agent, "accept": "text/html", "accept-language": "de-AT,de;q=0.9"}
    async with _limiter:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, transport=_transport) as c:
                r = await c.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise GeizhalsError(t("Geizhals unreachable: {error}", error=e)) from e
    if r.status_code != 200:
        raise GeizhalsError(t("Geizhals answered HTTP {status}", status=r.status_code))
    return r.text


async def search(query: str, limit: int = 5) -> list[Hit]:
    page = await _get(f"{BASE}/?{urlencode({'fs': query, 'hloc': 'at'})}")
    return parse_search(page)[:limit]


async def product(url: str) -> Product:
    if not re.fullmatch(r"https://geizhals\.at/[^\s\"<>]+-a\d+\.html", url):
        raise GeizhalsError(t("Not a Geizhals product page"))
    return parse_product(await _get(url), url)


async def lookup(query: str) -> dict[str, Any] | None:
    """Best match for a product name, as stored on the draft; None if nothing usable was found."""
    query = query.strip()
    if not query:
        return None
    hits = await search(query)
    if not hits:
        return {"query": query, "match": None, "candidates": []}
    prod = await product(hits[0].url)
    return {"query": query, "match": asdict(prod), "candidates": [asdict(h) for h in hits]}


def spec_block(match: dict[str, Any] | None) -> str:
    """Text appended to the description when the user ticks "add the spec sheet" (ad content: stays German)."""
    if not match or not match.get("specs"):
        return ""
    lines = [f"Technische Daten ({match['name']}, laut Hersteller/Geizhals):"]
    lines += [f"- {k}: {v}" for k, v in match["specs"]]
    return "\n".join(lines)
