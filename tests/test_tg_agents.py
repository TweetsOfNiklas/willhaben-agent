"""The Telegram Mini App's search agents (/tg/agents): the list, one agent with its settings, the builder; their forms
post to the dashboard's own endpoints (app/tg_bridge.py) and come back to the Mini App."""
import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import Hit, SearchAgent, session
from app.services.search_agents import agent_searches, create_agent
from tests.fake_platform import FakePlatform, listing

URL = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad"


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake():
    p = FakePlatform()
    platforms.set_platform("fake", p)
    yield p
    platforms.reset()


def _agent(agent_id):
    with session() as db:
        return db.get(SearchAgent, agent_id)


def test_the_list_with_switch_check_now_and_bulk(client):
    a = create_agent(URL)
    with session() as db:
        db.add(Hit(agent_id=a.id, ad_id="1", heading="Fahrrad", url="u1", state="new"))
        db.commit()
    page = client.get("/tg/agents").text
    assert f'data-agent="{a.id}"' in page and f'href="/tg/agents/{a.id}"' in page and 'class="badge">1<' in page
    assert f'action="/agents/{a.id}/run"' in page and 'action="/agents/bulk"' in page and "data-new" in page
    r = client.post(f"/tg/agents/{a.id}/toggle", data={"back": "/tg"}, follow_redirects=False)
    assert r.headers["location"] == "/tg" and _agent(a.id).enabled is False         # the home's switch comes back home
    assert client.post(f"/tg/agents/{a.id}/toggle", follow_redirects=False).headers["location"] == "/tg/agents"
    r = client.post("/agents/bulk", data={"ids": [str(a.id)], "notify": "0", "interval_min": "17"},
                    headers={"X-TG-Back": "/tg/agents"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/tg/agents?msg=")
    assert _agent(a.id).notify is False and _agent(a.id).interval_s >= 17 * 60


def test_one_agent_edited_and_deleted_through_the_dashboards_endpoints(client):
    a = create_agent(URL)
    page = client.get(f"/tg/agents/{a.id}").text
    assert 'id="agent-form"' in page and f'action="/agents/{a.id}/edit"' in page and 'name="screen_brief"' in page
    assert 'name="gh_level"' in page and 'name="max_km"' in page and 'name="add_keyword"' in page and 'name="gh_check"' in page
    assert "data-main-form=\"agent-form\"" in page and f'action="/agents/{a.id}/delete"' in page and "hx-confirm" in page
    form = {"name": "Räder", "interval_min": "20", "notify_on": "on", "screen_on": "on", "screen_brief": "nur 28 Zoll",
            "screen_min": "good", "gh_on": "on", "gh_level": "pct", "gh_pct": "40", "km_on": "on", "max_km": "25",
            "add_keyword": ["rennrad", ""], "add_price_from": ["", ""], "add_price_to": ["500", ""]}
    r = client.post(f"/agents/{a.id}/edit", data=form, headers={"X-TG-Back": f"/tg/agents/{a.id}"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/tg/agents")
    row = _agent(a.id)
    assert (row.name, row.screen, row.screen_brief, row.screen_min, row.geizhals_pct, row.max_km) == \
        ("Räder", True, "nur 28 Zoll", "good", 40, 25)
    assert any(q.get("keyword") == "rennrad" and q.get("price_to") == 500 for q in agent_searches(row))
    r = client.post(f"/agents/{a.id}/delete", headers={"X-TG-Back": f"/tg/agents/{a.id}"}, follow_redirects=False)
    assert r.headers["location"] == "/tg/agents" and _agent(a.id) is None
    assert client.get(f"/tg/agents/{a.id}").status_code == 404


def test_the_builder_previews_and_creates(client, fake):
    fake.pages = [[listing("1", "Rennrad Canyon", 900.0)]]
    page = client.get("/tg/agents/new?platform=fake&keyword=rennrad&price_to=1000").text
    assert "Rennrad Canyon" in page and 'id="agent-create"' in page and 'action="/agents"' in page
    assert 'name="platform" value="fake"' in page and 'action="/tg/agents/new"' in page and 'name="screen_on"' in page
    url = "https://fake.example/s/kinderrad"
    r = client.post("/agents", data={"url": url, "platform": "fake", "name": "Kinderrad", "interval_min": "10"},
                    headers={"X-TG-Back": "/tg/agents/new"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tg/agents"
    with session() as db:
        assert db.exec(SearchAgent.__table__.select().where(SearchAgent.name == "Kinderrad")).first() is not None


def test_the_cards_show_the_latest_hits_and_the_searches_preview_their_count(client):
    """As on the dashboard: each agent's latest hits as pictures (opening the hit in the Mini App); the searches
    editor carries the data its script counts with (searches per check, terms added before Save, the last-search
    guard)."""
    a = create_agent(URL)
    with session() as db:
        for i in range(5):
            db.add(Hit(agent_id=a.id, ad_id=str(i), heading=f"Rad {i}", url=f"u{i}", state="new",
                       image_url=f"https://img.example/{i}.jpg"))
        db.commit()
    page = client.get("/tg/agents").text
    strip = page[page.index(f'data-thumbs="{a.id}"'):]
    strip = strip[:strip.index("</span>")]
    assert strip.count('href="/tg/hits/') == 4 and "https://img.example/" in strip
    agent = client.get(f"/tg/agents/{a.id}").text
    assert "data-per-check" in agent and 'data-js="' in agent and "data-add-row" in agent and "data-k=" in agent
    assert 'data-warn="last"' in agent and "&#34;fill&#34;" in agent
