from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Conversation, Draft, Message, session, utcnow
from app.services import chats
from app.willhaben.account import ConversationSummary


def _setup(monkeypatch, state):
    class _Sess:
        started = logged_in = True

    class _Acct:
        async def conversations(self, limit=50):
            return [ConversationSummary(id="c1", ad_id="9", ad_title="Rad", partner_name="Anna", unread_count=0,
                                        last_text=state["text"], last_at=state["at"], raw={}, role="seller",
                                        last_is_mine=state["mine"])]

    synced = []

    async def sync_messages(cid):
        synced.append(cid)
        return "full"

    monkeypatch.setattr(chats, "get_session", lambda: _Sess())
    monkeypatch.setattr(chats, "_account", lambda: _Acct())
    monkeypatch.setattr(chats, "sync_messages", sync_messages)
    return synced


async def test_removed_chat_stays_away_until_they_write_again(monkeypatch):
    at = utcnow() - timedelta(hours=1)
    state = {"text": "Noch da?", "at": at, "mine": False}
    synced = _setup(monkeypatch, state)
    await chats.sync()
    with session() as db:
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in", text="Noch da?"))
        db.add(Draft(conversation_id="c1", text="Ja"))
        db.commit()
    chats.remove("c1")
    with session() as db:
        assert db.exec(select(Message)).all() == [] and db.exec(select(Draft)).all() == []
        assert db.get(Conversation, "c1").removed
    synced.clear()
    await chats.sync(force=True)
    assert synced == []                                     # removed: not synced again
    state.update(at=utcnow(), mine=True, text="Meine Antwort von woanders")
    await chats.sync()
    with session() as db:
        assert synced == [] and db.get(Conversation, "c1").removed   # my own message does not bring it back
    state.update(at=utcnow() + timedelta(seconds=1), mine=False, text="Hallo?")
    await chats.sync()
    with session() as db:
        c = db.get(Conversation, "c1")
        assert synced == ["c1"] and not c.removed and c.hidden_at is None and c.last_message_text == "Hallo?"


async def test_hidden_chat_comes_back_on_a_new_message(monkeypatch):
    state = {"text": "Noch da?", "at": utcnow() - timedelta(hours=1), "mine": False}
    _setup(monkeypatch, state)
    await chats.sync()
    chats.hide("c1")
    await chats.sync(force=True)
    with session() as db:
        assert db.get(Conversation, "c1").hidden_at is not None    # still hidden, but kept up to date
    state.update(at=utcnow(), text="Hallo?")
    await chats.sync()
    with session() as db:
        assert db.get(Conversation, "c1").hidden_at is None


def test_chat_list_hides_and_removes():
    from app.main import app

    with session() as db:
        db.add(Conversation(id="a", partner_name="Sichtbar", last_message_at=utcnow()))
        db.add(Conversation(id="b", partner_name="Versteckt", last_message_at=utcnow(), hidden_at=utcnow()))
        db.add(Conversation(id="c", partner_name="Entfernt", last_message_at=utcnow(), hidden_at=utcnow(), removed=True))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/chats").text
        assert "Sichtbar" in page and "Versteckt" not in page and "Entfernt" not in page
        assert "Show hidden chats (1)" in page
        page = c.get("/chats?show_hidden=1").text
        assert "Versteckt" in page and "Entfernt" not in page
        c.post("/chats/a/hide", data={"hidden": "1"})
        c.post("/chats/b/hide", data={"hidden": "0"})
        page = c.get("/chats").text
        assert "Versteckt" in page and "Sichtbar" not in page
        c.post("/chats/b/remove")
        assert "Versteckt" not in c.get("/chats?show_hidden=1").text
        assert {x["id"] for x in c.get("/api/chats").json()} == {"a"}
