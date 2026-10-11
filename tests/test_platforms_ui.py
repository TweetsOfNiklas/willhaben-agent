"""Hits of other sites in the dashboard: refs in links and pushes, the viewer with and without ad details, the
price check, the Telegram buttons, the hits filter. willhaben's bare-id routes keep working as aliases."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.db import EventLog, Hit, SearchAgent, TelegramAction, log_event, session
from app.main import event_link
from app.services import notify as notify_mod
from app.services import telegram
from tests.fake_platform import FakePlatform, detail_of, listing


@pytest.fixture
def fake():
    ad = listing("77", "Kinderrad 16 Zoll", 60.0, body="Fast neu, Abholung in Berlin")
    p = FakePlatform(details={"77": detail_of(ad, "Fast neu, Abholung in Berlin. Beschreibung lang.")})
    platforms.set_platform("fake", p)
    with session() as db:
        wh = SearchAgent(name="Räder AT", source_url="https://www.willhaben.at/iad/x?keyword=rad", webapi_url="https://x")
        fk = SearchAgent(name="Räder DE", platform="fake", source_url="https://fake.example/s/rad", webapi_url="https://fake.example/s/rad",
                         spec={"platform": "fake", "url": "https://fake.example/s/rad", "keyword": "rad"})
        db.add(wh)
        db.add(fk)
        db.commit()
        db.refresh(wh)
        db.refresh(fk)
        db.add(Hit(agent_id=wh.id, ad_id="77", heading="Kinderrad AT", url="https://www.willhaben.at/iad/d/kinderrad-77/", price=70.0))
        db.add(Hit(agent_id=fk.id, platform="fake", ad_id="77", heading="Kinderrad 16 Zoll", url="https://fake.example/ad/77",
                   price=60.0, body="Fast neu", location="10115 Berlin", image_url="https://img.fake.example/1.jpg"))
        db.commit()
    yield p
    platforms.reset()


@pytest.fixture
def client(fake):
    from app.main import app
    with TestClient(app) as c:
        yield c


def _hit(platform: str) -> Hit:
    with session() as db:
        return db.exec(select(Hit).where(Hit.platform == platform)).one()


def test_hits_page_shows_sites_and_filters(client):
    page = client.get("/hits?state=all").text
    assert 'data-ad="fake:77"' in page and 'data-ad="77"' in page
    assert page.count("Fake") >= 1 and "willhaben" in page                  # pills, because two sites are watched
    only = client.get("/hits?state=all&platform=fake").text
    assert 'data-ad="fake:77"' in only and 'data-ad="77"' not in only
    assert platforms.multi() is True


def test_viewer_with_details_from_the_site(client, fake):
    h = _hit("fake")
    frag = client.get(f"/hits/ad/fake/77?hit={h.id}").text
    assert "Beschreibung lang" in frag and "Fake ↗" in frag and "https://fake.example/ad/77" in frag
    assert "/purchases/new/" not in frag                                      # buying is willhaben's
    assert 'href="/deals/ad/fake/77?hit=' in frag and 'hx-get="/hits/ad/fake/77/deal?hit=' in frag
    assert fake.detail_calls == ["77"] and _hit("fake").state == "seen"
    assert client.get("/hits/ad/nope/77").status_code == 404


def test_viewer_without_details_uses_the_hit(client, fake):
    fake.capabilities = frozenset({"search_url", "keyword_search"})
    h = _hit("fake")
    frag = client.get(f"/hits/ad/fake/77?hit={h.id}").text
    assert "Kinderrad 16 Zoll" in frag and "Fast neu" in frag and "shows no details here" in frag
    assert fake.detail_calls == []
    assert client.get("/hits/ad/fake/77").status_code == 200                 # no hit: "no longer online" text


def test_willhaben_aliases_and_route_order(client):
    import json

    import httpx

    from tests.conftest import load
    from app.willhaben import public as public_mod
    from app.willhaben.public import PublicClient, RateLimiter

    def handler(request: httpx.Request) -> httpx.Response:
        if "/webapi/ad-search/search" in request.url.path:
            return httpx.Response(200, json=load("search_fahrrad_rows3.json"))
        nd = load("detail_922008415_nextdata.json")
        return httpx.Response(200, text=f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>')

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    public_mod.set_public(c)
    try:
        frag = client.get("/hits/ad/77").text
        assert "willhaben ↗" in frag and "Buy" in frag
        assert client.get("/hits/ad/abc").status_code == 400
        # /hits/ad/77/deal must reach the price section, not the platform viewer with platform="77"
        r = client.get("/hits/ad/77/deal")
        assert r.status_code == 200 and "willhaben ↗" not in r.text
        r = client.get("/deals/ad/77/result")
        assert r.status_code == 200
        assert client.get("/deals/ad/fake/77").status_code == 200
        assert client.get("/deals/ad/nope/77").status_code == 404
    finally:
        public_mod.set_public(None)


def test_deals_search_box_understands_refs_and_links(client):
    r = client.get("/deals?q=kleinanzeigen:2712345678", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deals/ad/kleinanzeigen/2712345678"
    r = client.get("/deals?q=https://fake.example/ad/77", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deals/ad/fake/77"
    r = client.get("/deals?q=1000000001", follow_redirects=False)
    assert r.headers["location"] == "/deals/ad/1000000001"
    assert "not an ad on a supported site" in client.get("/deals?q=https://fake.example/s/rad").text


def test_viewer_deep_link_accepts_refs(client):
    page = client.get("/hits?state=all&view=fake:77").text
    assert "/^(?:[a-z][a-z0-9_]*:)?[\\w-]+$/" in page


async def test_push_links_and_inbox_use_refs(fake):
    assert notify_mod.app_path("hit", "https://fake.example/ad/77", "fake:77") == "/hits?view=fake:77"
    assert notify_mod.app_path("hit", "https://www.willhaben.at/iad/d/x-77/", "77") == "/hits?view=77"
    await notify_mod.notify("hit", "Räder DE: 60 €", "Kinderrad", url="https://fake.example/ad/77",
                            data={"agent": "Räder DE", "platform": "fake", "ad_id": "77", "ref": "fake:77"}, push=False)
    with session() as db:
        e = db.exec(select(EventLog).order_by(EventLog.id.desc())).first()
    assert e.ad_id == "fake:77"
    assert event_link(e) == {"href": "/hits?view=fake:77", "ad": "fake:77", "external": False}
    log_event("hit", "old push", "https://www.willhaben.at/iad/d/x-77/", ad_id="77")
    assert event_link(EventLog(kind="hit", message="x", ad_id="77"))["href"] == "/hits?view=77"


def test_telegram_keep_is_scoped_to_the_site(fake):
    kb = telegram.keyboard("hit", {"ad_id": "77", "ref": "fake:77"}, [], "https://fake.example/ad/77")
    tokens = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    with session() as db:
        refs = {db.get(TelegramAction, t[2:]).verb: db.get(TelegramAction, t[2:]).ref for t in tokens}
    assert refs == {"H_SAVE": "fake:77", "H_IGNORE": "fake:77"}
    assert telegram._set_hit_state("fake:77", "saved") == 1
    assert _hit("fake").state == "saved" and _hit("willhaben").state == "new"
    assert telegram._set_hit_state("77", "ignored") == 1                     # an old push: the willhaben hit
    assert _hit("willhaben").state == "ignored" and _hit("fake").state == "saved"
    assert telegram._set_hit_state("Bad:1", "saved") == 0


def test_api_hits_carry_platform_and_ref(client):
    rows = client.get("/api/hits?state=all").json()
    assert {(r["platform"], r["ref"]) for r in rows} == {("willhaben", "77"), ("fake", "fake:77")}
