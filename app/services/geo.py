"""Places as coordinates, for the distance from home to an ad (the product search sorted by distance, hit cards and an
agent's "pickup within N km").

Home is the shipping sender address (Settings → Shipping & pickup), geocoded once with Nominatim: the same cached
value the drop-off finder uses (setting `ship_sender_geo`, app/services/dropoff.py). Ads bring their own coordinates
where the site sends them (willhaben COORDINATES, Kleinanzeigen ad-address); for an ad with only a postcode (Shpock)
the postcode's centre is asked once: Nominatim, at most one request a second as its usage policy asks, cached per
country and postcode in the setting `geo_postcodes` (also "not found"). A failed request is not cached, but Nominatim
gets a rest of DOWN_S before the next try (a search must not wait on it ten times).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from app.db import get_setting, set_setting
from app.services import dropoff

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "willhaben-agent (self-hosted; distance to ads)"
MIN_GAP_S = 1.1
DOWN_S = 600
CACHE_KEY = "geo_postcodes"

_lock = asyncio.Lock()
_last = [0.0]
_down_until = [0.0]


def _down() -> bool:
    return time.monotonic() < _down_until[0]


def _failed(what: str, e: Exception) -> None:
    log.info("geocoding %s failed: %s", what, e)
    _down_until[0] = time.monotonic() + DOWN_S


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return dropoff._dist_m(a[0], a[1], b[0], b[1]) / 1000


def home_address() -> str | None:
    """The sender address as one line, None without a postcode or town (the page then asks for an address)."""
    from app.services import shipping

    s = shipping.sender()
    return dropoff.address_line(s) if (s.get("zip") or s.get("city")) else None


def home_cached() -> tuple[float, float] | None:
    """Home, only when it is geocoded already (no request: for page rendering)."""
    addr = home_address()
    try:
        cached = json.loads(get_setting("ship_sender_geo") or "{}")
    except ValueError:
        return None
    if addr and cached.get("addr") == addr and cached.get("lat") is not None:
        return cached["lat"], cached["lon"]
    return None


async def _pace() -> None:
    wait = _last[0] + MIN_GAP_S - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _last[0] = time.monotonic()


async def home() -> tuple[float, float] | None:
    """Home as coordinates; asks Nominatim once per address. None without an address or when it is not found."""
    if (p := home_cached()) is not None:
        return p
    addr = home_address()
    if not addr or _down():
        return None
    async with _lock:
        await _pace()
        try:
            async with httpx.AsyncClient(timeout=dropoff.TIMEOUT_S, transport=dropoff._transport) as c:
                return await dropoff.geocode(c, addr)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            _failed("the home address", e)
            return None


def country_of(postcode: str | None) -> str | None:
    """Austrian postcodes have 4 digits, German ones 5."""
    pc = (postcode or "").strip()
    return {4: "at", 5: "de"}.get(len(pc)) if pc.isdigit() else None


def _cache() -> dict[str, Any]:
    try:
        return json.loads(get_setting(CACHE_KEY) or "{}")
    except ValueError:
        return {}


async def postcode_point(postcode: str, country: str) -> tuple[float, float] | None:
    """The centre of a postcode area (cached; one Nominatim request the first time)."""
    key = f"{country.upper()}:{postcode}"
    if key in (cache := _cache()):
        return tuple(cache[key]) if cache[key] else None
    if _down():
        return None
    async with _lock:
        if key in (cache := _cache()):          # asked meanwhile by another task
            return tuple(cache[key]) if cache[key] else None
        await _pace()
        try:
            async with httpx.AsyncClient(timeout=dropoff.TIMEOUT_S, transport=dropoff._transport) as c:
                r = await c.get(NOMINATIM, headers={"User-Agent": USER_AGENT},
                                params={"format": "json", "limit": 1, "postalcode": postcode, "countrycodes": country})
            hits = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as e:
            _failed(f"postcode {key}", e)
            return None
        if hits is None:
            _failed(f"postcode {key}", ValueError(f"HTTP {r.status_code}"))
            return None
        point = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
        cache = _cache()
        cache[key] = point
        set_setting(CACHE_KEY, json.dumps(cache))
        return tuple(point) if point else None


async def locate(results: list[dict[str, Any]]) -> None:
    """Coordinates for results that have none but a postcode (in place); also geocodes home once. Never raises."""
    try:
        await home()
        for r in results:
            if r.get("lat") is not None:
                continue
            pc = r.get("postcode") or (m.group(1) if (m := re.match(r"(\d{4,5})\b", r.get("location") or "")) else None)
            if (country := country_of(pc)) and (p := await postcode_point(pc, country)):
                r["lat"], r["lon"] = p
    except Exception as e:  # noqa: BLE001 - a distance is a nicety: never fail a search over it
        log.warning("locating results failed: %s", e)


def pickup_radius() -> int | None:
    """Settings → Shipping & pickup: pickup-only ads farther from home than this are not shown (None: no limit)."""
    raw = (get_setting("pickup_max_km") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def ships(x: Any) -> bool:
    """The ad states shipping (a hit, listing or result dict); an ad that says nothing counts as pickup."""
    get = x.get if isinstance(x, dict) else (lambda k: getattr(x, k, None))
    return get("shipping") == "ship" or get("shipping_price") is not None


def too_far(x: Any, limit: int | None, origin: tuple[float, float] | None = None) -> bool:
    """A pickup-only ad farther from home than `limit` km; False when it ships, its place or home is unknown."""
    if not limit or ships(x):
        return False
    km = hit_km(x, origin)
    return km is not None and km > limit


def postcode_of(postcode: str | None, location: str | None) -> str | None:
    """The postcode an ad states, or the one its place line starts with ("1100 Wien")."""
    return postcode or (m.group(1) if (m := re.match(r"(\d{4,5})\b", location or "")) else None)


def point_cached(postcode: str | None) -> tuple[float, float] | None:
    """A postcode's centre, only when asked before (no request: for page rendering)."""
    country = country_of(postcode)
    point = _cache().get(f"{country.upper()}:{postcode}") if country else None
    return tuple(point) if point else None


