"""Every dashboard page renders (template errors show up here first), with a small data set."""
import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Hit, MyAd, Purchase, SearchAgent, session, utcnow
from app.main import SETTINGS_SECTIONS, app

PAGES = ["/", "/agents", "/hits", "/hits?agent_id=1&state=all", "/chats", "/chats/c1", "/myads", "/myads?ad=9", "/new-ad",
         "/purchases", "/purchases/1", "/purchases?show=all", "/settings", "/stats/willhaben", "/session", "/deals",
         "/myads/cleanup", "/bugreport"] + [f"/settings/{s}" for s in SETTINGS_SECTIONS]


@pytest.fixture
def data():
    with session() as db:
        db.add(SearchAgent(name="x", source_url="https://www.willhaben.at/iad/x", webapi_url="https://www.willhaben.at/webapi/x"))
        db.commit()
        db.add(Hit(agent_id=1, ad_id="1", state="new", heading="t", url="https://www.willhaben.at/iad/object?adId=1"))
        db.add(Conversation(id="c1", partner_name="Anna", ad_id="9", ad_title="Rad", unread_count=1, role="seller", last_message_at=utcnow()))
        db.add(MyAd(ad_id="9", heading="Rad", status="active", end_date=utcnow()))
        db.add(Purchase(ad_id="5", ad_title="Ding", status="new", mode="direct"))
        db.commit()


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(data, path):
    with TestClient(app) as c:
        r = c.get(path)
    assert r.status_code == 200, path
    assert 'class="rail"' in r.text or "embed" in path


def test_unknown_settings_section_is_404(data):
    with TestClient(app) as c:
        assert c.get("/settings/nope").status_code == 404
