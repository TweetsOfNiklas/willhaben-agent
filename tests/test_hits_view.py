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