def hit_km(h: Any, origin: tuple[float, float] | None = None) -> float | None:
    """Distance from home to a hit (or a listing, or a result dict): its coordinates, else its postcode's centre if
    known already; None otherwise."""
    origin = origin or home_cached()
    if origin is None:
        return None
    get = h.get if isinstance(h, dict) else (lambda k: getattr(h, k, None))
    lat, lon = get("lat"), get("lon")
    if lat is None or lon is None:
        p = point_cached(postcode_of(get("postcode"), get("location")))
        if p is None:
            return None
        lat, lon = p
    return km(origin, (lat, lon))


async def locate_hits(hits: list[Any], limit: int = 60) -> list[Any]:
    """Coordinates for hits without any but with a postcode (in place, at most `limit` new postcodes asked); the
    hits that got some. Also geocodes home once. Never raises."""
    located = []
    try:
        if await home() is None:
            return located
        asked = 0
        for h in hits:
            if h.lat is not None:
                continue
            pc = postcode_of(h.postcode, h.location)
            country = country_of(pc)
            if not country:
                continue
            if point_cached(pc) is None and f"{country.upper()}:{pc}" not in _cache():
                if asked >= limit:
                    continue
                asked += 1
            if (p := await postcode_point(pc, country)) is not None:
                h.lat, h.lon = p
                located.append(h)
    except Exception as e:  # noqa: BLE001 - a distance is a nicety: never fail a check over it
        log.warning("locating hits failed: %s", e)
    return located


def distance_km(r: dict[str, Any], origin: tuple[float, float] | None = None) -> float | None:
    """Distance from home (geocoded already) to a result with coordinates, else None."""
    origin = origin or home_cached()
    if origin is None or r.get("lat") is None or r.get("lon") is None:
        return None
    return km(origin, (r["lat"], r["lon"]))
