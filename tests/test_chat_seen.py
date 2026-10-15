"""Sent / seen ticks on my own chat messages: willhaben's read flag (history and chat list) and the live socket's read
events, stored on Message.read and shown in the chat; the composer shows the newest open draft."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Conversation, Draft, Message, session, utcnow
from app.services import chats
from app.willhaben.account import ChatMessage, ConversationSummary, parse_conversations, parse_messages

T0 = utcnow().replace(microsecond=0) - timedelta(hours=2)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _rows():
    with session() as db:
        return {m.ext_id: m for m in db.exec(select(Message))}


def _chat(*msgs):
    with session() as db:
        db.add(Conversation(id="c1", partner_name="Käufer", last_message_at=T0))
        for ext, direction, minutes in msgs:
            db.add(Message(conversation_id="c1", ext_id=ext, direction=direction, text=ext,
                           created_at=T0 + timedelta(minutes=minutes)))
        db.commit()


def test_parsing_keeps_the_read_flag_of_messages_and_of_the_lists_newest_message():
    msgs = parse_messages({"messages": [
        {"message_id": "a", "text": "Hallo", "created_at": _iso(T0), "is_mine": True, "read": True},
        {"message_id": "b", "text": "Noch da?", "created_at": _iso(T0 + timedelta(minutes=1)), "is_mine": True, "read": False},
    ]}, set())
    assert [(m.ext_id, m.direction, m.read) for m in msgs] == [("a", "out", True), ("b", "out", False)]
    convs = parse_conversations({"conversations": [
        {"conversation_id": "c1", "last_message": {"is_mine": True, "read": False, "timestamp": _iso(T0)}},
        {"conversation_id": "c2", "last_message": {"is_mine": True, "read": True, "timestamp": _iso(T0)}},
        {"conversation_id": "c3", "last_message": {"timestamp": _iso(T0)}},
    ]})
    assert [(c.id, c.last_is_mine, c.last_read) for c in convs] == [("c1", True, False), ("c2", True, True), ("c3", None, None)]


async def test_history_flag_is_kept_for_my_messages_and_never_goes_back(monkeypatch):
    state = {"read": False}

    class _Acct:
        async def messages(self, cid):
            return [ChatMessage(ext_id="in1", text="Frage", created_at=T0, direction="in", read=True),
                    ChatMessage(ext_id="out1", text="Antwort", created_at=T0 + timedelta(minutes=1), direction="out",
                                read=state["read"])]

    monkeypatch.setattr(chats, "_account", lambda: _Acct())
    with session() as db:
        db.add(Conversation(id="c1"))
        db.commit()
    await chats.sync_messages("c1")
    rows = _rows()
    assert rows["out1"].read is False and rows["in1"].read is None      # theirs: whether I read it is not stored
    state["read"] = True
    await chats.sync_messages("c1")
    assert _rows()["out1"].read is True
    state["read"] = False                                                 # a stale answer does not unsee it
    await chats.sync_messages("c1")
    assert _rows()["out1"].read is True


async def test_sync_marks_my_messages_seen_when_the_list_says_my_newest_was_read(monkeypatch):
    _chat(("o1", "out", 0), ("i1", "in", 1), ("o2", "out", 2), ("o3", "out", 30))
    state = {"read": False}

    class _Sess:
        started = logged_in = True

    class _Acct:
        async def conversations(self, limit=50):
            return [ConversationSummary(id="c1", ad_id=None, ad_title=None, partner_name="Käufer", unread_count=0,
                                        last_text="o2", last_at=T0 + timedelta(minutes=2), raw={}, role="seller",
                                        last_is_mine=True, last_read=state["read"])]

    async def sync_messages(cid):
        return "full"

    monkeypatch.setattr(chats, "get_session", lambda: _Sess())
    monkeypatch.setattr(chats, "_account", lambda: _Acct())
    monkeypatch.setattr(chats, "sync_messages", sync_messages)
    await chats.sync()
    assert not any(m.read for m in _rows().values())
    state["read"] = True
    await chats.sync()
    rows = _rows()
    assert rows["o1"].read and rows["o2"].read and rows["i1"].read is None
    assert not rows["o3"].read                     # newer than the list's newest message: not seen yet


async def test_their_live_read_events_mark_my_messages_seen_without_a_sync(monkeypatch):
    _chat(("o1", "out", 0), ("o2", "out", 1), ("i1", "in", 2), ("o3", "out", 3), ("o4", "out", 10))
    synced = []

    async def fake_sync(force=False):
        synced.append(1)
        return {}

    monkeypatch.setattr(chats, "sync", fake_sync)
    monkeypatch.setattr(chats, "_live", {"pending": False, "task": None, "last": 0.0})
    seen_at = T0 + timedelta(minutes=5)

    def frame(kind, incoming, **payload):
        return {"type": kind, "conversation_id": "c1", "created_at": _iso(seen_at), "incoming": incoming,
                "payload": {"conversation_id": "c1", "incoming": incoming, **payload}}

    await chats.on_live_event("c1", frame("mark_message_as_read", True, message_id="a-notice-of-theirs"))
    assert not any(m.read for m in _rows().values())            # an id I do not have: nothing of mine was read
    await chats.on_live_event("c1", frame("mark_message_as_read", True, message_id="o2"))
    rows = _rows()
    assert rows["o1"].read and rows["o2"].read and not rows["o3"].read and not rows["o4"].read
    assert rows["o2"].read_at.replace(tzinfo=None) == seen_at.replace(tzinfo=None)
    await chats.on_live_event("c1", frame("mark_inbox_as_read", True))
    rows = _rows()
    assert rows["o3"].read and not rows["o4"].read and rows["i1"].read is None   # o4 came after they read the chat
    assert synced == [] and chats._live["task"] is None         # stored right away, nothing to fetch
    await chats.on_live_event("c1", frame("mark_message_as_read", False, message_id="i1"))   # I read theirs
    assert not _rows()["o4"].read and chats._live["task"] is not None
    chats._live["task"].cancel()


def test_chat_shows_sent_and_seen_ticks_and_the_newest_open_draft():
    from app.main import app

    _chat(("o1", "out", 0), ("i1", "in", 1), ("o2", "out", 2))
    with session() as db:
        o1 = db.exec(select(Message).where(Message.ext_id == "o1")).one()
        o1.read, o1.read_at = True, T0 + timedelta(minutes=1)
        db.add(o1)
        db.add(Draft(conversation_id="c1", text="old draft", needs_info=["Hinweis: stale note"],
                     created_at=T0 + timedelta(minutes=3)))
        db.add(Draft(conversation_id="c1", text="new draft", needs_info=["Hinweis: fresh note"],
                     created_at=T0 + timedelta(minutes=4)))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/chats/c1").text
    assert page.count('data-seen="1"') == 1 and page.count('data-seen="0"') == 1
    assert 'class="chat-footer' in page and page.count('class="chat-footer') == 1
    footer = page.split('class="chat-footer', 1)[1].split("</div>", 1)[0]
    assert "Sent" in footer                                    # the newest of mine (o2) is not seen yet
    assert "Seen " in page                                     # o1: seen, with the time
    assert "fresh note" in page and "new draft" in page
    assert "stale note" not in page and "old draft" not in page
