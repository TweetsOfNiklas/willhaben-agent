"""Hits page: sort by price or price with shipping, filter by shipping and by price label; the bulk buttons act on
what the page shows."""
import re

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Hit, SearchAgent, session


def _setup():
    with session() as db:
        a = SearchAgent(name="Rad", source_url="x", webapi_url="x")
        db.add(a)
        db.commit()
        for ad_id, price, ship_price, ship, deal in (("1", 50.0, 20.0, "ship", "great"), ("2", 60.0, 0.0, "ship", "fair"),
                                                     ("3", 40.0, None, "pickup", "overpriced"), ("4", 55.0, None, None, None)):
            db.add(Hit(agent_id=a.id, platform="willhaben", ad_id=ad_id, heading=f"Ad {ad_id}", url=f"https://x/{ad_id}",
                       price=price, shipping_price=ship_price, shipping=ship, screen_relevant=deal is not None,
                       screen_deal=deal))
        db.commit()


def _order(page):
    return list(dict.fromkeys(re.findall(r">Ad (\d)<", page)))


def test_sort_and_filters():
    from app.main import app

    _setup()
    with TestClient(app) as c:
        assert _order(c.get("/hits?sort=price").text) == ["3", "1", "4", "2"]
        assert _order(c.get("/hits?sort=price_desc").text) == ["2", "4", "1", "3"]
        assert _order(c.get("/hits?sort=total").text) == ["3", "4", "2", "1"]   # 50 + 20 shipping is the dearest
        assert set(_order(c.get("/hits?ship=ship").text)) == {"1", "2"}
        assert _order(c.get("/hits?ship=pickup").text) == ["3"]
        assert _order(c.get("/hits?level=bargain").text) == ["1"]
        assert set(_order(c.get("/hits?level=fair").text)) == {"1", "2"}
        page = c.get("/hits?sort=total&ship=ship").text
        assert "/hits?state=saved&amp;sort=total&amp;ship=ship" in page       # the tabs keep the view
        r = c.post("/hits/mark-seen", data={"ship": "pickup", "sort": "total"}, follow_redirects=False)
        assert r.headers["location"] == "/hits?sort=total&ship=pickup"
    with session() as db:
        assert {h.ad_id: h.state for h in db.exec(select(Hit))} == {"1": "new", "2": "new", "3": "seen", "4": "new"}


def test_bulk_actions_on_the_ticked_hits():
    from app.main import app

    _setup()
    with session() as db:
        ids = {h.ad_id: h.id for h in db.exec(select(Hit))}
    with TestClient(app) as c:
        page = c.get("/hits").text
        assert 'id="hits-bulk"' in page and f'form="hits-bulk" name="ids" value="{ids["1"]}"' in page
        r = c.post("/hits/bulk", data={"ids": [ids["1"], ids["2"]], "action": "saved", "sort": "price"},
                   follow_redirects=False)
        assert r.headers["location"] == "/hits?state=new&sort=price"
        c.post("/hits/bulk", data={"ids": [ids["3"]], "action": "ignored"})
        c.post("/hits/bulk", data={"ids": [ids["1"], ids["4"]], "action": "clear"})     # the saved one stays
        c.post("/hits/bulk", data={"ids": [ids["2"]], "action": "bogus"})
    with session() as db:
        assert {h.ad_id: h.state for h in db.exec(select(Hit))} == {"1": "saved", "2": "saved", "3": "ignored",
                                                                    "4": "cleared"}


def test_shipping_with_buyer_protection():
    from app.main import app

    with session() as db:
        a = SearchAgent(name="Rad", source_url="x", webapi_url="x")
        db.add(a)
        db.commit()
        for ad_id, platform, ship_price, ship, pl in (
                ("1", "willhaben", None, "ship", True),          # PayLivery on (the list's p2penabled)
                ("2", "kleinanzeigen", 5.0, "ship", None),       # no buyer protection visible
                ("3", "vinted", None, None, None),               # Vinted: always
                ("4", "ebay", 4.9, None, None),
                ("5", "willhaben", None, "pickup", None),
                ("6", "facebook", None, "ship", None),
                ("7", "willhaben", None, "ship", False),         # handover "Versand", PayLivery off
                ("8", "willhaben", None, "ship", None),          # from before the flag, no PayLivery price: unknown
                ("9", "willhaben", 3.49, "ship", None)):         # from before the flag, PayLivery's price
            db.add(Hit(agent_id=a.id, platform=platform, ad_id=ad_id, heading=f"Ad {ad_id}", url=f"https://x/{ad_id}",
                       price=10.0, shipping_price=ship_price, shipping=ship, paylivery=pl))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/hits?ship=protected").text
        assert set(_order(page)) == {"1", "3", "4", "9"}
        assert 'value="protected" selected' in page


def test_newest_hits_first_and_no_negotiable_kleinanzeigen_ads():
    from datetime import datetime, timedelta, timezone

    from app.main import app

    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with session() as db:
        a = SearchAgent(name="Rad", source_url="x", webapi_url="x")
        db.add(a)
        db.commit()
        for ad_id, platform, display, found, published in (
                ("1", "willhaben", "€ 10", 1, 9), ("2", "willhaben", "€ 10", 3, 1), ("3", "kleinanzeigen", "10 €", 2, 5),
                ("4", "kleinanzeigen", "VB", 4, 4), ("5", "kleinanzeigen", "120 € VB", 5, 3)):
            db.add(Hit(agent_id=a.id, platform=platform, ad_id=ad_id, heading=f"Ad {ad_id}", url=f"https://x/{ad_id}",
                       price=10.0, price_display=display, first_seen_at=t0 + timedelta(days=found),
                       published_at=t0 + timedelta(days=published)))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/hits").text
        assert _order(page) == ["2", "3", "1"]                       # found last first; VB ads not shown
        assert 'value="newest" selected' in page                   # the default
        assert _order(c.get("/hits?sort=oldest").text) == ["1", "3", "2"]
        assert _order(c.get("/hits?sort=published").text) == ["1", "3", "2"]
        assert _order(c.get("/hits?state=all").text) == ["2", "3", "1"]


def test_the_paylivery_flag_comes_from_the_list_and_the_ad_page():
    from types import SimpleNamespace

    from app.platforms.shipping_info import apply_shipping
    hit = Hit(agent_id=1, ad_id="1", heading="x", url="u", platform="willhaben")
    apply_shipping(hit, SimpleNamespace(shipping_price=None, shipping="ship", paylivery=False))
    assert hit.paylivery is False and hit.shipping == "ship"
    page = SimpleNamespace(platform="willhaben", shipping_price=3.49, shipping="ship", paylivery_enabled=True,
                           shipping_options=[{"name": "Post", "home": 5.7, "point": 3.49}])
    apply_shipping(hit, page)
    assert hit.paylivery is True
    apply_shipping(hit, SimpleNamespace(shipping_price=None, shipping=None))      # says nothing: kept
    assert hit.paylivery is True
