"""Geizhals.at lookup for ad drafts: find the product, read its spec sheet and lowest new price.

Plain HTML pages (no API): search results are `galleryview__item` cards, an article page
(`...-a<id>.html`) lists its properties as `<dl class="specs-grid">` with dt/dd pairs and the
lowest offer as `lowPrice: <n>`. Requests are few (a search or up to three, then one product page, which a mere
price lookup skips) and 3 s apart (bursts of searches got 403 at 2 s); a 403/429 pauses Geizhals like any other site.
With its switch in Settings → VPN on, the requests go through the VPN's shared connection (app/platforms/vpn.py),
fail closed; there a 403/429, a bot check or no answer refuses the server (a shuffle) instead of pausing Geizhals.

Matching (checked against real searches, 2026-10-13): the first search result is often another variant ("iPhone 16"
→ "16 Pro Max", "UniFi Switch 24" → "Switch Pro 24"), so a result counts only when every number of the query is in
its name, an order code ("USW-24-POE") in its name or link, and it has no variant word (Pro, Max, Lite …), pack or
accessory word the query lacks. Spelling is forgiven ("I-Phone" = "iPhone", "Rasperry" ≈ "Raspberry"). A query
that finds nothing fitting is retried with its order code alone, and with its key words when nothing was found.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings
from app.i18n import t
from app.platforms import vpn
from app.platforms.base import PlatformError
from app.platforms.httpclient import current_trial
from app.willhaben.public import Blocked, RateLimiter

log = logging.getLogger(__name__)

BASE = "https://geizhals.at"
_limiter = RateLimiter(3.0, name="Geizhals")
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
    if (tunnel := vpn.tunnel_for(vpn.GEIZHALS)) is not None:
        return await _get_vpn(tunnel, url, headers)
    if current_trial() is not None:   # the test after a shuffle: never from the home IP, even after the switch went off
        raise GeizhalsError(t("Geizhals: the VPN is off, the test sends nothing directly"))
    r = None
    try:
        async with _limiter:
            if vpn.enabled(vpn.GEIZHALS):     # switched on while it waited: through the VPN after all
                _limiter.refund()
            else:
                try:
                    async with _client(None) as c:
                        r = await c.get(url, headers=headers)
                except httpx.HTTPError as e:
                    raise GeizhalsError(t("Geizhals unreachable: {error}", error=e)) from e
    except Blocked as e:              # still pausing after a block: no request at all
        raise GeizhalsError(str(e)) from e
    if r is None:
        return await _get(url)
    if r.status_code in (403, 429):   # blocked: pause (60 s, doubling) instead of asking again every 2 s
        _limiter.penalize()
    elif r.status_code == 200:
        _limiter.reward()
    if r.status_code != 200:
        raise GeizhalsError(t("Geizhals answered HTTP {status}", status=r.status_code))
    return r.text


def _client(proxy: str | None) -> httpx.AsyncClient:
    """One request's client: direct, or through the VPN's local proxy (never the environment's proxy settings)."""
    if proxy is None:
        return httpx.AsyncClient(timeout=15, follow_redirects=True, transport=_transport)
    if _transport is not None:        # httpx would send through the proxy and quietly skip the mock
        raise GeizhalsError("a mock transport and a proxy at the same time")
    return httpx.AsyncClient(timeout=15, follow_redirects=True, proxy=proxy, trust_env=False)


async def _get_vpn(tunnel: vpn.Tunnel, url: str, headers: dict[str, str]) -> str:
    """Through the VPN's shared connection; no route: GeizhalsError, never a direct request. A 403/429, Cloudflare's
    challenge or no answer refuses the server (Tunnel.failed(): Geizhals pauses on it, a shuffle starts) and does not
    penalize the limiter, whose pause is for blocks of the home IP. Under a Trial (the test after a shuffle) through
    its server only, and what it met is noted there."""
    tr = current_trial()
    try:
        route = tr.route if tr is not None else await tunnel.ensure()
    except PlatformError as e:
        raise GeizhalsError(str(e)) from e
    error: httpx.HTTPError | None = None
    try:
        async with _limiter:
            if tunnel.current() != route:     # the connection changed while it waited: nothing goes out
                _limiter.refund()
                raise GeizhalsError(t("Geizhals: the VPN server changed before the request went out"))
            try:
                async with _client(route.proxy) as c:
                    r = await c.get(url, headers=headers)
            except httpx.HTTPError as e:
                error = e
    except Blocked as e:              # still pausing after a block: no request at all
        raise GeizhalsError(str(e)) from e
    if error is not None:
        detail = str(error)[:160] or type(error).__name__
        if tr is not None:
            tr.sent, tr.refused, tr.detail = tr.sent + 1, "unreachable", detail
        else:
            await tunnel.failed(route, "unreachable", detail)
        raise GeizhalsError(t("Geizhals unreachable through the VPN: {error}", error=detail)) from error
    challenge = r.headers.get("cf-mitigated", "").lower() == "challenge"
    if tr is not None:
        tr.sent, tr.status = tr.sent + 1, r.status_code
        tr.ctype, tr.challenge = r.headers.get("content-type", "").lower(), challenge
    if r.status_code in (403, 429) or challenge:
        reason = "rate" if r.status_code == 429 else "blocked" if r.status_code == 403 else "botcheck"
        if tr is not None:
            tr.refused, tr.detail = reason, f"HTTP {r.status_code}"
        else:
            await tunnel.failed(route, reason, f"HTTP {r.status_code}")
        raise GeizhalsError(t("Geizhals refused the VPN server (HTTP {status})", status=r.status_code))
    if r.status_code == 200:
        _limiter.reward()
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


_WATT_RE = re.compile(r"(\d)\s*w\b", re.I)
_SLUG_RE = re.compile(r"https://geizhals\.at/(.+?)(?:-a\d+)?\.html")
# words that make another product of the same line: "iPhone 16" is not the "16 Pro Max", "Switch 8" not "Enterprise 8"
VARIANT_WORDS = {"max", "plus", "mini", "ultra", "lite", "pro", "se", "air", "fe", "neo", "enterprise", "flex", "xg",
                 "compute", "mesh", "lr"}
# brands and lines a seller or Geizhals may leave out ("UniFi UNAS 2" is "Ubiquiti UNAS 2"; "U6 Mesh" sells without
# "Ubiquiti UniFi"): a match need not have them, and the price check's comparable search drops them
BRANDS = frozenset({"ubiquiti", "unifi", "apple", "amazon", "samsung", "sony", "google", "microsoft"})
# a result with one of these the query lacks is something else: an accessory, a multi-pack or a bundle
OTHER_THING_WORDS = {"kit", "halterung", "mount", "hülle", "case", "cover", "adapter", "kabel", "netzteil", "upgrade",
                     "ersatzteil", "ersatzteile", "zubehör", "pack", "bundle", "bundles"}


def _norm(s: str) -> str:
    return _WATT_RE.sub(r"\1w", glue_units(s)).lower()


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]", "", (s or "").lower())


