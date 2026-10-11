"""Shpock, Vinted and Preisjäger in the dashboard: settings pages, the product-search hint on switched-off sites, the
viewer for a non-numeric id and for a site without details, and refs to their ads."""
import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import Hit, SearchAgent, session, set_setting
from app.platforms import preisjaeger, shpock
from app.platforms.ratelimit import RateLimiter
from app.services.deals import parse_ad_ref
from tests.test_platforms_contract import PREISJAEGER_ROUTES, SHPOCK_ROUTES, FileTransport

NEW = ("shpock", "vinted", "preisjaeger")


@pytest.fixture
def sites():
    for mod, cls, routes in ((shpock, "ShpockClient", SHPOCK_ROUTES), (preisjaeger, "PreisjaegerClient", PREISJAEGER_ROUTES)):
        c = getattr(mod, cls)(transport=FileTransport(routes))
        c.limiter = RateLimiter(0)
        mod.set_client(c)
    platforms.reset()
    yield
    shpock.set_client(None)
    preisjaeger.set_client(None)
    platforms.reset()


@pytest.fixture
def client(sites):
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_registered_off_by_default_with_a_settings_page(client):
    assert [p.id for p in platforms.all(enabled_only=False)] == ["willhaben", "kleinanzeigen", *NEW]
    assert [p.id for p in platforms.all()] == ["willhaben"]
    page = client.get("/settings/vinted").text
    assert "about 3 MB" in page and 'action="/settings/platform/vinted"' in page
    nav = page[page.index('<nav aria-label="Settings"'):]
    assert all(f'href="/settings/{pid}"' in nav for pid in NEW) and nav.count(">off</small>") == 4
    assert "the shop is shown where other sites show the place" in client.get("/settings/preisjaeger").text
    client.post("/settings/platform/shpock", data={"enabled": "on", "product_search": "on"})
    assert platforms.enabled("shpock") and platforms.get("shpock").limiter.min_gap_s == 4.0
    assert [p["id"] for p in client.get("/api/platforms").json()] == ["willhaben", "kleinanzeigen", *NEW]


def test_product_search_names_the_switched_off_sites(client):
    page = client.get("/deals").text
    assert "Kleinanzeigen, Shpock, Vinted, Preisjäger can also be searched once you switch them on" in page
    assert 'href="/settings/kleinanzeigen">Switch on' in page and 'name="sites"' not in page
    for pid in ("kleinanzeigen", "shpock"):
        set_setting(f"platform_{pid}_enabled", "1")
    page = client.get("/deals").text
    assert "Vinted, Preisjäger can also be searched" in page and 'href="/settings/vinted">Switch on' in page
    assert page.count('name="sites"') == 3                               # willhaben, Kleinanzeigen, Shpock to tick
    for pid in ("vinted", "preisjaeger"):
        set_setting(f"platform_{pid}_enabled", "1")
    assert "can also be searched once you switch them on" not in client.get("/deals").text


def test_refs_with_letters_and_site_links():
    set_setting("platform_shpock_enabled", "1")
    set_setting("platform_vinted_enabled", "1")
    assert parse_ad_ref("shpock:asu6-t2hxq7bDRFz") == ("shpock", "asu6-t2hxq7bDRFz")
    assert parse_ad_ref("https://www.shpock.com/de-at/i/asu6-t2hxq7bDRFz/iphone") == ("shpock", "asu6-t2hxq7bDRFz")
    assert parse_ad_ref("www.vinted.at/items/10341874098-hulle") == ("vinted", "10341874098")
    assert parse_ad_ref("iphone 15 pro") is None


def test_viewer_for_a_shpock_ad_and_a_deal(client):
    for pid in ("shpock", "preisjaeger"):
        set_setting(f"platform_{pid}_enabled", "1")
    with session() as db:
        a = SearchAgent(name="Deals", platform="preisjaeger", source_url="https://www.preisjaeger.at/search?q=iphone",
                        webapi_url="https://www.preisjaeger.at/search?q=iphone")
        db.add(a)
        db.commit()
        db.refresh(a)
        db.add(Hit(agent_id=a.id, platform="preisjaeger", ad_id="369807", heading="EKE Case 2.0", price=19.92,
                   url="https://www.preisjaeger.at/deals/eke-case-369807", location="EKE Case", body="420° · Elektronik"))
        db.commit()
        hit = db.exec(__import__("sqlmodel").select(Hit).where(Hit.ad_id == "369807")).one()
    frag = client.get("/hits/ad/shpock/asu6-t2hxq7bDRFz").text
    assert "Apple IPhone 15 Pro 128 GB Titan Schwarz" in frag and "Max M." in frag and "Shpock" in frag
    deal = client.get(f"/hits/ad/preisjaeger/369807?hit={hit.id}").text
    assert "EKE Case 2.0" in deal and "Preisjäger shows no details here" in deal
    page = client.get("/hits?state=all").text
    assert 'data-ad="preisjaeger:369807"' in page
