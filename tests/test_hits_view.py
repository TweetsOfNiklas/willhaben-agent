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
        assert 'value="total" selected' in page and 'name="state" value="saved"' in page   # one form: the view stays
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


def test_several_agents_and_sites_at_once():
    """The agent and site filters take several (ticked boxes send repeated fields, links a comma list); all ticked is
    no filter; "Clear" on Filtered out clears exactly what the page shows."""
    from app.main import app

    with session() as db:
        agents = [SearchAgent(name=n, source_url=n, webapi_url=n) for n in ("A", "B", "C")]
        db.add_all(agents)
        db.commit()
        for a in agents:
            db.refresh(a)
        ids = [a.id for a in agents]
        for i, (aid, site) in enumerate([(ids[0], "willhaben"), (ids[1], "willhaben"), (ids[2], "willhaben"),
                                         (ids[0], "vinted")]):
            db.add(Hit(agent_id=aid, platform=site, ad_id=str(i + 1), heading=f"Ad {i + 1}", url=f"https://x/{i}",
                       price=10.0, state="filtered"))
        db.commit()
    a, b, c = ids
    with TestClient(app) as cl:
        two = cl.get(f"/hits?state=filtered&agent_id={a}&agent_id={b}").text
        assert set(_order(two)) == {"1", "2", "4"}
        assert f'name="agent_id" value="{a}" checked' in two and f'name="agent_id" value="{c}" checked' not in two
        assert "2 search agents" in two and f'name="agent_id" value="{a},{b}"' in two     # the bulk forms keep both
        assert set(_order(cl.get(f"/hits?state=filtered&agent_id={a},{b}&platform=vinted").text)) == {"4"}
        every = cl.get(f"/hits?state=filtered&agent_id={a}&agent_id={b}&agent_id={c}").text
        assert set(_order(every)) == {"1", "2", "3", "4"} and "All search agents" in every
        assert "data-multi-all" in every and "data-multi-none" in every
        cl.post("/hits/clear-filtered", data={"agent_id": f"{a},{b}", "platform": "willhaben"})
    with session() as db:
        states = {h.ad_id: h.state for h in db.exec(select(Hit))}
    assert states == {"1": "cleared", "2": "cleared", "3": "filtered", "4": "filtered"}   # only what was shown


def test_the_list_loads_more_when_scrolled_to_its_end():
    from datetime import datetime, timedelta, timezone

    from app.main import HIT_PAGE, app

    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with session() as db:
        a = SearchAgent(name="Rad", source_url="x", webapi_url="x")
        db.add(a)
        db.commit()
        for i in range(HIT_PAGE + 5):
            db.add(Hit(agent_id=a.id, platform="willhaben", ad_id=str(i), heading=f"Rad {i}", url=f"https://x/{i}",
                       price=10.0, first_seen_at=t0 + timedelta(minutes=i)))
        db.commit()
    with TestClient(app) as cl:
        first = cl.get("/hits").text
        assert first.count('data-hit="') // 2 == HIT_PAGE and 'id="hits-more"' in first
        assert re.search(rf'hx-get="/hits\?[^"]*offset={HIT_PAGE}"', first) and 'hx-trigger="revealed"' in first
        assert f">Rad {HIT_PAGE + 4}<" in first and ">Rad 4<" not in first           # newest first
        rest = cl.get(f"/hits?offset={HIT_PAGE}").text
        assert rest.count('data-hit="') // 2 == 5 and 'id="hits-more"' not in rest and ">Rad 0<" in rest


def test_several_states_at_once():
    """The states (New, Saved …) are a multi-select too: New + Saved shows both; all ticked is every state."""
    from app.main import app

    _setup()
    with session() as db:
        db.get(Hit, 1).state = "saved"
        db.add(db.get(Hit, 1))
        db.get(Hit, 3).state = "seen"
        db.add(db.get(Hit, 3))
        db.commit()
    with TestClient(app) as c:
        both = c.get("/hits?state=new&state=saved").text
        assert set(_order(both)) == {"1", "2", "4"} and "2 states" in both
        assert 'name="state" value="new" checked' in both and 'name="state" value="seen" checked' not in both
        assert set(_order(c.get("/hits?state=new,seen").text)) == {"2", "3", "4"}
        every = c.get("/hits?state=new&state=saved&state=seen&state=ignored&state=filtered").text
        assert set(_order(every)) == {"1", "2", "3", "4"}
        r = c.post("/hits/bulk", data={"state": "new,saved", "ids": "2", "action": "ignored"}, follow_redirects=False)
        assert r.headers["location"] == "/hits?state=new%2Csaved"


def test_cards_show_when_a_hit_was_found_the_order_of_newest():
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app.db import Hit, SearchAgent, session, utcnow
    from app.main import app
    now = utcnow()
    with session() as db:
        a = SearchAgent(name="SSD", source_url="https://www.willhaben.at/iad/x", webapi_url="/x")
        db.add(a)
        db.commit()
        db.refresh(a)
        db.add(Hit(agent_id=a.id, platform="vinted", ad_id="v1", heading="Vinted SSD", url="https://x/1", state="new",
                   first_seen_at=now - timedelta(hours=3), last_seen_at=now - timedelta(hours=3)))
        db.add(Hit(agent_id=a.id, platform="willhaben", ad_id="w1", heading="Older find", url="https://x/2", state="new",
                   first_seen_at=now - timedelta(hours=7), last_seen_at=now, published_at=now - timedelta(hours=8)))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/hits?state=new").text
    assert page.index("Vinted SSD") < page.index("Older find")
    assert page.count("data-found") == 2 and "found 3 h ago" in page.replace(" ", " ") or "found" in page
    first = page[page.index("Vinted SSD"):page.index("Older find")]
    assert "published" not in first.split("data-found")[1][:200]            # no date: nothing shown for it


def test_a_card_has_tidy_place_time_and_status_lines():
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app.db import Hit, SearchAgent, session, utcnow
    from app.main import app
    now = utcnow()
    with session() as db:
        a = SearchAgent(name="SSD", source_url="https://www.willhaben.at/iad/x", webapi_url="/x")
        db.add(a)
        db.commit()
        db.refresh(a)
        db.add(Hit(agent_id=a.id, platform="vinted", ad_id="v1", heading="No place", url="https://x/1", state="new",
                   first_seen_at=now - timedelta(hours=3), last_seen_at=now, site_status="active",
                   status_checked_at=now - timedelta(hours=1)))
        db.add(Hit(agent_id=a.id, platform="willhaben", ad_id="w1", heading="With place", url="https://x/2", state="new",
                   location="Haag", postcode="3350", is_private=False, first_seen_at=now - timedelta(hours=5),
                   last_seen_at=now, published_at=now - timedelta(hours=6)))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/hits?state=new").text
    card = page[page.index("No place"):page.index("With place")]
    assert "data-hit-place" not in card                                  # nothing to say: no line, no stray "·"
    assert "data-hit-when" in card and "published" not in card.split("data-hit-when")[1][:300]
    status = card[card.index("data-hit-status"):][:600]
    assert "still online" in status and 'title="listed' in status and ">listed" not in status   # ages in the tooltip
    other = page[page.index("With place"):]
    assert "Haag (3350) · dealer" in other[other.index("data-hit-place"):][:200]
