"""Distance from home to ads: coordinates from the sites, postcode centres from Nominatim (cached, paced, resting after
a failure), and the product search's offers shown with their distance and sortable by it."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

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


def _search_with_offers():
    results = [{"ad_id": str(n), "platform": "willhaben", "title": f"Rad {n}", "price": 100.0 + n, "url": f"https://x/{n}",
                "location": "Wien", "lat": 48.2, "lon": 16.37, "fit": 90 - n, "position": {"pos": None}} for n in range(2)]
    with dbmod.session() as s:
        row = dbmod.ProductSearch(description="Rad", status="done", results=results, settings={"platforms": ["willhaben"]})
        s.add(row)
        s.commit()
        s.refresh(row)
    return row.id


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.02)
    assert cond()


def test_saving_the_address_locates_it(nominatim):
    """The address entered under Settings → Shipping is located in the background, so the distance sort works at once."""
    from app.main import app

    sid = _search_with_offers()
    with TestClient(app) as c:
        assert 'disabled title="Enter your address' in c.get(f"/deals/search/{sid}").text
        assert c.post("/settings/shipping", data=HOME, follow_redirects=False).status_code == 303
        _wait(lambda: geo.home_cached() is not None)
        frag = c.get(f"/deals/search/{sid}").text
    assert geo.home_cached() == (48.19, 16.33) and "km away" in frag and "disabled title=" not in frag
    assert len(nominatim) == 1


def test_an_address_not_found_gets_its_own_hint(nominatim):
    from app.main import app

    sid = _search_with_offers()
    with TestClient(app) as c:
        c.post("/settings/shipping", data={**HOME, "zip": "9999", "city": "Nirgendwo"}, follow_redirects=False)
        _wait(lambda: len(nominatim) == 1)
        frag = c.get(f"/deals/search/{sid}").text
    assert geo.home_cached() is None and "km away" not in frag and "Enter your address" not in frag
    assert 'disabled title="Your address under Settings → Shipping &amp; pickup could not be located (yet)' in frag


def _home():
    set_setting("ship_sender", json.dumps(HOME))
    set_setting("ship_sender_geo", json.dumps({"addr": dropoff.address_line(HOME), "lat": 48.19, "lon": 16.33}))


async def test_agent_distance_limit_holds_far_pickup_ads(nominatim):
    from app.db import Hit, SearchAgent
    from app.services import search_agents as sa

    _home()
    agent = SearchAgent(name="Rad", source_url="x", webapi_url="x", max_km=50)
    near = Hit(agent_id=1, ad_id="1", heading="a", url="u", lat=48.2, lon=16.4, shipping="pickup")
    far = Hit(agent_id=1, ad_id="2", heading="b", url="u", postcode="6971", location="6971 Hard", shipping="pickup")
    far_ships = Hit(agent_id=1, ad_id="3", heading="c", url="u", lat=47.48, lon=9.73, shipping="ship")
    nowhere = Hit(agent_id=1, ad_id="4", heading="d", url="u")
    assert sa._holds(SearchAgent(name="x", source_url="x", webapi_url="x", max_km=10))
    assert sa.distance_passes(agent, far)                      # its postcode not asked yet: unknown passes
    assert await geo.locate_hits([near, far, far_ships, nowhere]) == [far]
    assert (far.lat, far.lon) == (47.48, 9.73) and len(nominatim) == 1
    assert sa.distance_passes(agent, near) and not sa.distance_passes(agent, far)
    assert sa.distance_passes(agent, far_ships) and sa.distance_passes(agent, nowhere)
    agent.max_km = None
    assert sa.distance_passes(agent, far)


def test_hit_cards_show_the_distance_and_sort_by_it():
    from app.db import Hit, SearchAgent, session
    from app.main import app

    _home()
    with session() as db:
        a = SearchAgent(name="Rad", source_url="x", webapi_url="x", max_km=100)
        db.add(a)
        db.commit()
        db.add(Hit(agent_id=a.id, ad_id="1", heading="Ad 1", url="u1", lat=47.48, lon=9.73, shipping="pickup"))
        db.add(Hit(agent_id=a.id, ad_id="2", heading="Ad 2", url="u2", lat=48.2, lon=16.4))
        db.add(Hit(agent_id=a.id, ad_id="3", heading="Ad 3", url="u3"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/hits?sort=distance").text
        assert page.index(">Ad 2<") < page.index(">Ad 1<") < page.index(">Ad 3<")
        assert "Nearest first" in page and 'class="text-warning">499 km' in page    # over the agent's 100


def test_global_pickup_radius_hides_far_pickup_only_ads():
    from app.db import Hit, SearchAgent, session
    from app.main import app
    from app.services import search_agents as sa

    _home()
    with TestClient(app) as c:
        c.post("/settings/shipping", data={**HOME, "pickup_max_km": "100"})
        assert geo.pickup_radius() == 100 and 'name="pickup_max_km" type="number" min="1" max="2000" step="1" value="100"' \
            in c.get("/settings/shipping").text
        with session() as db:
            a = SearchAgent(name="Rad", source_url="x", webapi_url="x")
            db.add(a)
            db.commit()
            for ad_id, lat, lon, ship in (("1", 47.48, 9.73, None), ("2", 47.48, 9.73, "ship"), ("3", 48.2, 16.4, None),
                                          ("4", None, None, None)):
                db.add(Hit(agent_id=a.id, ad_id=ad_id, heading=f"Ad {ad_id}", url=f"u{ad_id}", lat=lat, lon=lon,
                           shipping=ship))
            db.commit()
            far = db.exec(select(Hit).where(Hit.ad_id == "1")).one()
            agent = db.get(SearchAgent, a.id)
        assert sa.pickup_limit(agent) == 100 and sa._holds(agent) and not sa.distance_passes(agent, far)
        agent.max_km = 600                                             # the agent's own limit comes first
        assert sa.distance_passes(agent, far)
        page = c.get("/hits").text
        assert ">Ad 1<" not in page and all(f">Ad {n}<" in page for n in "234")   # ships, near, place unknown
        assert ">Ad 1<" in c.get("/hits?state=all").text                         # "All" keeps it
        c.post("/settings/shipping", data={**HOME, "pickup_max_km": ""})
        assert geo.pickup_radius() is None and ">Ad 1<" in c.get("/hits").text
    cards = [{"lat": 47.48, "lon": 9.73, "shipping": None}, {"postcode": "1150", "shipping": "pickup"},
             {"lat": 47.48, "lon": 9.73, "shipping_price": 4.9}]
    assert [geo.too_far(x, 100, (48.19, 16.33)) for x in cards] == [True, False, False]   # 1150: not located yet
