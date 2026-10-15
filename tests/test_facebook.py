"""Facebook Marketplace through its logged-out web pages: result list, listing page, links, refusals, the stored
location, the streaming read and a search agent end to end."""
import json
from datetime import timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.db import Hit, session, set_setting
from app.platforms import Blocked, InvalidSearchUrl, PlatformError
from app.platforms import facebook as fb
from app.platforms.ratelimit import RateLimiter
from app.services.search_agents import create_agent, run_agent
from tests.conftest import FIXTURES

PAGE = (FIXTURES / "facebook_search.html").read_text(encoding="utf-8")
ITEM = (FIXTURES / "facebook_item.html").read_text(encoding="utf-8")
FIRST = "1681600913571246"
ITEM_ID = "1438848741764802"
LINK = "https://www.facebook.com/marketplace/vienna/search?query=fahrrad&maxPrice=100&radius=20"


@pytest.fixture
def site():
    """facebook.com behind an httpx MockTransport (through the streaming transport): a queue of answers, else the
    search page / the listing page by path. Records the requests."""
    state = {"queue": [], "calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append(request)
        if state["queue"]:
            item = state["queue"].pop(0)
            if isinstance(item, int):
                return httpx.Response(item)
            if isinstance(item, tuple):                     # a redirect
                return httpx.Response(302, headers={"location": item[0]})
            return httpx.Response(200, text=item)
        if request.url.path.startswith(f"/marketplace/item/{ITEM_ID}"):
            return httpx.Response(200, text=ITEM)
        if request.url.path.startswith("/marketplace/item/"):
            return httpx.Response(200, text="<html>nothing</html>")
        return httpx.Response(200, text=PAGE)

    c = fb.FacebookClient(httpx_transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0, name="Facebook")
    fb.set_client(c)
    platforms.reset()
    set_setting("platform_facebook_enabled", "1")
    yield state
    fb.set_client(None)
    platforms.reset()


def test_result_page_becomes_listings():
    page = fb.parse_search(PAGE)
    assert page.title == "Wien, Österreich" and page.rows_found == len(page.ads) == 3
    a = page.ads[0]
    assert a.ad_id == FIRST and a.ref == f"facebook:{FIRST}" and a.platform == "facebook"
    assert a.url == f"https://www.facebook.com/marketplace/item/{FIRST}/" and a.price == 99 and a.price_display == "99 €"
    assert a.location == "Wien" and a.image_url == "https://scontent.example/listing0.jpg" and a.body is None
    assert a.published_at.tzinfo == timezone.utc and a.is_bumped is None
    with pytest.raises(PlatformError):              # login wall / changed page: no search data at all
        fb.parse_search('<html><script type="application/json">{"x": 1}</script></html>')
    empty = json.dumps({"data": {"marketplace_search": {"feed_units": {"edges": []}}}})
    assert fb.parse_search(f'<script type="application/json">{empty}</script>').ads == []


def test_listing_page_becomes_detail():
    d = fb.parse_item_page(ITEM, ITEM_ID)
    assert d.heading == "Couch" and d.price == 50 and d.price_display == "50 €" and d.status == "active"
    assert d.images == ["https://scontent.example/couch0.jpg", "https://scontent.example/couch1.jpg"]
    assert d.description.startswith("1,70m x 2,20m") and d.description.endswith("Zustand: Gebraucht – guter Zustand")
    assert d.item_condition == "used" and d.item_condition_label == "Gebraucht – guter Zustand"
    assert d.category_path == ["Haushaltsbedarf", "Möbel", "Wohnzimmermöbel", "Sofas"] and d.category_id is None
    assert d.location == d.place == "Oberwart, BGLD." and d.ref == f"facebook:{ITEM_ID}"
    assert fb.parse_item_page(ITEM, "999") is None and fb.parse_item_page("<html>login</html>", ITEM_ID) is None


def test_links_sort_and_location():
    p = fb.FacebookPlatform()
    spec = p.parse_search_url(LINK + "&ref=search")
    assert spec.keyword == "fahrrad" and spec.price_to == 100 and spec.location == "vienna" and spec.sort == "newest"
    assert spec.extra == {"radius": "20"} and "sortBy=creation_time_descend" in spec.url and "ref=" not in spec.url
    assert p.parse_search_url("https://m.facebook.com/marketplace/100232032143091/search/?query=sofa&sortBy=price_ascend"
                              ).sort == "price_asc"
    assert fb.location_from_link("https://www.facebook.com/marketplace/graz/?radius=40") == ("graz", "40")
    with pytest.raises(InvalidSearchUrl):
        fb.location_from_link("https://www.facebook.com/marketplace/item/123/")
    # keyword searches (product search) need the stored place
    assert p.keyword_search("sofa", None, 300, sort="relevance").location is None
    set_setting(fb.LOCATION_KEY, json.dumps({"loc": "vienna", "radius": "40", "place": "Wien"}))
    spec = p.keyword_search("sofa", None, 300, sort="relevance")
    assert spec.url == "https://www.facebook.com/marketplace/vienna/search?query=sofa&maxPrice=300&radius=40"


def test_streaming_read_stops_when_the_data_is_there():
    assert fb._complete(bytearray(b'x "feed_units": 1 </script> tail'), fb.SEARCH_MARKERS)
    assert not fb._complete(bytearray(b'x "feed_units": 1 no end yet'), fb.SEARCH_MARKERS)
    assert fb._markers("https://www.facebook.com/marketplace/item/1/") == fb.ITEM_MARKERS
    assert fb._markers("https://www.facebook.com/marketplace/vienna/search?query=x") == fb.SEARCH_MARKERS


async def test_search_and_detail_through_the_platform(site):
    p = platforms.get("facebook")
    page = await p.search(p.parse_search_url(LINK))
    assert [a.ad_id for a in page.ads][0] == FIRST and page.title == "Wien, Österreich"
    req = site["calls"][-1]
    assert req.headers["sec-fetch-mode"] == "navigate" and "cookie" not in req.headers
    d = await p.detail(ITEM_ID)
    assert d.heading == "Couch"
    assert await p.detail("123456789") is None
    with pytest.raises(PlatformError, match="location"):
        await p.search(p.keyword_search("sofa"))                    # no place stored yet


async def test_refusals_unknown_place_and_login_wall(site):
    p = platforms.get("facebook")
    spec = p.parse_search_url(LINK)
    site["queue"].append(400)
    with pytest.raises(Blocked):
        await p.search(spec)
    p.client.limiter = RateLimiter(0, name="Facebook")
    site["queue"].append(("https://www.facebook.com/login/?next=x",))
    site["queue"].append("<html>login</html>")
    with pytest.raises(Blocked):
        await p.search(spec)
    p.client.limiter = RateLimiter(0, name="Facebook")
    site["queue"].append(("https://www.facebook.com/marketplace/category/search/?query=fahrrad",))
    site["queue"].append(PAGE)
    with pytest.raises(PlatformError, match="location"):
        await p.search(spec)
    site["queue"].append("<html>no data</html>")
    with pytest.raises(PlatformError, match="login wall"):
        await p.search(spec)


async def test_agent_end_to_end(site):
    a = create_agent(LINK)
    assert a.platform == "facebook" and a.name == "fahrrad"
    res = await run_agent(a.id)
    assert res.baseline and res.error is None
    with session() as db:
        hits = list(db.exec(select(Hit).where(Hit.agent_id == a.id)))
    assert len(hits) == 3 and FIRST in {h.ad_id for h in hits} and {h.platform for h in hits} == {"facebook"}


def test_location_in_the_settings(site):
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/settings/platform/facebook", data={"enabled": "1", "product_search": "1",
                                                         "fb_location": "https://www.facebook.com/marketplace/vienna/search?query=x&radius=40"})
        assert r.status_code in (200, 303)
        assert fb.location() == {"loc": "vienna", "radius": "40", "place": "Wien, Österreich",
                                 "link": "https://www.facebook.com/marketplace/vienna/search?query=x&radius=40"}
        page = c.get("/settings/facebook").text
        assert "Wien, Österreich" in page and 'name="fb_location"' in page
        c.post("/settings/platform/facebook", data={"enabled": "1", "fb_location": ""})
        assert fb.location() is None
