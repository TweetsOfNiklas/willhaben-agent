import json

import httpx
import pytest

from app.config import get_settings
from app.db import Conversation, session, set_setting, utcnow
from app.services import chats, ha_auth, notify as notify_mod, telegram
from app.services.my_ads import MYADVERTS_URL
from app.willhaben.account import ConversationSummary, parse_conversations


@pytest.fixture
def channels(monkeypatch, tmp_path):
    sent = {"ha_push": 0, "ha_event": 0, "telegram": 0, "bodies": {}, "tg_url": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/events/" in request.url.path:
            sent["ha_event"] += 1
            sent["bodies"]["event"] = json.loads(request.content)
        elif "/api/services/notify/" in request.url.path:
            sent["ha_push"] += 1
            sent["bodies"]["push"] = json.loads(request.content)
        return httpx.Response(200, json={})

    monkeypatch.setattr(notify_mod, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(ha_auth, "ha_url", lambda: "https://ha.example.com")
    monkeypatch.setattr(ha_auth, "ha_token", lambda: "tok")
    monkeypatch.setattr(ha_auth, "notify_service", lambda: "mobile_app_x")

    async def fake_tg(*a, **kw):
        sent["telegram"] += 1
        sent["tg_url"] = kw.get("url")
        return True

    monkeypatch.setattr(telegram, "send", fake_tg)
    return sent


@pytest.mark.parametrize("setting, linked, want", [
    ("auto", True, {"ha_push": 0, "telegram": 1}),
    ("auto", False, {"ha_push": 1, "telegram": 0}),
    ("ha", True, {"ha_push": 1, "telegram": 0}),
    ("telegram", True, {"ha_push": 0, "telegram": 1}),
    ("both", True, {"ha_push": 1, "telegram": 1}),
])
async def test_one_push_channel(channels, monkeypatch, setting, linked, want):
    monkeypatch.setattr(telegram, "active", lambda: linked)
    set_setting("push_channel", setting)
    await notify_mod.notify("message", "T", "M")
    assert {k: channels[k] for k in want} == want and channels["ha_event"] == 1   # event always


WH_AD = "https://www.willhaben.at/iad/kaufen-und-verkaufen/d/raspberry-pi-4-4gb-1662146981/"
WH_CHAT = "https://www.willhaben.at/iad/myprofile/chat"


@pytest.mark.parametrize("kind, url, data, path", [
    ("message", WH_CHAT, {"conversation_id": "c1", "ad_id": "1662146981"}, "/chats/c1"),
    ("hit", WH_AD, {"ad_id": "1662146981", "agent": "Pi"}, "/hits?view=1662146981"),
    ("price_drop", WH_AD, {"ad_id": 1662146981}, "/hits?view=1662146981"),
    ("ad_expired", MYADVERTS_URL, {"ad_id": "1662146981"}, "/myads?ad=1662146981"),
    ("health", "/settings/claude", None, "/settings/claude"),          # Claude token limit: a bare app path
    ("hit", None, {"agent": "Pi", "count": 3}, "/hits"),                # "+3 more new hits"
])
async def test_pushes_open_the_app(channels, monkeypatch, kind, url, data, path):
    """Phone pushes link to the app page when public_url is reachable (https for Telegram); the HA event keeps "url"."""
    monkeypatch.setattr(get_settings(), "public_url", "https://wh.example.com/")
    set_setting("push_channel", "both")
    await notify_mod.notify(kind, "T", "M", url=url, data=data)
    push, event = channels["bodies"]["push"]["data"], channels["bodies"]["event"]
    assert push["clickAction"] == push["url"] == channels["tg_url"] == "https://wh.example.com" + path
    assert event["url"] == url and event["app_url"] == "https://wh.example.com" + path
    assert telegram.keyboard(kind, {}, [], channels["tg_url"])["inline_keyboard"][-1][0]["url"] == "https://wh.example.com" + path


async def test_pushes_keep_willhaben_when_the_app_is_not_reachable(channels, monkeypatch):
    set_setting("push_channel", "both")
    data = {"conversation_id": "c1"}
    monkeypatch.setattr(get_settings(), "public_url", "http://localhost:8000")   # the default: a phone cannot open it
    await notify_mod.notify("message", "T", "M", url=WH_CHAT, data=data)
    assert channels["bodies"]["push"]["data"]["clickAction"] == channels["tg_url"] == WH_CHAT
    assert channels["bodies"]["event"]["url"] == WH_CHAT
    assert channels["bodies"]["event"]["app_url"] is None        # automations can use `app_url or url`
    await notify_mod.notify("health", "T", "M", url="/settings/claude")   # a bare app path: no link at all
    assert "clickAction" not in channels["bodies"]["push"]["data"] and channels["tg_url"] == "/settings/claude"
    assert telegram.keyboard("health", {}, [], channels["tg_url"]) is None                 # no Open button
    # reachable but plain http (LAN): HA opens the app, Telegram (https buttons only) keeps willhaben
    monkeypatch.setattr(get_settings(), "public_url", "http://192.168.1.5:8000")
    await notify_mod.notify("message", "T", "M", url=WH_CHAT, data=data)
    assert channels["bodies"]["push"]["data"]["clickAction"] == "http://192.168.1.5:8000/chats/c1"
    assert channels["bodies"]["event"]["app_url"] == "http://192.168.1.5:8000/chats/c1"
    assert channels["tg_url"] == WH_CHAT
    # nothing in the app fits (calendar link): the original link everywhere, no app_url
    monkeypatch.setattr(get_settings(), "public_url", "https://wh.example.com")
    await notify_mod.notify("meetup", "T", "M", url="https://calendar.google.com/e/1")
    assert channels["bodies"]["push"]["data"]["clickAction"] == channels["tg_url"] == "https://calendar.google.com/e/1"
    assert channels["bodies"]["event"]["app_url"] is None


def test_parse_last_is_mine():
    base = {"conversation_id": "c", "unseen": 1, "last_message": {"is_mine": True, "preview": "Ja"}}
    assert parse_conversations({"conversations": [base]})[0].last_is_mine is True
    base["last_message"]["is_mine"] = False
    assert parse_conversations({"conversations": [base]})[0].last_is_mine is False


async def test_own_reply_is_not_pushed_as_new_message(monkeypatch):
    notes = []

    async def fake_notify(kind, title, message, **kw):
        notes.append(kind)
        return True

    class _Sess:
        started = logged_in = True

    state = {"mine": True}

    class _Acct:
        async def conversations(self, limit=50):
            return [ConversationSummary(id="c1", ad_id="9", ad_title="Rad", partner_name="Anna", unread_count=1,
                                        last_text="Ja, noch da", last_at=utcnow(), raw={}, role="seller",
                                        last_is_mine=state["mine"])]

    async def no_msgs(cid):
        return "full"

    monkeypatch.setattr(chats, "notify", fake_notify)
    monkeypatch.setattr(chats, "get_session", lambda: _Sess())
    monkeypatch.setattr(chats, "_account", lambda: _Acct())
    monkeypatch.setattr(chats, "sync_messages", no_msgs)
    set_setting("notify_messages", "1")
    with session() as db:
        db.add(Conversation(id="c0", ad_id="1"))        # not the first sync
        db.commit()
    await chats.sync()
    assert notes == []                                   # my own answer: no "new message" push
    state["mine"] = False
    await chats.sync()
    assert notes == ["message"]                          # the buyer wrote: one push
