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
_UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(gb|tb|mb|mah)\b", re.I)
CAPACITY_RE = re.compile(r"\d+(?:[.,]\d+)?(?:gb|tb|mb|mah)", re.I)   # a variant-defining size: "4GB", "1.5TB"
_QTOK_RE = re.compile(r"[^\s,;:()\[\]{}\"'!?/|]+")


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


def glue_units(s: str) -> str:
    """Number and capacity unit as one word, case kept: "4 GB" -> "4GB", "1,5 TB" -> "1.5TB"."""
    return _UNIT_RE.sub(lambda m: m.group(1).replace(",", ".") + m.group(2), s or "")


def _norm(s: str) -> str:
    return glue_units(s).lower()


def _has_word(name: str, w: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(w)}(?!\w)", name) is not None


def matches(name: str, query: str) -> bool:
    """Is this product really the one asked for (the variant too)? Every word with a digit – also a bare "4" as in
    "Pi 4", and capacities like "4GB" – appears in the name as a whole word ("13" is not "113" or "230cm", "4" not
    "400" or "4GB"), and most other words appear. "4 GB" equals "4GB"; a capacity only in the name is fine."""
    n = _norm(name)
    toks = [w for w in (x.strip(".-") for x in _QTOK_RE.findall(_norm(query))) if w]
    model = [w for w in toks if re.search(r"\d", w)]
    words = [w for w in toks if not re.search(r"\d", w) and len(w) >= 2]
    if not toks or not all(_has_word(n, w) for w in model):
        return False
    return not words or sum(w in n for w in words) / len(words) >= 0.6


def best_hit(hits: list[Hit], query: str) -> Hit | None:
    """The first hit (Geizhals' order) that is really this product and variant."""
    return next((h for h in hits if matches(h.name, query)), None)


async def lookup(query: str) -> dict[str, Any] | None:
    """Best match for a product name, as stored on the draft; None if nothing usable was found. `verified`: the
    product (search card and page title) matches the query, variant included; False = Geizhals' first hit as a guess."""
    query = query.strip()
    if not query:
        return None
    hits = await search(query)
    if not hits:
        return {"query": query, "match": None, "candidates": [], "verified": False}
    best = best_hit(hits, query)
    prod = await product((best or hits[0]).url)
    verified = best is not None and (not prod.name or matches(prod.name, query))   # the h1 can be more precise
    return {"query": query, "match": asdict(prod), "candidates": [asdict(h) for h in hits], "verified": verified}


def spec_block(match: dict[str, Any] | None) -> str:
    """Text appended to the description when the user ticks "add the spec sheet" (ad content: stays German)."""
    if not match or not match.get("specs"):
        return ""
    lines = [f"Technische Daten ({match['name']}, laut Hersteller/Geizhals):"]
    lines += [f"- {k}: {v}" for k, v in match["specs"]]
    return "\n".join(lines)
