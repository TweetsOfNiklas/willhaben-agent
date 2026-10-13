"""Distance from home to ads: coordinates from the sites, postcode centres from Nominatim (cached, paced, resting after
a failure), and the product search's offers shown with their distance and sortable by it."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.db import get_setting, set_setting
from app.platforms import kleinanzeigen as ka
from app.services import dropoff, geo
from app.willhaben.parse import parse_search
from tests.conftest import load

HOME = {"name": "Max", "street": "Hauptstraße", "house_no": "1", "zip": "1150", "city": "Wien"}


@pytest.fixture
def nominatim(monkeypatch):
    """Nominatim answers by postcode (unknown: an empty list); every request is recorded."""
    asked = []
    points = {"6971": ("47.48", "9.73"), "10115": ("52.53", "13.38"), "1150": ("48.19", "16.33")}

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        pc = request.url.params.get("postalcode") or request.url.params.get("q", "").split(", ")[-1].split(" ")[0]
        if pc == "500":
            return httpx.Response(500)
        hit = points.get(pc)
        return httpx.Response(200, json=[{"lat": hit[0], "lon": hit[1]}] if hit else [])

    monkeypatch.setattr(dropoff, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(geo, "MIN_GAP_S", 0)
    monkeypatch.setattr(geo, "_down_until", [0.0])
    return asked


def test_sites_bring_coordinates():
    ad = parse_search(load("search_fahrrad_rows3.json")).ads[0]
    assert (ad.lat, ad.lon) == (48.1, 13.1) and ad.as_dict()["lat"] == 48.1 and ad.as_dict()["postcode"] == "5230"
    k = ka.parse_ads(load("kleinanzeigen_ads.json")).ads[0]
    assert (k.lat, k.lon) == (52.5, 13.4)
    from app.willhaben.parse import _coordinates
    assert _coordinates("") == {} and _coordinates("x,y") == {} and _coordinates("0,0") == {} and _coordinates("91,1") == {}


async def test_postcode_points_are_cached(nominatim):
    assert await geo.postcode_point("6971", "at") == (47.48, 9.73)
    assert await geo.postcode_point("6971", "at") == (47.48, 9.73) and len(nominatim) == 1     # cached
    assert nominatim[0]["countrycodes"] == "at" and nominatim[0]["postalcode"] == "6971"
    assert await geo.postcode_point("9999", "at") is None and await geo.postcode_point("9999", "at") is None
    assert len(nominatim) == 2 and json.loads(get_setting(geo.CACHE_KEY))["AT:9999"] is None   # "not found" kept


async def test_a_failure_rests_nominatim(nominatim):
    assert await geo.postcode_point("500", "at") is None
    assert await geo.postcode_point("6971", "at") is None and len(nominatim) == 1             # resting, no request
    assert "AT:500" not in json.loads(get_setting(geo.CACHE_KEY) or "{}")                     # failures not cached


async def test_home_and_locate(nominatim):
    assert await geo.home() is None and geo.home_cached() is None                              # no address yet
    set_setting("ship_sender", json.dumps(HOME))
    assert await geo.home() == (48.19, 16.33) and geo.home_cached() == (48.19, 16.33)
    results = [{"lat": 48.1, "lon": 13.1}, {"postcode": "6971"}, {"location": "10115 Berlin"}, {"location": "Amazon"}]
    await geo.locate(results)
    assert results[1]["lat"] == 47.48 and results[2]["lat"] == 52.53 and "lat" not in results[3]
    assert round(geo.distance_km(results[1])) == 499 and geo.distance_km(results[3]) is None
    assert geo.country_of("1150") == "at" and geo.country_of("10115") == "de" and geo.country_of("A-1150") is None


def test_offers_show_and_sort_by_distance():
    from app.main import app

    results = [{"ad_id": "1", "platform": "willhaben", "title": "Weit weg", "price": 100.0, "url": "https://x/1",
                "location": "Hard", "lat": 47.48, "lon": 9.73, "fit": 90, "position": {"pos": None}},
               {"ad_id": "2", "platform": "willhaben", "title": "Ums Eck", "price": 150.0, "url": "https://x/2",
                "location": "Wien", "lat": 48.2, "lon": 16.37, "fit": 70, "position": {"pos": None}},
               {"ad_id": "3", "platform": "vinted", "title": "Ohne Ort", "price": None, "url": "https://x/3", "fit": 60, "position": {"pos": None}}]
    with dbmod.session() as s:
        row = dbmod.ProductSearch(description="Rad", status="done", results=results, settings={"platforms": ["willhaben"]})
        s.add(row)
        s.commit()
        s.refresh(row)
    with TestClient(app) as c:
        frag = c.get(f"/deals/search/{row.id}").text
        assert 'aria-label="Sort the offers"' in frag and "km away" not in frag
        assert 'disabled title="Enter your address' in frag and 'data-km=""' in frag            # no home: no distances
        set_setting("ship_sender", json.dumps(HOME))
        set_setting("ship_sender_geo", json.dumps({"addr": dropoff.address_line(HOME), "lat": 48.19, "lon": 16.33}))
        frag = c.get(f"/deals/search/{row.id}").text
    assert "499 km away" in frag and "3,2 km away" in frag and 'disabled title="Enter' not in frag
    assert 'data-i="0" data-price="100.0" data-km="498.76' in frag and 'data-i="2" data-price="" data-km=""' in frag
