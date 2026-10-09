import httpx
import pytest

from app.db import Conversation, session, set_setting, utcnow
from app.services import chats, ha_auth, notify as notify_mod, telegram
from app.willhaben.account import ConversationSummary, parse_conversations


@pytest.fixture
def channels(monkeypatch, tmp_path):
    sent = {"ha_push": 0, "ha_event": 0, "telegram": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/events/" in request.url.path:
            sent["ha_event"] += 1
        elif "/api/services/notify/" in request.url.path:
            sent["ha_push"] += 1
        return httpx.Response(200, json={})

    monkeypatch.setattr(notify_mod, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(ha_auth, "ha_url", lambda: "https://ha.example.com")
    monkeypatch.setattr(ha_auth, "ha_token", lambda: "tok")
    monkeypatch.setattr(ha_auth, "notify_service", lambda: "mobile_app_x")

    async def fake_tg(*a, **kw):
        sent["telegram"] += 1
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
