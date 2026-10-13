"""ebay.at through its result pages: the cards, the links, the request, the bot check's warm-up and its blocks."""
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import select

from app import platforms
from app.db import Hit, session, set_setting
from app.platforms import Blocked, InvalidSearchUrl, PlatformError
from app.platforms import ebay
from app.platforms.ratelimit import RateLimiter
from app.services.search_agents import create_agent, run_agent
from tests.conftest import FIXTURES
from tests.test_kleinanzeigen import Queue
from tests.test_platforms_contract import EBAY_ROUTES, FileTransport

PAGE = (FIXTURES / "ebay_search.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 13, 21, 10, tzinfo=ebay.VIENNA)
IDS = ["407279207688", "237116368492", "920023027672", "178553946444", "287646086506"]


@pytest.fixture
def client():
    c = ebay.EbayClient(transport=FileTransport(EBAY_ROUTES))
    c.limiter = RateLimiter(0, name="eBay")
    ebay.set_client(c)
    yield c
    ebay.set_client(None)


@pytest.fixture
def platform(client):
    platforms.reset()
    set_setting("platform_ebay_enabled", "1")
    yield platforms.get("ebay")
    platforms.reset()


def test_result_cards_become_listings():
    page = ebay.parse_search(PAGE, NOW)
    assert [a.ad_id for a in page.ads] == IDS and page.rows_found == 129    # placeholder card and auctions: out
    first, shop, offer, _, abroad = page.ads
    assert first.heading == "Apple iPhone 13 – Top Zustand – 100 % Akkukapazität" and first.ref == f"ebay:{IDS[0]}"
    assert first.price == 229.99 and first.price_display == "229,99 €" and first.is_private is True
    assert first.published_at.isoformat() == "2026-10-09T23:33:00+02:00" and first.location is None
    assert first.url == f"https://www.ebay.at/itm/{IDS[0]}" and first.image_url.endswith("/s-l500.webp")
    assert first.body == "Refurbished · +EUR 6,99 Versand · Seller verkaeufer-1, 100% positiv (1)"
    assert shop.is_private is False and "Kostenloser Versand" in shop.body
    assert offer.body.startswith("Gebraucht · oder Preisvorschlag · Kostenlose Abholung")
    assert abroad.heading == "LEGO TECHNIC Liebherr Bagger R 9800 (42100)" and abroad.location == "Deutschland"


def test_no_results_and_fewer_words():
    none = ebay.parse_search((FIXTURES / "ebay_none.html").read_text(encoding="utf-8"), NOW)
    assert none.rows_found == 0 and none.ads == []                          # "Keine exakten Treffer gefunden"
    cut = PAGE.index(f"data-listingid={IDS[2]}")
    cut = PAGE.rindex("<li", 0, cut)
    rewritten = PAGE[:cut] + '<li class="srp-river-answer srp-river-answer--REWRITE_START">Weniger Suchbegriffe</li>' + PAGE[cut:]
    assert [a.ad_id for a in ebay.parse_search(rewritten, NOW).ads] == IDS[:2]
    with pytest.raises(PlatformError, match="without a result list"):
        ebay.parse_search("<html><body>Pardon Our Interruption</body></html>")


def test_listing_time_has_no_year():
    assert ebay.listed_at("Oct-9 23:33", NOW).isoformat() == "2026-10-09T23:33:00+02:00"
    assert ebay.listed_at("Okt-9 7:05", NOW).isoformat() == "2026-10-09T07:05:00+02:00"
    assert ebay.listed_at("Dec-30 10:00", NOW).year == 2025                  # later than today: last year
    assert ebay.listed_at("Feb-30 10:00", NOW) is None and ebay.listed_at("vor 3 Tagen", NOW) is None


def test_links_and_ids():
    p = ebay.EbayPlatform()
    spec = p.parse_search_url("https://www.ebay.at/sch/i.html?_nkw=rennrad+carbon&_udhi=900&_pgn=2&_ipg=240&rt=nc")
    assert spec.keyword == "rennrad carbon" and spec.price_to == 900 and spec.extra == {}
    assert spec.url == "https://www.ebay.at/sch/i.html?_nkw=rennrad+carbon&_sacat=0&_sop=10&LH_BIN=1&LH_PrefLoc=1&_udhi=900"
    wide = p.parse_search_url("https://www.ebay.at/sch/Handys/9355/i.html?_nkw=iphone&LH_PrefLoc=3&LH_ItemCondition=3000"
                              "&LH_Auction=1&_sop=15")
    assert wide.category == "9355" and wide.sort == "price_asc"
    assert wide.extra == {"LH_ItemCondition": "3000", "LH_PrefLoc": "3"}       # a link may widen the place
    assert "LH_BIN=1" in wide.url and "LH_Auction" not in wide.url            # never auctions
    assert p.parse_search_url("https://www.ebay.at/sch/i.html?_sacat=9355").keyword is None
    for bad in ("https://www.ebay.at/itm/287646086506", "https://www.ebay.de/sch/i.html?_nkw=x",
                "https://www.ebay.at/sch/i.html?_nkw=", "https://www.ebay.at/sch/i.html?_nkw=x&_udlo=abc",
                "https://www.ebay.at/b/Handys/9355"):
        with pytest.raises(InvalidSearchUrl):
            p.parse_search_url(bad)
    assert p.parse_ad_url("https://www.ebay.at/itm/LEGO-Technic-42100/287646086506?hash=x") == "287646086506"
    assert p.extract_ad_id("287646086506") == "287646086506" and p.ad_url("1") == "https://www.ebay.at/itm/1"
    assert ebay.classify(spec.url) == "search" and ebay.classify(p.ad_url("1")) == "detail"
    from app.services import ad_images
    photo = "https://i.ebayimg.com/images/g/qvYAAeSwmhhqy9Wh/s-l500.webp"   # Kleinanzeigen's photos live there too
    assert ad_images._site(photo, "ebay").id == "ebay" and ad_images._site(photo, "kleinanzeigen").id == "kleinanzeigen"
    assert ad_images._site(photo, None).id in ("kleinanzeigen", "ebay")


async def test_search_is_one_page_with_the_fixed_filters(platform, client):
    page = await platform.search(platform.keyword_search("iphone 13", 100, 500), rows=3)
    assert len(page.ads) == 3 and page.title == "iphone 13"
    url, headers = client._transport.calls[-1]
    qs = parse_qs(urlsplit(url).query)
    assert url.startswith("https://www.ebay.at/sch/i.html?") and headers["Referer"] == "https://www.ebay.at/"
    assert {k: v[0] for k, v in qs.items()} == {"_nkw": "iphone 13", "_sacat": "0", "_sop": "10", "LH_BIN": "1",
                                                "LH_PrefLoc": "1", "_udlo": "100", "_udhi": "500", "_ipg": "60"}


class CookieQueue(Queue):
    """Canned answers; counts how often the client throws its visitor away."""
    cleared = 0

    def clear_cookies(self):
        self.cleared += 1


async def test_first_403s_are_the_bot_checks_warm_up():
    t = CookieQueue([(403, b"<html>Error Page | eBay</html>"), (403, b""), (200, PAGE.encode()), (403, b"")])
    c = ebay.EbayClient(transport=t)
    c.limiter = RateLimiter(0, name="eBay")
    page = await c.search_page({"_nkw": "iphone 13"})
    assert len(page.ads) == 5 and len(t.calls) == 3 and t.cleared == 0     # same visitor, its cookies kept
    with pytest.raises(Blocked):                                           # warm now: a 403 is a block
        await c.search_page({"_nkw": "iphone 14"})
    assert t.cleared == 1 and len(t.calls) == 4


async def test_warm_up_gives_up_and_errors():
    t = CookieQueue([(403, b""), (403, b""), (403, b"")])
    c = ebay.EbayClient(transport=t)
    c.limiter = RateLimiter(0, name="eBay")
    with pytest.raises(Blocked):
        await c.search_page({"_nkw": "x"})
    assert len(t.calls) == 1 + ebay.WARMUP_RETRIES and t.cleared == 1
    c = ebay.EbayClient(transport=CookieQueue([(500, b"")]))
    c.limiter = RateLimiter(0, name="eBay")
    with pytest.raises(PlatformError, match="HTTP 500"):
        await c.search_page({"_nkw": "x"})


async def test_agent_end_to_end(platform, client):
    a = create_agent("https://www.ebay.at/sch/i.html?_nkw=iphone+13&LH_PrefLoc=1&LH_BIN=1&_sop=10")
    assert a.platform == "ebay" and a.name == "iphone 13" and a.interval_s >= 300
    res = await run_agent(a.id)
    assert res.baseline and res.error is None
    with session() as db:
        hits = list(db.exec(select(Hit).where(Hit.agent_id == a.id)))
    assert sorted(h.ad_id for h in hits) == sorted(IDS) and {h.platform for h in hits} == {"ebay"}


# ------------------------------------------------------------------ the item page
ITEM, SHOP = "920036165580", "168768204140"
ITEM_PAGE = (FIXTURES / "ebay_item.html").read_text(encoding="utf-8")
SHOP_PAGE = (FIXTURES / "ebay_item_shop.html").read_text(encoding="utf-8")
DESC_PAGE = (FIXTURES / "ebay_desc.html").read_text(encoding="utf-8")


def test_item_pages_become_details():
    desc = ebay.parse_description(DESC_PAGE)
    assert desc.startswith("Verkaufe ein gut erhaltenes Maxi-Cosi Kinderwagen-Set in Braun.\n\nDas Set besteht aus:")
    d = ebay.parse_item_page(ITEM_PAGE, ITEM, desc)
    assert d.heading.startswith("Maxi-Cosi Mura 4 Kinderwagen Set") and d.price == 200 and d.price_display == "200 €"
    assert d.place == d.location == "Thalgau, Österreich" and d.is_private is True and d.seller_name == "verkaeufer-a"
    assert d.status == "active" and d.published_at.isoformat() == "2026-10-13T15:13:03+00:00" and d.ref == f"ebay:{ITEM}"
    assert len(d.images) == 4 and all(u.endswith("/s-l1600.jpg") for u in d.images) and len(set(d.images)) == 4
    assert d.category_path == ["Baby", "Kinderwagen & Zubehör", "Kinderwagen"]
    assert d.description.startswith(desc) and "Condition: Gebraucht" in d.description and "Pickup possible" in d.description
    assert "1170" not in d.description                                    # the visitor's postcode, not the item's

    s = ebay.parse_item_page(SHOP_PAGE, SHOP)
    assert s.heading == "Lenovo ThinkPad x1 Carbon Gen 8 | i7 16GB Touch |QWERTY|GARANTIE&RECHNUNG"   # <wbr> joined
    assert s.price == 384 and s.place == "Wien, Österreich" and s.is_private is False and s.seller_name == "Beispiel Laptops GmbH"
    assert "Top Zustand" in s.description and "Marke: Lenovo" in s.description and "Artikelzustand" not in s.description
    assert "EUR 30,00" in s.description and "48,00" not in s.description   # shipping yes, the unit price no


def test_item_page_traps():
    with pytest.raises(PlatformError, match="without the item page"):
        ebay.parse_item_page("<html><title>Error Page | eBay</title>SORRY</html>", ITEM)
    assert ebay.parse_item_page(ITEM_PAGE, "111111111111") is None           # eBay showed another item
    ended = ebay.parse_item_page(ITEM_PAGE.replace('"listingStatus":"ACTIVE"', '"listingStatus":"ENDED"'), ITEM)
    assert ended.status == "expired"
    assert ebay.parse_description("<html><body>nothing</body></html>") is None


async def test_detail_loads_the_page_and_the_description(platform, client):
    d = await platform.detail(ITEM)
    assert d.place == "Thalgau, Österreich" and d.description.startswith("Verkaufe ein gut erhaltenes")
    urls = [u for u, _ in client._transport.calls]
    assert urls[0] == f"https://www.ebay.at/itm/{ITEM}" and urls[1].startswith(f"https://itm.ebaydesc.com/itmdesc/{ITEM}?")
    assert await platform.detail("123456789012") is None                     # 404


async def test_item_page_after_the_warm_up():
    t = CookieQueue([(403, b""), (200, ITEM_PAGE.encode()), (500, b"")])
    c = ebay.EbayClient(transport=t)
    c.limiter = RateLimiter(0, name="eBay")
    d = await c.item(ITEM)
    assert d.heading.startswith("Maxi-Cosi") and d.description and "Verkaufe" not in d.description   # no description page
    assert len(t.calls) == 3 and t.cleared == 0


def test_viewer_shows_the_item(platform):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        page = c.get(f"/hits/ad/ebay/{ITEM}").text
    assert "Thalgau, Österreich" in page and "Maxi-Cosi Mura 4" in page and "Verkaufe ein gut erhaltenes" in page
    assert "no longer online" not in page and "shows no details here" not in page
