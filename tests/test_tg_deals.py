"""The Telegram Mini App's price check and product search (/tg/search, /tg/check) and the hits page's filters and
bulk actions, posted to the dashboard's own endpoints (app/tg_bridge.py)."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Hit, ProductSearch, SearchAgent, session, set_setting, utcnow


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _agents():
    with session() as db:
        a = SearchAgent(name="Raspberry Pi 5", source_url="https://www.willhaben.at/iad/x", webapi_url="/x")
        b = SearchAgent(name="UniFi", source_url="https://www.willhaben.at/iad/y", webapi_url="/y")
        db.add(a)
        db.add(b)
        db.commit()
        db.refresh(a)
        db.refresh(b)
        for n, agent in ((1, a), (2, a), (3, b)):
            db.add(Hit(agent_id=agent.id, ad_id=str(n), heading=f"Ad {n} of {agent.name}", url=f"https://x/{n}",
                       state="new", price=50.0 + n, first_seen_at=utcnow(), last_seen_at=utcnow()))
        db.commit()
        return a.id, b.id


def _states():
    with session() as db:
        return {h.ad_id: h.state for h in db.exec(select(Hit))}


def test_hits_filter_by_agent_and_act_on_several_or_all(client):
    """The agent filter shows that agent's hits; ticked hits are saved at once and "mark all as seen" takes exactly
    what the view shows – both through the dashboard's endpoints, back to the same view."""
    a, _ = _agents()
    page = client.get(f"/tg/hits?agent_id={a}").text
    assert "Ad 1 of Raspberry Pi 5" in page and "Ad 3 of UniFi" not in page
    assert 'action="/hits/bulk"' in page and 'action="/hits/mark-seen"' in page and "data-hit-filters" in page
    with session() as db:
        ids = [h.id for h in db.exec(select(Hit).where(Hit.ad_id == "1"))]
    r = client.post("/hits/bulk", data={"ids": ids, "action": "saved", "state": "new", "agent_id": str(a)},
                    headers={"X-TG-Back": f"/tg/hits?agent_id={a}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/tg/hits")
    assert _states()["1"] == "saved"
    r = client.post("/hits/mark-seen", data={"agent_id": str(a)}, headers={"X-TG-Back": f"/tg/hits?agent_id={a}"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/tg/hits")
    assert _states() == {"1": "saved", "2": "seen", "3": "new"}                 # the other agent's hit stays new
    held = client.get("/tg/hits?state=filtered").text
    assert 'action="/hits/clear-filtered"' not in held                           # nothing held back: no button


def test_a_hit_links_to_the_full_price_check_and_to_buying(client, monkeypatch):
    from app import platforms
    from app.services import purchase
    _agents()
    set_setting(purchase.SETTING_ENABLED, "1")

    async def none(*a, **k):
        return None
    monkeypatch.setattr(platforms.get("willhaben"), "detail", none)
    with session() as db:
        h = db.exec(select(Hit).where(Hit.ad_id == "1")).one()
    page = client.get(f"/tg/hits/{h.id}").text
    assert f'href="/tg/check/willhaben/1?hit={h.id}"' in page and 'href="/tg/purchases/new/1"' in page


def test_the_search_page_its_redirects_and_a_product_search(client):
    """One page for both searches; an ad link goes to its price check, the dashboard's redirect after starting a
    search goes to that search, its short codes become their text."""
    page = client.get("/tg/search").text
    assert 'action="/deals/search"' in page and 'name="description"' in page
    assert 'name="q"' in client.get("/tg/search?mode=direct").text
    r = client.get("/tg/search?q=https://www.willhaben.at/iad/kaufen-und-verkaufen/d/x-123456789/",
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tg/check/willhaben/123456789"
    r = client.get("/tg/search?search=7&msg=busy", follow_redirects=False)
    assert r.headers["location"].startswith("/tg/search/7?msg=") and "busy" not in r.headers["location"]
    with session() as db:
        ps = ProductSearch(description="ein leiser Geschirrspüler", status="done", settings={"depth": "normal"},
                           plan={"summary": "Geschirrspüler bis 300 €", "queries": [{"keyword": "Geschirrspüler"}]},
                           results=[{"ad_id": "555", "platform": "willhaben", "title": "Bosch Geschirrspüler",
                                     "price": 180.0, "url": "https://x/555", "fit": 82, "reason": "Leise."}],
                           requests=3, candidates=20, started_at=utcnow())
        db.add(ps)
        db.commit()
        db.refresh(ps)
        asking = ProductSearch(description="Fahrrad", status="asking", settings={},
                               plan={"questions": [{"id": "size", "text": "Welche Größe?", "options": ["M", "L"]}]})
        db.add(asking)
        db.commit()
        db.refresh(asking)
    res = client.get(f"/tg/search/{ps.id}").text
    assert "Bosch Geschirrspüler" in res and 'href="/tg/check/willhaben/555?q=' in res
    assert f'action="/deals/search/{ps.id}/agent"' in res and "Geschirrspüler bis 300 €" in res
    ask = client.get(f"/tg/search/{asking.id}").text
    assert 'name="a_size"' in ask and 'name="o_size"' in ask and f'action="/deals/search/{asking.id}/answers"' in ask
    assert "Fahrrad" in client.get("/tg/search").text                              # among the recent searches


def _analysis_result():
    from app.services import deals
    comps = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0]
    st = deals.stats(comps)
    return {"ad": {"ad_id": "123456789", "ref": "123456789", "platform": "willhaben", "title": "Raspberry Pi 5 8GB",
                   "price": 90.0, "url": "https://x/1", "image": None, "location": "Wien"},
            "query": "Raspberry Pi 5", "asked_query": None, "own_query": False, "category_url": None,
            "comparables": {"stats": st, "count": len(comps),
                            "closest": [{"ad_id": str(i), "title": f"Pi {i}", "price": p, "url": f"https://x/c{i}"}
                                        for i, p in enumerate(comps)]},
            "geizhals": None, "position": deals.position(90.0, st, None, None), "claude": None,
            "checked_at": utcnow().isoformat()}


def test_the_price_check_of_any_ad_and_claudes_verdict_in_the_background(client, monkeypatch):
    """The page answers at once and loads the analysis; "Ask Claude" runs on its own, the section shows it running
    and asks again until the verdict is there."""
    from app import deals_web, tg_deals
    from app.services import deals

    async def analysis(*a, **k):
        return _analysis_result(), None

    async def no_choices(*a, **k):
        return None
    monkeypatch.setattr(deals_web, "_analysis", analysis)
    monkeypatch.setattr(deals, "category_choices", no_choices)
    page = client.get("/tg/check/willhaben/123456789").text
    assert 'hx-get="/tg/check/willhaben/123456789/result"' in page
    part = client.get("/tg/check/willhaben/123456789/result").text
    assert "Raspberry Pi 5 8GB" in part and "data-pricebar" in part and "data-comparables" in part
    assert 'action="/tg/check/willhaben/123456789/claude"' in part and "every 3s" not in part

    gate = asyncio.Event()

    async def claude(*a, **k):
        await gate.wait()
    monkeypatch.setattr(deals_web, "deal_claude_platform", claude)
    r = client.post("/tg/check/willhaben/123456789/claude", data={"q": "", "hit": "", "cat": "", "own": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tg/check/willhaben/123456789"
    part = client.get("/tg/check/willhaben/123456789/result").text
    assert 'hx-trigger="every 3s"' in part and 'hx-disinherit="*"' in part and "data-ask-claude" not in part
    tg_deals._running.pop(("willhaben", "123456789", "claude")).cancel()
    assert "data-ask-claude" in client.get("/tg/check/willhaben/123456789/result").text
