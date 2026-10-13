"""The ad viewer for ads that are no saved hit: what the product search or the direct search showed stands in for sites
without ad details (Preisjäger) and for a site that cannot be reached."""
import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import ProductSearch, session
from app.platforms import PlatformError
from app.services import deals
from tests.fake_platform import FakePlatform

RESULT = {"ad_id": "369807", "platform": "preisjaeger", "ref": "preisjaeger:369807", "title": "EKE Case 2.0 für iPhone 18",
          "price": 19.92, "price_display": "19,92 €", "location": "Amazon", "url": "https://www.preisjaeger.at/share-deal/369807",
          "image_url": "https://static.preisjaeger.at/x.jpg", "is_private": None, "published_at": "2026-10-12T10:00:00+00:00",
          "text": "Hülle mit Kantenschutz"}


class Unreachable(FakePlatform):
    async def detail(self, ad_id, *, url=None):
        raise PlatformError("HTTP 503")


@pytest.fixture
def client():
    from app.main import app
    deals._shown.clear()
    with TestClient(app) as c:
        yield c
    deals._shown.clear()
    platforms.reset()


def test_a_product_search_result_opens_without_a_hit(client):
    with session() as db:
        db.add(ProductSearch(description="Hülle", status="done", results=[RESULT]))
        db.commit()
    page = client.get("/hits/ad/preisjaeger/369807").text
    assert "EKE Case 2.0" in page and "19,92 €" in page and "Hülle mit Kantenschutz" in page
    assert "no longer online" not in page and "shows no details here" in page
    assert "no longer online" in client.get("/hits/ad/preisjaeger/111111").text      # nothing known: as before


def test_a_direct_search_result_and_an_unreachable_site(client):
    platforms.set_platform("fake", Unreachable())
    deals.remember_shown([{**RESULT, "platform": "fake", "ad_id": "77", "title": "Rennrad Carbon", "price_display": "900 €"}])
    page = client.get("/hits/ad/fake/77").text
    assert "Rennrad Carbon" in page and "900 €" in page and "could not be reached (HTTP 503)" in page
    assert "Fake error" in client.get("/hits/ad/fake/78").text                    # nothing to show: the error


def test_the_memory_keeps_the_latest():
    deals._shown.clear()
    deals.remember_shown([{"ad_id": str(i), "platform": "x", "title": str(i)} for i in range(deals.SHOWN_MAX + 5)])
    assert len(deals._shown) == deals.SHOWN_MAX and deals.shown_result("x", "0") is None
    assert deals.shown_result("x", str(deals.SHOWN_MAX + 4))["title"] == str(deals.SHOWN_MAX + 4)
    deals._shown.clear()
