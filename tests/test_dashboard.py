import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db as dbmod
from app.llm.base import LLMResult
from app.willhaben import public as public_mod
from app.willhaben.public import PublicClient
from tests.conftest import FIXTURES, load
from tests.conftest import wait_jobs


class FakeBackend:
    model = "fake-opus"

    def __init__(self):
        self.calls = []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "images": images or []})
        if "reply" in schema["properties"]:
            data = {"reply": "Hallo, ja, das Rad ist noch da. 180 € wäre ok.", "intent": "price_negotiation",
                    "needs_info": ["Abholzeit"], "suggested_price": 180, "note": ""}
        else:
            data = {"title": "KTM Kinderrad 20 Zoll blau", "description": "Gut erhalten.", "category_hint": "Sport",
                    "condition": "gebraucht", "attributes": [{"name": "Marke", "value": "KTM"}],
                    "search_keywords": "ktm kinderrad 20", "price_estimate": 120, "note": ""}
        return LLMResult(data=data, model=self.model)


def _detail_html() -> str:
    nd = {"page": "/iad/kaufen-und-verkaufen/d/[[...seopath]]", "buildId": "x",
          "props": {"pageProps": load("detail_1662146981_nextdata.json")}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'


@pytest.fixture
def client(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr("app.services.chats.get_backend", lambda *a: fake)
    monkeypatch.setattr("app.services.ad_creator.get_backend", lambda *a: fake)
    search = load("search_fahrrad_rows3.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/webapi/ad-search/search"):
            return httpx.Response(200, json=search)
        if request.url.path == "/iad/object":
            return httpx.Response(200, text=_detail_html())
        return httpx.Response(404)

    public_mod.set_public(PublicClient(transport=httpx.MockTransport(handler)))
    from app.main import app

    with TestClient(app) as c:
        c.fake = fake
        yield c
    public_mod.set_public(None)


def _conversation():
    with dbmod.session() as db:
        db.add(dbmod.Conversation(id="c1", ad_id="1662146981", ad_title="Fahrradhose", partner_name="Max",
                                  unread_count=1, last_message_text="Nehmen Sie 150?",
                                  last_message_at=datetime(2026, 10, 9, 8, tzinfo=timezone.utc)))
        db.add(dbmod.Message(conversation_id="c1", ext_id="m1", direction="in", text="Nehmen Sie 150?",
                             created_at=datetime(2026, 10, 9, 8, tzinfo=timezone.utc)))
        db.commit()


def test_pages_render(client):
    for path in ["/", "/agents", "/hits", "/chats", "/myads", "/new-ad", "/session", "/settings"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "willhaben Agent" in r.text


def test_chat_draft_send_flow(client):
    _conversation()
    r = client.post("/chats/c1/draft", data={"hint": "Gegenangebot 180"}, follow_redirects=False)
    wait_jobs()
    assert r.status_code == 303 and r.headers["location"] == "/chats/c1"

    prompt = client.fake.calls[-1]["prompt"]
    assert "Fahrradhose Gr xs" in prompt          # ad context fetched from the (mocked) detail page
    assert "Partner: Nehmen Sie 150?" in prompt
    assert "Gegenangebot 180" in prompt

    page = client.get("/chats/c1").text
    assert "180 € wäre ok" in page and "Abholzeit" in page

    with dbmod.session() as db:
        draft = db.exec(select(dbmod.Draft)).one()
        conv = db.get(dbmod.Conversation, "c1")
    assert conv.ad_context["title"] == "Fahrradhose Gr xs"

    # Sending is queued: the page comes back at once with the message marked "sending" ...
    r = client.post(f"/drafts/{draft.id}/send", data={"text": "Hallo, 180 € passt."}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/chats/c1#draft-{draft.id}"
    wait_jobs()
    # ... no browser session in tests: it fails visibly, goes back to an open draft and nothing is marked as sent
    with dbmod.session() as db:
        d = db.get(dbmod.Draft, draft.id)
        assert d.status == "draft" and d.text == "Hallo, 180 € passt." and "Browser session is not running" in d.error
    assert client.get("/jobs").json()["recent"][0]["status"] == "failed"
    assert "Browser session is not running" in client.get("/chats/c1").text

    client.post(f"/drafts/{draft.id}/mark-sent", data={"text": "Hallo, 180 € passt."})
    msgs = client.get("/api/chats/c1").json()["messages"]
    assert [m["direction"] for m in msgs] == ["in", "out"]

    # the sent text becomes a style example for the next draft
    client.post("/chats/c1/draft", data={"hint": ""})
    wait_jobs()
    assert "Hallo, 180 € passt." in client.fake.calls[-1]["prompt"]


def test_api_save_draft_and_send_conflict(client):
    _conversation()
    d = client.post("/api/chats/c1/drafts", json={"text": "Ja, noch da."}).json()
    r = client.post(f"/api/drafts/{d['id']}/send", json={"text": "Ja, noch da."})
    assert r.status_code == 503 and r.json()["sent"] is False


def test_ad_draft_with_photo_and_price_stats(client):
    jpg = (FIXTURES / "search_fahrrad_rows3.json").read_bytes()[:100]  # any bytes, the fake ignores content
    r = client.post("/new-ad", data={"notes": "KTM Kinderrad 20 Zoll"},
                    files=[("photos", ("rad.jpg", jpg, "image/jpeg"))], follow_redirects=False)
    assert r.status_code == 303
    draft_url = r.headers["location"]
    call = client.fake.calls[-1]
    assert len(call["images"]) == 1 and "KTM Kinderrad" in call["prompt"]
    page = client.get(draft_url).text
    assert "KTM Kinderrad 20 Zoll blau" in page
    with dbmod.session() as db:
        d = db.exec(select(dbmod.AdDraft)).one()
    assert d.price_stats["llm_estimate"] == 120
    assert d.comparables  # comparable ads from the mocked search
    assert client.get(f"/ad-drafts/{d.id}/image/0").status_code == 200


def test_agent_create_via_api(client):
    r = client.post("/api/agents", json={"url": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=rad"})
    assert r.status_code == 200 and r.json()["webapi_url"].endswith("keyword=rad&sort=1&rows=30")
    assert client.post("/api/agents", json={"url": "https://example.com"}).status_code == 400


class _FakeKeyboard:
    def __init__(self, log):
        self.log = log

    async def type(self, text):
        self.log.append(("type", text))

    async def press(self, key):
        self.log.append(("press", key))


class _FakeMouse:
    def __init__(self, log):
        self.log = log

    async def click(self, x, y):
        self.log.append(("click", x, y))

    async def move(self, x, y):
        pass

    async def wheel(self, dx, dy):
        self.log.append(("wheel", dy))


class _FakeContext:
    async def close(self):
        pass


class _FakePage:
    def __init__(self):
        self.log = []
        self.keyboard, self.mouse = _FakeKeyboard(self.log), _FakeMouse(self.log)


def test_remote_input(client, monkeypatch):
    from app.willhaben.session import get_session

    sess, page = get_session(), _FakePage()
    monkeypatch.setattr(sess, "context", _FakeContext())
    monkeypatch.setattr(sess, "api_page", object())
    monkeypatch.setattr(sess, "ui_page", page)

    assert client.post("/session/input", json={"type": "click", "x": 10.5, "y": 20}).status_code == 204
    assert client.post("/session/input", json={"type": "text", "text": "user@example.at"}).status_code == 204
    assert client.post("/session/input", json={"type": "key", "key": "Enter"}).status_code == 204
    assert client.post("/session/input", json={"type": "scroll", "dy": 99999}).status_code == 204
    assert page.log == [("click", 10.5, 20.0), ("type", "user@example.at"), ("press", "Enter"), ("wheel", 3000.0)]

    assert client.post("/session/input", json={"type": "key", "key": "Control+w"}).status_code == 400
    assert client.post("/session/input", json={"type": "click"}).status_code == 400
    assert client.post("/session/input", json={"type": "nope"}).status_code == 400


def test_remote_input_without_browser(client):
    assert client.post("/session/input", json={"type": "text", "text": "x"}).status_code == 503
    assert client.get("/session/screen.jpg").status_code == 503


def test_agent_builder(client):
    r = client.get("/agents/new", params={"keyword": "fahrrad", "r_PRICE_TO": "800 €"})
    assert r.status_code == 200
    assert "Antiquitäten / Kunst" in r.text and "Create search agent" in r.text
    assert 'name="url" value="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad"' in r.text

    r = client.post("/agents", data={"url": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad",
                                     "name": "fahrrad", "interval_min": "5"}, follow_redirects=False)
    assert r.status_code == 303
    agents = client.get("/api/agents").json()
    assert agents[-1]["name"] == "fahrrad" and "keyword=fahrrad" in agents[-1]["webapi_url"]


def test_agent_builder_rejects_foreign_url(client):
    r = client.get("/agents/new", params={"url": "https://evil.example/iad/x"})
    assert r.status_code == 200 and "Not a willhaben.at URL" in r.text


def test_model_selector(client):
    from app.llm.base import get_backend

    import re

    def selected(page, p):           # the option(s) marked selected in the model_<p> select
        box = re.search(rf'<select name="model_{p}"[^>]*>(.*?)</select>', page, re.S).group(1)
        return re.findall(r'<option value="([^"]+)" selected>', box)

    page = client.get("/settings/claude").text
    assert 'name="model_chat"' in page and 'name="model_ads"' in page and 'name="claude_token_limit_m"' in page
    assert 'name="model_shipping"' in page and 'name="model_purchase"' in page
    assert selected(page, "shipping") == selected(page, "purchase") == ["claude-sonnet-5-5"]
    assert selected(page, "ads") == ["claude-opus-5-5"] and 'value="claude-fable-5-1"' in page
    assert client.post("/settings/claude", data={"model_chat": "haiku", "model_screen": "sonnet", "claude_token_limit_m": "12",
                                                 "model_purchase": "claude-fable-5-1", "model_shipping": "fable",
                                                 "claude_limit_mode": "haiku"}, follow_redirects=False).status_code == 303
    from app.db import get_setting, set_setting
    assert get_setting("claude_token_limit_m") == "12" and get_setting("claude_limit_mode") == "haiku"
    assert get_setting("llm_model_purchase") == get_setting("llm_model_shipping") == "claude-fable-5-1"
    assert get_setting("llm_model_chat") == "claude-haiku-5-5" and get_setting("llm_model_screen") == "claude-sonnet-5-5"
    page = client.get("/settings/claude").text
    assert selected(page, "purchase") == selected(page, "shipping") == ["claude-fable-5-1"]
    assert selected(page, "chat") == ["claude-haiku-5-5"]
    home = client.get("/").text
    assert 'href="/settings/claude" data-pop="claude" title="Claude tokens this month"' in home and 'id="claude-pop"' in home   # meter + popover
    assert client.post("/settings/model", data={"purpose": "ads", "model": "claude-fable-5-1"},
                       follow_redirects=False).status_code == 303
    assert get_backend("chat").model == "claude-haiku-5-5"
    assert get_backend("ads").model == "claude-fable-5-1"
    args = get_backend("ads").build_args(system="s", schema={}, with_images=True)
    assert args[args.index("--model") + 1] == "claude-fable-5-1"
    assert get_backend(None).model == "opus"            # test-llm etc. still use the default
    assert client.post("/settings/model", data={"purpose": "chat", "model": "gpt-4"}).status_code == 400
    assert client.post("/settings/model", data={"purpose": "x", "model": "claude-opus-5-5"}).status_code == 400
    set_setting("llm_model_search", "claude-sonnet-4-5")     # not in the list: shown as it is, Save keeps it
    assert selected(client.get("/settings/claude").text, "search") == ["claude-sonnet-4-5"]
    client.post("/settings/claude", data={"model_search": "claude-sonnet-4-5"}, follow_redirects=False)
    assert get_setting("llm_model_search") == "claude-sonnet-4-5"


def test_hit_detail_viewer(client):
    from app.db import Hit, SearchAgent

    with dbmod.session() as db:
        db.add(SearchAgent(id=7, name="Rad", source_url="https://www.willhaben.at/iad/x", webapi_url="https://x"))
        db.add(Hit(id=5, agent_id=7, ad_id="1662146981", heading="Fahrradhose", url="https://www.willhaben.at/iad/object?adId=1662146981",
                   state="new"))
        db.commit()
    page = client.get("/hits", params={"agent_id": 7}).text
    assert 'data-ad="1662146981" data-hit="5"' in page and 'id="ad-viewer"' in page
    r = client.get("/hits?state=new&agent_id=")                 # "All search agents" sends an empty value
    assert r.status_code == 200 and 'data-hit="5"' in r.text
    assert client.get("/hits?agent_id=abc").status_code == 200   # junk is "all", not a 422 page
    frag = client.get("/hits/ad/1662146981", params={"hit": 5}).text
    assert "Fahrradhose Gr xs" in frag and "willhaben ↗" in frag and 'data-state="saved"' in frag
    assert "<html" not in frag                                   # fragment, not a full page
    with dbmod.session() as db:
        assert db.get(Hit, 5).state == "seen"                      # opening counts as seen
    assert client.get("/hits/ad/abc").status_code == 400


def test_ad_viewer_shows_full_claude_assessment(client):
    from app.db import Hit, SearchAgent

    reason = "Passt genau: Carbon-Rennrad Größe M. " + "Preis liegt deutlich unter vergleichbaren Angeboten. " * 6
    with dbmod.session() as db:
        db.add(SearchAgent(id=8, name="Rad", source_url="https://www.willhaben.at/iad/x", webapi_url="https://x"))
        db.add(Hit(id=6, agent_id=8, ad_id="1662146981", heading="Fahrradhose", url="https://www.willhaben.at/iad/object?adId=1662146981",
                   state="new", screen_relevant=True, screen_deal="great", screen_reason=reason, screen_at=dbmod.utcnow()))
        db.commit()
    frag = client.get("/hits/ad/1662146981", params={"hit": 6}).text
    assert "Claude" in frag and reason.strip() in frag            # complete text, not the 90-char card excerpt


def test_remote_overlay_compact_header(client, monkeypatch):
    from app.willhaben.session import get_session

    sess = get_session()
    status = {**sess.status(), "started": True, "logged_in": True, "user": {"nickname": "Max"}}
    monkeypatch.setattr(sess, "status", lambda: status)
    embed = client.get("/session", params={"embed": 1}).text
    assert 'class="embed-status' in embed and "Max" in embed
    assert "You log in in the app" not in embed and "Sie loggen" not in embed     # long explanation only on the full page
    assert '<header>' not in embed.split('id="remote"')[1].split("remote-screen")[0]  # no "Browser window" heading in the overlay
    full = client.get("/session").text
    assert 'class="embed-status' not in full and 'id="remote"' in full


def test_agent_create_form_is_not_a_squeezing_grid(client):
    page = client.get("/agents/new", params={"keyword": "fahrrad"}).text
    form = page.split('action="/agents"')[1].split("</form>")[0]
    assert 'name="name"' in form and 'name="screen_on"' in form and 'name="interval_min"' in form


def test_home_lists_only_ads_expiring_within_5_days_and_8_activities(client):  # noqa: F811
    from datetime import timedelta

    from app.db import EventLog, MyAd, session, utcnow
    with session() as db:
        db.add(MyAd(ad_id="31", heading="Bald weg", status="active", end_date=utcnow() + timedelta(days=3)))
        db.add(MyAd(ad_id="32", heading="Noch lange", status="active", end_date=utcnow() + timedelta(days=20)))
        for i in range(12):
            db.add(EventLog(kind="hit", message=f"Ereignis {i:02d}"))
        db.commit()
    page = client.get("/").text
    soon = page.split("Expiring soon")[1]
    assert "Bald weg" in soon and "Noch lange" not in soon
    activity = page.split('id="activity"')[1].split("</section>")[0]          # the inbox popover has its own list
    assert sum(f"Ereignis {i:02d}" in activity for i in range(12)) == 8