def _parts(s: str) -> list[str]:
    return [x for x in re.split(r"[^a-z0-9äöüß]+", (s or "").lower()) if x]


def _has_run(parts: list[str], within: list[str]) -> bool:
    """The code's parts appear one after another: "USW-24-POE" in "…-switch-usw-24-poe", not "18V-55" in "18V-550"."""
    k = len(parts)
    return any(within[i:i + k] == parts for i in range(len(within) - k + 1))


def _has_word(name: str, w: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(w)}(?!\w)", name) is not None


def is_code(token: str) -> bool:
    """An order code such as "USW-24-POE", "UAP-AC-PRO", "BE-02001.02" or "UXG-Lite": parts joined by "-", each in
    capitals or digits (or a variant word), at least five letters/digits; not "RJ-45", "Wi-Fi" or "WLAN-Repeater"."""
    parts = token.split("-")
    if len(parts) < 2 or len(_flat(token)) < 5 or not all(parts):
        return False
    return all(re.fullmatch(r"[A-Z0-9.]+", p) or p.lower() in VARIANT_WORDS for p in parts) \
        and any(re.fullmatch(r"[A-Z]{2,}|[0-9.]+[A-Z]*", p) for p in parts)


def strong_code(token: str) -> bool:
    """A code that names one product ("USW-24-POE", "BE-02001.02"), worth a search of its own – unlike "18V-55"."""
    return is_code(token) and (token.count("-") >= 2 or len(_flat(token)) >= 7)


