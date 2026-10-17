"""Hits: the Clear button of Filtered out and the retention (all hits but saved ones). A cleared hit keeps a bare row, shown nowhere, so a
search that still lists the ad does not find it new; the row goes once no search has seen the ad for GONE_DAYS."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Hit, SearchAgent, session, utcnow
from app.services import search_agents as sa


def _hit(agent_id, ad_id, state="filtered", age=0, unseen=0):
    with session() as db:
        db.add(Hit(agent_id=agent_id, platform="willhaben", ad_id=ad_id, heading=f"Ad {ad_id}", body="text",
                   url=f"https://example.invalid/{ad_id}", state=state, screen_reason="why",
                   first_seen_at=utcnow() - timedelta(days=age), last_seen_at=utcnow() - timedelta(days=unseen)))
        db.commit()


def _agents():
    with session() as db:
        ids = []
        for n in ("a", "b"):
            a = SearchAgent(name=n, source_url="x", webapi_url="x")
            db.add(a)
            db.commit()
            ids.append(a.id)
    return ids


def _states():
    with session() as db:
        return {h.ad_id: h.state for h in db.exec(select(Hit))}


def test_clear_and_retention():
    a, b = _agents()
    _hit(a, "1", age=40), _hit(a, "2", age=5), _hit(b, "3", age=40), _hit(a, "4", "new", age=40)
    _hit(a, "6", "saved", age=400), _hit(a, "7", "seen", age=40), _hit(a, "8", "ignored", age=31)
    assert sa.keep_days() == sa.KEEP_DAYS_DEFAULT == 30
    assert sa.clear_hits(sa.EXPIRING, older_than_days=30) == 5
    assert _states() == {"1": "cleared", "2": "filtered", "3": "cleared", "4": "cleared", "6": "saved",
                         "7": "cleared", "8": "cleared"}
    assert sa.clear_hits(("saved",)) == 0 and _states()["6"] == "saved"         # never saved ones
    with session() as db:
        h = db.exec(select(Hit).where(Hit.ad_id == "1")).one()
        assert h.body is None and h.screen_reason is None
    assert sa.clear_hits(agent_id=b) == 0 and sa.clear_hits(agent_id=a) == 1 and _states()["2"] == "cleared"
    assert sa.prune_cleared() == 0                    # still listed by the searches: the rows stay
    _hit(a, "5", "cleared", unseen=sa.GONE_DAYS + 1)
    assert sa.prune_cleared() == 1 and "5" not in _states()
    assert sa.set_keep_days(0) == 0 and sa.keep_days() == 0


def test_the_filtered_only_setting_carries_over():
    from app.db import set_setting

    set_setting(sa.OLD_KEEP_DAYS_KEY, "7")
    assert sa.keep_days() == 7
    sa.set_keep_days(14)
    assert sa.keep_days() == 14


async def test_the_job_follows_the_setting():
    a, _ = _agents()
    _hit(a, "1", age=10), _hit(a, "2", "seen", age=10), _hit(a, "3", "saved", age=10)
    await sa.retention_job()
    assert set(_states().values()) == {"filtered", "seen", "saved"}
    sa.set_keep_days(7)
    await sa.retention_job()
    assert _states() == {"1": "cleared", "2": "cleared", "3": "saved"}


def test_the_page():
    from app.main import app

    a, b = _agents()
    _hit(a, "1"), _hit(b, "2"), _hit(a, "3", "cleared"), _hit(a, "4", "seen")
    with TestClient(app) as c:
        page = c.get("/hits?state=filtered").text
        assert 'action="/hits/clear-filtered"' in page and 'name="days"' in page and "after 30 days" in page
        page = c.get("/hits?state=saved").text
        assert 'action="/hits/clear-filtered"' not in page and "saved ones are kept" in page
        all_page = c.get("/hits?state=all").text
        assert "Ad 3" not in all_page and "Ad 4" in all_page          # cleared: shown nowhere
        c.post("/hits/keep-days", data={"days": "14", "agent_id": "", "state": "seen"})
        assert sa.keep_days() == 14
        c.post("/hits/clear-filtered", data={"agent_id": str(a)})
        assert _states() == {"1": "cleared", "2": "filtered", "3": "cleared", "4": "seen"}
        c.post("/hits/clear-filtered", data={"agent_id": ""})
        assert _states()["2"] == "cleared"
        assert all(h["state"] != "cleared" for h in c.get("/api/hits?state=all").json())


def test_the_sync_button_keeps_the_sites_status(monkeypatch):
    """A hit card's sync loads the ad: sold, reserved or gone shows on every hit of the ad, with when it was checked."""
    from app import platforms
    from app.main import app
    from tests.fake_platform import FakePlatform, detail_of, listing

    fake = FakePlatform()
    platforms.set_platform("fake", fake)
    try:
        a, b = _agents()
        for agent in (a, b):
            with session() as db:
                db.add(Hit(agent_id=agent, platform="fake", ad_id="7", heading="Ad 7", url="https://fake.example/ad/7",
                           state="new"))
                db.commit()
        with session() as db:
            hid = db.exec(select(Hit).where(Hit.agent_id == a)).one().id
        d = detail_of(listing("7", "Ad 7", 10.0))
        d.status = "reserved"
        fake.details = {"7": d}
        with TestClient(app) as c:
            assert f'hx-post="/hits/{hid}/sync"' in c.get("/hits").text
            html = c.post(f"/hits/{hid}/sync").text
            assert "reserved" in html and "checked" in html
            with session() as db:
                assert {h.site_status for h in db.exec(select(Hit))} == {"reserved"}
            fake.details = {"7": None}                                  # the site no longer has it
            assert "no longer online" in c.post(f"/hits/{hid}/sync").text
    finally:
        platforms.reset()
    assert sa.site_status(None) == "gone"


def test_browser_notifications_page_and_poll():
    """Settings → Browser: the switch lives in the page (per browser); the jobs poll carries each event's kind, so the
    page shows hits and price drops as notifications."""
    from app.db import log_event
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/settings/browser").text
        assert "browserNotify.enable()" in page and "Show new hits as notifications" in page
        assert "const browserNotify" in page and "'price_drop'" in page
        cursor = c.get("/jobs").json()["last_event"]
        log_event("hit", "Agent: 20 €", "https://example.invalid/ad/1")
        events = c.get(f"/jobs?since={cursor}").json()["events"]
        assert [e["kind"] for e in events] == ["hit"]


def test_the_ad_viewer_shows_the_status_it_just_loaded():
    from app import platforms
    from app.main import app
    from tests.fake_platform import FakePlatform, detail_of, listing

    fake = FakePlatform()
    platforms.set_platform("fake", fake)
    try:
        a, _ = _agents()
        with session() as db:
            h = Hit(agent_id=a, platform="fake", ad_id="8", heading="Ad 8", url="https://fake.example/ad/8", state="seen")
            db.add(h)
            db.commit()
            hid = h.id
        d = detail_of(listing("8", "Ad 8", 10.0))
        d.status = "sold"
        fake.details = {"8": d}
        with TestClient(app) as c:
            pane = c.get(f"/hits/ad/fake/8?hit={hid}").text
        assert 'data-hit-status="%d"' % hid in pane and "sold" in pane and "checked" in pane
        assert 'hx-target="closest [data-hit-status]"' in pane
    finally:
        platforms.reset()
