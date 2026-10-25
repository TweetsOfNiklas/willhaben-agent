"""Produktsuche: the Geizhals new price of every offer after the ranking (the price check's lookup, one per product
name), the discount on it, and the order by discount on the dashboard and in the Telegram Mini App."""
import pytest
from fastapi.testclient import TestClient

from app.db import ProductSearch, session, utcnow
from app.services import deals, geizhals
from app.services import product_search as ps

GH = {"bosch sms46": {"name": "Bosch Serie 4 SMS46", "url": "https://geizhals.at/a1", "price_from": 400.0},
      "raspberry pi 5 8gb": {"name": "Raspberry Pi 5 8GB", "url": "https://geizhals.at/a2", "price_from": 80.0}}


@pytest.fixture
def lookups(monkeypatch):
    asked = []

    async def match(query, raise_errors=False):
        asked.append(query)
        if query == "fail":
            raise geizhals.GeizhalsError("blocked")
        return GH.get(query.lower())
    monkeypatch.setattr(deals, "geizhals_match", match)
    monkeypatch.setattr(deals, "stored_geizhals", lambda ref: None)
    return asked


def _row(results, status="prices"):
    with session() as db:
        row = ProductSearch(description="Geschirrspüler", status=status, settings={"platforms": ["willhaben"]},
                            plan={"queries": [{"keyword": "Geschirrspüler"}]}, results=results,
                            requests=1, candidates=len(results), started_at=utcnow())
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _r(ad_id, title, price, fit, **kw):
    return {"ad_id": ad_id, "platform": "willhaben", "ref": ad_id, "title": title, "price": price, "fit": fit,
            "url": f"https://x/{ad_id}", **kw}


async def test_one_lookup_per_product_name_and_the_discount(lookups):
    results = [_r("1", "Bosch SMS46 Geschirrspüler", 300.0, 90), _r("2", "Bosch SMS46 neuwertig", 100.0, 80),
               _r("3", "Siemens Geschirrspüler", 150.0, 70), _r("4", "Raspberry Pi 5 8GB", 100.0, 60),
               _r("5", "schon gefragt", 10.0, 50, geizhals={})]
    sid = _row(results)
    await ps.geizhals_prices(sid, results)
    assert lookups == ["Bosch SMS46", "Raspberry Pi 5 8GB"]                   # no product name, asked before: none
    assert [ps.discount(r) for r in results] == [25, 75, None, -25, None]
    assert results[2]["geizhals"] == {} and results[3]["geizhals"]["url"] == "https://geizhals.at/a2"
    row = ps.get(sid)
    assert row.results[0]["geizhals"]["price_from"] == 400.0 and "prices" not in row.progress


async def test_a_geizhals_failure_leaves_the_rest_not_asked(lookups, monkeypatch):
    monkeypatch.setattr(ps.deals, "geizhals_query", lambda title: title)
    results = [_r("1", "fail", 10.0, 90), _r("2", "bosch sms46", 300.0, 80)]
    await ps.geizhals_prices(_row(results), results)
    assert lookups == ["fail"] and all("geizhals" not in r for r in results)


async def test_lookups_are_capped_best_offers_first(lookups, monkeypatch):
    monkeypatch.setattr(ps, "MAX_NEW_PRICES", 2)
    monkeypatch.setattr(ps.deals, "geizhals_query", lambda title: title)
    results = [_r(str(i), f"p{i}", 10.0, 90 - i) for i in range(4)]
    await ps.geizhals_prices(_row(results), results)
    assert lookups == ["p0", "p1"] and "geizhals" not in results[2]


def test_the_lists_sort_by_discount(lookups):
    from app.main import app
    results = [_r("1", "Wenig Rabatt", 300.0, 90, geizhals=GH["bosch sms46"]),
               _r("2", "Ohne Neupreis", 50.0, 80, geizhals={}),
               _r("3", "Viel Rabatt", 100.0, 70, geizhals=GH["bosch sms46"])]
    sid = _row(results, status="done")
    with TestClient(app) as c:
        web = c.get(f"/deals/search/{sid}").text.replace("\u00a0", " ").replace("&nbsp;", " ")
        assert "sort('discount')" in web and 'data-off="-25"' in web and 'data-off="-75"' in web and 'data-off=""' in web
        assert "25 % below the new price" in web and 'href="https://geizhals.at/a1"' in web
        tg = c.get(f"/tg/search/{sid}?order=discount").text
        assert tg.index("Viel Rabatt") < tg.index("Wenig Rabatt") < tg.index("Ohne Neupreis")
        assert "75 % below the new price" in tg
        c.get(f"/tg/search/{sid}?order=fit")


def test_the_price_step_shows_its_progress(lookups):
    from app.main import app
    sid = _row([_r("1", "Bosch SMS46", 300.0, 90)])
    ps._active.add(sid)
    try:
        ps._update(sid, progress={"prices": {"done": 2, "total": 5}})
        with TestClient(app) as c:
            ps._active.add(sid)                         # the app's startup ends running searches
            ps._update(sid, status="prices")
            assert "new prices of the offers: 2 of 5 products" in c.get(f"/deals/search/{sid}").text
            assert "new prices of the offers: 2 of 5 products" in c.get(f"/tg/search/{sid}").text
    finally:
        ps._active.discard(sid)


def test_an_interrupted_price_step_keeps_the_offers():
    sid = _row([_r("1", "Bosch SMS46", 300.0, 90)])
    ps.recover()
    row = ps.get(sid)
    assert row.status == "done" and row.error is None


async def test_an_older_search_looks_its_new_prices_up_when_opened(lookups):
    """A search stored before the step (no "geizhals" on its offers): opening it looks them up in the background."""
    from tests.test_product_search import _finish
    results = [_r("1", "Bosch SMS46 Geschirrspüler", 300.0, 90), _r("2", "Bosch SMS46", 100.0, 80)]
    sid = _row(results, status="done")
    assert ps.fill_new_prices(sid) is True and ps.get(sid).status == "prices" and ps.running().id == sid
    assert ps.fill_new_prices(sid) is False                                   # already on it
    await _finish()
    row = ps.get(sid)
    assert row.status == "done" and [ps.discount(r) for r in row.results] == [25, 75] and ps.running() is None
    assert ps.fill_new_prices(sid) is False and lookups == ["Bosch SMS46"]   # all asked: nothing more


def test_nothing_priced_means_no_discount_order(lookups):
    from app.main import app
    sid = _row([_r("1", "Billig", 50.0, 90, geizhals={}), _r("2", "Teuer", 90.0, 70, geizhals={})], status="done")
    with TestClient(app) as c:
        web = c.get(f"/deals/search/{sid}").text
        assert "priced: false" in web and "Geizhals knows the new price of none" in web
        tg = c.get(f"/tg/search/{sid}?order=discount").text
        assert 'aria-disabled="true"' in tg and tg.index("Billig") < tg.index("Teuer")
        c.get(f"/tg/search/{sid}?order=fit")