def _tokens(query: str) -> list[str]:
    return [w for w in (x.strip(".-") for x in _QTOK_RE.findall(glue_units(query))) if w]


def _similar(w: str, words: set[str]) -> bool:
    """Typos in longer words: "rasperry" ≈ "raspberry"."""
    return len(w) >= 5 and any(abs(len(x) - len(w)) <= 2 and SequenceMatcher(None, w, x).ratio() >= 0.85 for x in words)


_ORDINAL_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th|ª|º)")                          # "3rd", "5ª": the generation number
_GLUED_RE = re.compile(r"([a-zäöü]+)(\d+)(\+?)")                                   # "Pi4", "Show8", "U6", "U6+"


def _model_found(w: str, n: str, parts: list[str]) -> bool:
    """A query word with a digit in the (normalised) name: as a whole word ("13" is not "113" or "230cm"), a bare
    number from 5 on also as a port count ("24" in "24x RJ-45"), glued or abbreviated to its line: "Pi4" is "Pi 4",
    "Show8" "Show 8", "U6" "UniFi 6" – and "U6+" only "UniFi 6+", "U6" not."""
    if _has_word(n, w) or w.isdigit() and int(w) >= 5 and _has_word(n, w + "x"):
        return True
    m = _GLUED_RE.fullmatch(w)
    if not m:
        return False
    letters, digits, plus = m.groups()
    for p, nxt in zip(parts, parts[1:], strict=False):
        if nxt == digits and (p == letters or len(letters) <= 2 and p.startswith(letters)):
            return bool(plus) == bool(re.search(rf"(?<!\w){re.escape(p)}\s*{digits}\+", n))
    return False


def matches(name: str, query: str, url: str = "") -> bool:
    """Is this product really the one asked for (the variant too)? Every word with a digit – also a bare "4" as in
    "Pi 4", and capacities like "4GB" – appears in the name (_model_found: a whole word, "13" is not "113" or "230cm",
    "4" not "400" or "4GB"; glued or abbreviated, "Pi4", "U6" for "UniFi 6"; "3rd" as "3"); an order code in the name
    or the product link; most other words of three letters or more appear as words (hyphens, compounds and small typos
    forgiven; "Lot" is not in "Merlotglas"; a brand of BRANDS the name lacks does not count), all of them when the
    query has no number or code ("Ubiquiti UniFi Cloud Gateway Max"); and query and name have
    the same variant words (Pro, Max, Lite …), the name no pack or accessory word the query lacks. "4 GB" equals "4GB",
    "150 W" "150W"; a capacity only in the name is fine."""
    n = _norm(name)
    slug = _SLUG_RE.match(url or "")
    flat = _flat(name)
    raw = _tokens(query)
    if not raw:
        return False
    codes = [w for w in raw if is_code(w)]
    toks = [_ORDINAL_RE.sub(r"\1", _norm(w)) if _ORDINAL_RE.fullmatch(_norm(w)) else _norm(w) for w in raw if not is_code(w)]
    words = [w for w in toks if not re.search(r"\d", w) and len(w) >= 3]     # not "de", "of": they are in any name
    model = [w + "+" if "plus" in words and _GLUED_RE.fullmatch(w + "+") else w     # "U6 Plus" is the "U6+"
             for w in toks if re.search(r"\d", w)]
    where = (_parts(name), _parts(slug.group(1) if slug else ""))
    if not all(any(_has_run(_parts(c), w) for w in where) for c in codes) \
            or not all(_model_found(w, n, where[0]) for w in model):
        return False
    asked = {x for w in toks + codes for x in _parts(w)} | {m.group(1) for w in model if (m := _GLUED_RE.fullmatch(w))}
    name_words = set(re.findall(r"[a-z0-9äöüß]+", n)) | ({"plus"} if re.search(r"\d\+(?![\w+])", n) else set())
    asked |= {"plus"} if any(w.endswith("+") for w in model) else set()       # "U6+" asks for the "UniFi 6+"
    if (name_words & (VARIANT_WORDS | OTHER_THING_WORDS)) - asked or {w for w in words if w in VARIANT_WORDS} - name_words:
        return False

    def found(w: str) -> bool:      # a whole word, part of a compound ("UniFi" in "UniFiSwitch", "i-phone"), a typo
        return w in name_words or len(_flat(w)) >= 4 and _flat(w) in flat or _similar(w, name_words)

    joined = {x for a, b in zip(words, words[1:], strict=False) if found(a + b) for x in (a, b)}   # "ESY LUX": "Esylux"
    counted = [w for w in words if w not in BRANDS or found(w)]       # a brand Geizhals leaves out does not count
    need = 0.6 if model or codes else 1.0       # no number or code to go by: every word ("Dream Machine UDM" is no
    return not counted or sum(w in joined or found(w) for w in counted) / len(counted) >= need    # "… Beast")


