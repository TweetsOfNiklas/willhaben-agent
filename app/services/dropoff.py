"""Nearest places to hand in a parcel, per carrier, from the sender's address.

The carriers' own shop finders answer JSON without a login (found via their finder pages, 2026-10-09):
  Post    api.post.at GraphQL `branches` in a bounding box (post offices, Post Partner, stations with
          service 534 "Versandstation"; pickup-only stations are skipped)
  GLS     api.gls-group.net parcel shops (public key from gls-group.eu), only `offersPrepaidParcelDropOff`
  DPD     mydpd.at shop/findByAddr (geocodes itself; bot-protected, so it may fail now and then), only "WEB"
  Hermes  wsfinder.hlg.at FindPS (PaketShops; all accept parcels)
The sender address is geocoded once with Nominatim (cached in the setting `ship_sender_geo`). Results are
cached for 12 h; a failing finder only means that carrier shows no distance.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

import httpx

from app.db import get_setting, set_setting

log = logging.getLogger(__name__)

TTL_S = 12 * 3600
TIMEOUT_S = 8
NOMINATIM_UA = "willhaben-agent/0.1 (self-hosted; parcel drop-off lookup)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
GLS_KEY = "wpYvQxm7hN363U9lpSrWQL6qkTLx3kPl"   # public, embedded in gls-group.eu's shop finder

_transport: httpx.AsyncBaseTransport | None = None   # tests inject a mock transport
_cache: dict[str, tuple[float, dict[str, list[dict[str, Any]]]]] = {}


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


def _point(name: str, street: str, zip_: str, city: str, lat: float | None, lon: float | None, kind: str,
           origin: tuple[float, float], hours: str = "") -> dict[str, Any]:
    d = _dist_m(origin[0], origin[1], lat, lon) if lat is not None and lon is not None else None
    name, street = " ".join((name or "").split()), " ".join((street or "").split())
    if street and street in name:      # Post: "Abholstation A 1160 Wien, Rankgasse 13" already says where
        name = name.replace(street, "").strip(" ,")
    return {"name": name, "address": f"{street}, {zip_} {city}".strip(", "),
            "lat": lat, "lon": lon, "dist_m": d, "kind": kind, "hours": hours}


def address_line(a: dict[str, str]) -> str:
    return f"{a.get('street', '')} {a.get('house_no', '')}, {a.get('zip', '')} {a.get('city', '')}".strip(" ,")


# ------------------------------------------------------------------ geocoding
async def geocode(c: httpx.AsyncClient, addr: str) -> tuple[float, float] | None:
    try:
        cached = json.loads(get_setting("ship_sender_geo") or "{}")
    except ValueError:
        cached = {}
    if cached.get("addr") == addr and cached.get("lat") is not None:
        return cached["lat"], cached["lon"]
    r = await c.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": NOMINATIM_UA},
                    params={"format": "json", "countrycodes": "at", "limit": 1, "q": addr})
    hits = r.json() if r.status_code == 200 else []
    if not hits:
        return None
    lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
    set_setting("ship_sender_geo", json.dumps({"addr": addr, "lat": lat, "lon": lon}))
    return lat, lon


# ------------------------------------------------------------------ carriers
_POST_QUERY = """query($filter: BranchFilterType){branches(filter:$filter){type name address{streetname streetnumber
 postalcode city coordinates{latitudewgs84 longitudewgs84}} openinghourformatted services{serviceid}}}"""


async def _post(c: httpx.AsyncClient, origin: tuple[float, float], addr: str) -> list[dict[str, Any]]:
    lat, lon = origin
    box = {"coordinatesnw": {"latitudewgs84": lat + 0.02, "longitudewgs84": lon - 0.03},
           "coordinatesso": {"latitudewgs84": lat - 0.02, "longitudewgs84": lon + 0.03}}
    r = await c.post("https://api.post.at/branchservice/core/graphql", headers={"User-Agent": BROWSER_UA},
                     json={"query": _POST_QUERY, "variables": {"filter": {
                         "boundingbox": box, "branchtype": ["FILIALE", "POSTPARTNER", "ABHOLSTATION"]}}})
    r.raise_for_status()
    out = []
    for b in (r.json().get("data") or {}).get("branches") or []:
        services = {s.get("serviceid") for s in b.get("services") or []}
        if b.get("type") == "ABHOLSTATION" and 534 not in services:
            continue                               # pickup-only station: cannot hand in parcels
        a, co = b.get("address") or {}, (b.get("address") or {}).get("coordinates") or {}
        kind = {"FILIALE": "post office", "POSTPARTNER": "Post Partner", "ABHOLSTATION": "station"}.get(b["type"], b["type"])
        out.append(_point(b.get("name"), f"{a.get('streetname', '')} {a.get('streetnumber', '')}", a.get("postalcode", ""),
                          a.get("city", ""), co.get("latitudewgs84"), co.get("longitudewgs84"), kind, origin,
                          b.get("openinghourformatted") or ""))
    return out


async def _gls(c: httpx.AsyncClient, origin: tuple[float, float], addr: str) -> list[dict[str, Any]]:
    r = await c.get("https://api.gls-group.net/parcel-shop-management/v2/available-public-parcel-shops-pse-bulk",
                    headers={"apikey": GLS_KEY, "User-Agent": BROWSER_UA},
                    params={"latitude": origin[0], "longitude": origin[1], "distance": 3})
    r.raise_for_status()
    return [_point(s.get("name"), f"{s.get('street', '')} {s.get('houseNumber', '')}", s.get("zipCode", ""),
                   s.get("city", ""), s.get("latitude"), s.get("longitude"),
                   "locker" if s.get("type") == "LOCKER" else "shop", origin)
            for s in r.json() if s.get("offersPrepaidParcelDropOff") and not s.get("isTodayOnVacation")]


async def _dpd(c: httpx.AsyncClient, origin: tuple[float, float], addr: str) -> list[dict[str, Any]]:
    r = await c.post("https://www.mydpd.at/jws.php/shop/findByAddr",
                     headers={"User-Agent": BROWSER_UA, "X-Requested-With": "XMLHttpRequest",
                              "Content-Type": "application/json"},
                     json=[addr, "AT", 1, {"weekdays": [], "filter": {}, "depot": None, "inactiveDate": False}])
    r.raise_for_status()
    shops = ((r.json() or {}).get("data") or {}).get("shops") or []
    return [_point(s.get("name1"), s.get("street", ""), s.get("postcode", ""), s.get("city", ""),
                   _f(s.get("lat")), _f(s.get("lng")), "locker" if s.get("type") == "LOCKER" else "shop", origin)
            for s in shops if "WEB" in (s.get("services") or []) and s.get("is_full") != "Y" and s.get("active", True)]


async def _hermes(c: httpx.AsyncClient, origin: tuple[float, float], addr: str) -> list[dict[str, Any]]:
    r = await c.get("https://wsfinder.hlg.at/api//FindPS", headers={"User-Agent": BROWSER_UA}, params={
        "lat": origin[0], "lng": origin[1], "acceptsSuitcases": "false", "hasBoxes": "false", "openAtDayOfWeek": "",
        "openAtTime": "", "maxResult": 10, "maxDist": 15000, "searchTerm": "", "typeId": " ", "countries": "AT"})
    r.raise_for_status()
    return [_point(s.get("description"), f"{s.get('street', '')} {s.get('houseNumber', '')}", s.get("postCode", ""),
                   s.get("city", ""), _f(s.get("lat")), _f(s.get("lng")), "shop", origin)
            for s in r.json() if not s.get("onHoliday")]


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


FINDERS = {"post": _post, "gls": _gls, "dpd": _dpd, "hermes": _hermes}


async def nearest(sender: dict[str, str], carriers: list[str], per_carrier: int = 3) -> dict[str, list[dict[str, Any]]]:
    """{carrier: [closest drop-off points]} for the sender's address; {} when the address is unknown."""
    addr = address_line(sender)
    if not (sender.get("street") and sender.get("zip")):
        return {}
    key = f"{addr}|{','.join(sorted(carriers))}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL_S:
        return hit[1]
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=_transport, follow_redirects=True) as c:
        try:
            origin = await geocode(c, addr)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("geocoding the sender address failed: %s", e)
            return {}
        if origin is None:
            log.warning("sender address not found by the geocoder: %s", addr)
            return {}

        async def one(carrier: str) -> list[dict[str, Any]]:
            try:
                pts = await FINDERS[carrier](c, origin, addr)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                log.info("drop-off finder %s failed: %s", carrier, e)
                return []
            pts = sorted((p for p in pts if p["dist_m"] is not None), key=lambda p: p["dist_m"])
            top = pts[:per_carrier]
            if carrier == "post" and not any(p["kind"] != "station" for p in top):   # Päckchen need a counter
                top += [p for p in pts if p["kind"] != "station"][:1]
            return top

        found = await asyncio.gather(*(one(cr) for cr in carriers if cr in FINDERS))
    result = {cr: pts for cr, pts in zip([cr for cr in carriers if cr in FINDERS], found, strict=True)}
    if any(result.values()):
        _cache[key] = (time.monotonic(), result)
    return result


def fmt_dist(m: int | None) -> str:
    if m is None:
        return ""
    return f"{m} m" if m < 1000 else f"{m / 1000:.1f} km".replace(".", ",")
