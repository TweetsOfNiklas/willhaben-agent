import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.db import Conversation, Draft, Message, session, set_setting, utcnow
from app.llm.base import LLMResult
from app.llm.prompts import reply_prompt
from app.services import chats, gcal, meetups

TZ = gcal.TZ


@pytest.fixture
def google(monkeypatch, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/token":
            if "grant_type=authorization_code" in body:
                return httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "acc2", "expires_in": 3600})
        if request.url.path == "/revoke":
            return httpx.Response(200)
        if request.url.path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [
                {"id": "me@example.com", "summary": "Me", "primary": True, "accessRole": "owner"},
                {"id": "feiertage", "summary": "Feiertage", "accessRole": "reader"}]})
        if request.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": [
                {"start": "2026-10-10T08:00:00Z", "end": "2026-10-10T10:00:00Z"}]}}})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"id": "ev1", "htmlLink": "https://calendar.google.com/ev1"})
        return httpx.Response(404)

    monkeypatch.setattr(gcal, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(gcal, "_path", lambda: tmp_path / "google-calendar.json")
    gcal._cache.clear()
    gcal._pending.clear()
    return calls


async def _connect():
    gcal.save_client("", "", json.dumps({"installed": {"client_id": "abc.apps.googleusercontent.com",
                                                      "client_secret": "s3cret"}}))
    q = {k: v[0] for k, v in parse_qs(urlsplit(gcal.authorize_url()).query).items()}
    return await gcal.finish(f"{gcal.REDIRECT_URI}?state={q['state']}&iss=https%3A%2F%2Faccounts.google.com"
                             f"&code=4/abc&scope=https://www.googleapis.com/auth/calendar.events"), q


def test_web_client_is_flagged(google):
    gcal.save_client("", "", json.dumps({"web": {"client_id": "w.apps.googleusercontent.com", "client_secret": "s",
                                                "redirect_uris": ["https://example.com/cb"]}}))
    assert gcal.status()["kind"] == "web-unregistered"
    gcal.save_client("", "", json.dumps({"web": {"client_id": "w.apps.googleusercontent.com", "client_secret": "s",
                                                "redirect_uris": [gcal.REDIRECT_URI]}}))
    assert gcal.status()["kind"] == "web"


async def test_connect_flow(google, tmp_path):
    with pytest.raises(gcal.CalendarError):
        gcal.save_client("not-a-client", "x")
    account, q = await _connect()
    assert gcal.status()["kind"] == "desktop"
    assert q["redirect_uri"] == gcal.REDIRECT_URI and q["access_type"] == "offline"
    assert q["code_challenge_method"] == "S256" and "calendar.events" in q["scope"]
    assert account == "me@example.com" and gcal.connected()
    stored = tmp_path / "google-calendar.json"
    assert stored.stat().st_mode & 0o777 == 0o600 and json.loads(stored.read_text())["refresh_token"] == "ref"
    token_call = next(c for c in google if c[1] == "/token")
    assert "code_verifier=" in token_call[2] and "code=4%2Fabc" in token_call[2]
    with pytest.raises(gcal.CalendarError):          # state is single-use
        await gcal.finish(f"{gcal.REDIRECT_URI}?state={q['state']}&code=x")
    await gcal.disconnect()
    assert not gcal.connected() and gcal.has_client() and any(c[1] == "/revoke" for c in google)


def test_free_slots_window_and_busy():
    now = datetime(2026, 10, 9, 17, 7, tzinfo=TZ)      # Friday
    busy = [(datetime(2026, 10, 10, 10, 0, tzinfo=TZ), datetime(2026, 10, 10, 12, 0, tzinfo=TZ)),
            (datetime(2026, 10, 10, 11, 30, tzinfo=TZ), datetime(2026, 10, 10, 13, 0, tzinfo=TZ)),
            (datetime(2026, 10, 10, 13, 20, tzinfo=TZ), datetime(2026, 10, 10, 14, 0, tzinfo=TZ))]
    slots = gcal.free_slots(busy, now, 3, "09:00", "20:00")
    txt = gcal.format_slots(slots)
    assert "- Fr 09.10.: 17:15–20:00" in txt                         # from now, rounded to the quarter hour
    assert "- Sa 10.10.: 09:00–10:00, 14:00–20:00" in txt           # 13:00–13:20 is too short
    assert "- So 11.10.: 09:00–20:00" in txt
    late = gcal.free_slots([], datetime(2026, 10, 9, 19, 50, tzinfo=TZ), 1, "09:00", "20:00")
    assert gcal.format_slots(late) == "- Fr 09.10.: keine Zeit"


async def test_availability_text_uses_freebusy(google):
    await _connect()
    set_setting("gcal_window_start", "09:00")
    txt = await gcal.availability_text(now=datetime(2026, 10, 10, 8, 0, tzinfo=TZ))
    assert txt.startswith("- Sa 10.10.: 09:00–10:00, 12:00–20:00\n")   # 08–10 UTC = 10–12 Vienna is busy
    fb = json.loads(next(c for c in google if c[1].endswith("/freeBusy"))[2])
    assert fb["items"] == [{"id": "primary"}]


def test_reply_prompt_has_now_and_availability():
    p = reply_prompt(role="seller", ad=None, partner="Max", messages=[("in", utcnow(), "Wann kann ich kommen?")],
                     rules="", examples=[], hint=None, availability="- Sa 10.10.: 09:00–12:00",
                     now=datetime(2026, 10, 9, 12, 0, tzinfo=TZ))
    assert "Jetzt: Fr 2026-10-09 12:00" in p and "Meine freien Zeiten" in p and "09:00–12:00" in p
    assert "freien Zeiten" not in reply_prompt(role=None, ad=None, partner=None, messages=[], rules="",
                                               examples=[], hint=None)


def test_clean_meetup():
    future = (datetime.now(TZ) + timedelta(days=2)).strftime("%Y-%m-%dT15:30")
    assert meetups.clean({"start": future, "minutes": 0, "location": " Wien 15 "}) == {
        "start": future, "minutes": 30, "location": "Wien 15"}
    assert meetups.clean({"start": "", "minutes": 30, "location": ""}) is None
    assert meetups.clean({"start": "morgen", "minutes": 30, "location": ""}) is None
    assert meetups.clean({"start": "2020-01-01T10:00", "minutes": 30, "location": ""}) is None


async def test_meetup_draft_send_push_and_calendar(google, monkeypatch):
    from app.services import ha_actions, notify as notify_mod

    await _connect()
    start = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%dT18:00")

    class Backend:
        model = "fake"
        prompts = []

        async def complete(self, *, system, prompt, schema, images=None):
            self.prompts.append(prompt)
            return LLMResult(data={"reply": "Passt, morgen um 18 Uhr.", "intent": "logistics", "needs_info": [],
                                   "suggested_price": None, "note": "",
                                   "meetup": {"start": start, "minutes": 20, "location": "Wien 15"}}, model="fake")

    class Account:
        async def send_message(self, conversation_id, text):
            return "m1"

        async def mark_read(self, conversation_id):
            return 0

    notes, cleared = [], []

    async def fake_notify(kind, title, message, **kw):
        notes.append((kind, title, message, kw))
        return True

    async def fake_clear(tag):
        cleared.append(tag)

    async def no_ctx(cid):
        with session() as db:
            return db.get(Conversation, cid)

    backend = Backend()
    monkeypatch.setattr(chats, "get_backend", lambda *a: backend)
    monkeypatch.setattr(chats, "_account", lambda: Account())
    monkeypatch.setattr(chats, "ensure_ad_context", no_ctx)
    monkeypatch.setattr(notify_mod, "notify", fake_notify)
    monkeypatch.setattr(notify_mod, "clear_push", fake_clear)
    with session() as db:
        db.add(Conversation(id="c9", ad_id="9", ad_title="KTM Rad", partner_name="Max", role="seller"))
        db.add(Message(conversation_id="c9", ext_id="m0", direction="in", text="Morgen 18 Uhr?", created_at=utcnow()))
        db.commit()

    d = await chats.draft_reply("c9")
    assert d.meetup == {"start": start, "minutes": 20, "location": "Wien 15"}
    assert "Meine freien Zeiten" in backend.prompts[0]
    with session() as db:
        assert meetups.current(db.get(Conversation, "c9"), [d])["start"] == start

    await chats.send_draft(d.id, d.text)
    kind, _, _, kw = notes[-1]
    assert kind == "meetup" and kw["tag"] == f"wh-meetup-{d.id}"
    with session() as db:
        assert db.get(Conversation, "c9").meetup["summary"] == "willhaben: KTM Rad – Max"

    action = kw["actions"][0]["action"]
    assert await ha_actions.handle({"action": action}) == "calendar"
    ev = json.loads(next(c for c in google if c[1].endswith("/events"))[2])
    assert ev["summary"] == "willhaben: KTM Rad – Max" and ev["location"] == "Wien 15"
    assert ev["start"]["dateTime"].startswith(start) and ev["end"]["dateTime"].startswith(start[:11] + "18:20")
    with session() as db:
        assert db.get(Conversation, "c9").meetup["event_id"] == "ev1"
    assert await ha_actions.handle({"action": action}) is None       # second tap: nothing
    assert cleared == [f"wh-meetup-{d.id}"] * 2
    assert sum(1 for c in google if c[1].endswith("/events")) == 1

    # a later answer with the same time keeps the calendar link
    meetups.remember("c9", {"start": start, "minutes": 20, "location": "Wien 15"})
    with session() as db:
        assert db.get(Conversation, "c9").meetup["event_id"] == "ev1"


async def test_no_calendar_no_offer(monkeypatch):
    notes = []

    async def fake_notify(*a, **kw):
        notes.append(a)

    from app.services import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", fake_notify)
    with session() as db:
        db.add(Conversation(id="c8", ad_title="X"))
        d = Draft(conversation_id="c8", text="ok", meetup={"start": "2099-01-01T10:00", "minutes": 30, "location": ""})
        db.add(d)
        db.commit()
        db.refresh(d)
    monkeypatch.setattr(gcal, "connected", lambda: False)
    await meetups.on_sent(d)
    assert notes == []
    with session() as db:
        assert db.get(Conversation, "c8").meetup["start"] == "2099-01-01T10:00"   # chat page still offers it


async def test_pasting_json_or_error_page(google):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/settings/gcal/code", data={"pasted": json.dumps(
            {"installed": {"client_id": "d.apps.googleusercontent.com", "client_secret": "s"}})},
            follow_redirects=False)
    assert r.status_code == 303 and gcal.status()["kind"] == "desktop"
    import base64
    err = base64.urlsafe_b64encode(b"\x0a\x15redirect_uri_mismatch\x12\x10Bad request here").decode().rstrip("=")
    with pytest.raises(gcal.CalendarError, match="Desktop app"):
        await gcal.finish(f"https://accounts.google.com/signin/oauth/error/v2?authError={err}&client_id=x")
    err = base64.urlsafe_b64encode(b"\x0a\x0daccess_denied").decode()
    with pytest.raises(gcal.CalendarError, match="test user"):
        await gcal.finish(f"https://accounts.google.com/signin/oauth/error/v2?authError={err}")
    with pytest.raises(gcal.CalendarError, match="Finish the sign-in"):
        await gcal.finish("https://accounts.google.com/v3/signin/identifier?x=1")