def best_hit(hits: list[Hit], query: str) -> Hit | None:
    """The first hit (Geizhals' order) that is really this product and variant."""
    return next((h for h in hits if matches(h.name, query, h.url)), None)


def search_queries(query: str) -> list[str]:
    """What to search for, most promising first, at most three: an order code that names one product alone (Geizhals
    finds "USW-24-POE" but often nothing for the code among other words), the query, then its key words – the first
    two, variant words and numbers ("Ubiquiti UniFi Pro PoE 48" -> "Ubiquiti UniFi Pro 48"); lookup() searches the
    key words only when nothing was found at all."""
    raw = _tokens(query)
    codes = [w for w in raw if strong_code(w)]
    key = raw[:2] + [w for w in raw[2:] if not is_code(w) and (re.search(r"\d", w) or w.lower() in VARIANT_WORDS)]
    out = codes[:1] + [query] + ([" ".join(key)] if len(key) >= 2 else [])
    return list(dict.fromkeys(q for q in out if q))[:3]


async def lookup(query: str, page: bool = True) -> dict[str, Any] | None:
    """Best match for a product name, as stored on the draft; None if nothing usable was found. `verified`: the
    product (search card and page title) matches the query, variant included; False = Geizhals' first hit as a guess.
    Searches again (search_queries) while nothing found fits; a failing search raises GeizhalsError as before.
    page=False: no product page (no spec sheet; the card's price is the lowest offer too) – one request less for a
    mere price lookup."""
    query = query.strip()
    if not query:
        return None
    hits: list[Hit] = []
    best = None
    queries = search_queries(query)
    for i, q in enumerate(queries):
        if i == 2 and hits:          # the key words only when the code and the query found nothing at all
            break
        hits += [h for h in await search(q, limit=12) if h.url not in {x.url for x in hits}]
        best = best_hit(hits, query)
        if best is not None:
            break
    if not hits:
        return {"query": query, "match": None, "candidates": [], "verified": False}
    if not page:
        top = best or hits[0]
        return {"query": query, "match": {"name": top.name, "url": top.url, "price_from": top.price, "specs": []},
                "candidates": [asdict(h) for h in hits[:12]], "verified": best is not None}
    prod = await product((best or hits[0]).url)
    verified = best is not None and (not prod.name or matches(prod.name, query, prod.url))   # the h1 can be more precise
    return {"query": query, "match": asdict(prod), "candidates": [asdict(h) for h in hits[:12]], "verified": verified}


def spec_block(match: dict[str, Any] | None) -> str:
    """Text appended to the description when the user ticks "add the spec sheet" (ad content: stays German)."""
    if not match or not match.get("specs"):
        return ""
    lines = [f"Technische Daten ({match['name']}, laut Hersteller/Geizhals):"]
    lines += [f"- {k}: {v}" for k, v in match["specs"]]
    return "\n".join(lines)
