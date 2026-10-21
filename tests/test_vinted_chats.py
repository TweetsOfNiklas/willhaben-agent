"""Vinted chats in the chat list: the inbox and conversation answers of Vinted's web app (shapes as recorded, made-up
content) become Conversation and Message rows; sending is refused for now."""
import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Message, session
from app.platforms import vinted_inbox
from app.services import chats

INBOX = {"conversations": [{
    "id": "01a1-uuid", "conversation_type": "order", "created_at": "2026-10-20T10:00:00+02:00",
    "is_unread_by_current_user": True,
    "data": {"inventory_items": [{"id": 555, "title": "HomePod mini", "photos": []}]},
    "last_message": {"id": "m2", "created_at": "2026-10-21T12:00:00+02:00", "sender_id": "77",
                     "message_type": "legacy_last_message", "data": {"content": "Ist versendet!"}},
    "opposite_users": [{"type": "user", "data": {"id": 77, "login_name": "verkaeufer_x"}}]}],
    "pagination_temp": {"has_next": False}}
CONV = {"id": "01a1-uuid", "allow_reply": True, "data": {"order_id": "999"},
        "opposite_users": [{"type": "user", "data": {"id": 77, "login_name": "verkaeufer_x"}}],
        "messages": [
            {"id": "e1", "created_at": "2026-10-20T10:00:00+02:00", "sender_id": "1", "message_type": "legacy_action",
             "data": {"entity": {"title": "Bestellung bezahlt", "subtitle": "Der Verkäufer verschickt sie."}}},
            {"id": "m1", "created_at": "2026-10-20T11:00:00+02:00", "sender_id": "5", "message_type": "message",
             "data": {"body": "Danke, wann schickst du?"}},
            {"id": "m2", "created_at": "2026-10-21T12:00:00+02:00", "sender_id": "77", "message_type": "message",
             "data": {"body": "Ist versendet!"}}]}


def test_inbox_and_conversation_become_rows():
    assert vinted_inbox.apply_inbox(INBOX) == ["01a1-uuid"]
    with session() as db:
        c = db.get(Conversation, "01a1-uuid")
    assert c.platform == "vinted" and c.ad_id == "555" and c.partner_name == "verkaeufer_x" and c.unread_count == 1
    assert c.last_message_text == "Ist versendet!" and c.ad_context["url"] == "https://www.vinted.at/items/555"
    vinted_inbox.apply_conversation("01a1-uuid", CONV)
    msgs = chats.messages_of("01a1-uuid")
    assert [(m.direction, m.kind, m.text) for m in msgs] == [
        ("unknown", "integration", "Bestellung bezahlt – Der Verkäufer verschickt sie."),
        ("out", "text", "Danke, wann schickst du?"), ("in", "text", "Ist versendet!")]
    assert vinted_inbox.apply_inbox(INBOX) == []                    # nothing new: not opened again


async def test_sending_on_vinted_goes_through_its_page_and_keeps_vinteds_id(monkeypatch):
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        d = chats.Draft(conversation_id="01a1-uuid", text="Danke!")
        db.add(d)
        db.commit()
        db.refresh(d)
    sent = []

    async def send(cid, text):
        sent.append((cid, text))
        return {"id": "m9", "created_at": "2026-10-21T22:00:00+02:00", "message_type": "reply_plain",
                "data": {"content": text}}
    monkeypatch.setattr(vinted_inbox, "send", send)
    marked = []

    async def no_read(cid):
        marked.append(cid)
    monkeypatch.setattr(chats, "mark_read", no_read)
    out = await chats.send_draft(d.id, "Danke!")
    assert out.status == "sent" and sent == [("01a1-uuid", "Danke!")] and marked == []      # no willhaben receipt
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [
        {"id": "m9", "created_at": "2026-10-21T22:00:00+02:00", "sender_id": "5", "message_type": "reply_plain",
         "data": {"content": "Danke!"}}]})
    assert [m.text for m in chats.messages_of("01a1-uuid")].count("Danke!") == 1                # no double


async def test_an_unconfirmed_vinted_send_keeps_the_draft(monkeypatch):
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        d = chats.Draft(conversation_id="01a1-uuid", text="Danke!")
        db.add(d)
        db.commit()
        db.refresh(d)

    async def send(cid, text):
        raise vinted_inbox.SendError("Vinted answered HTTP 403")
    monkeypatch.setattr(vinted_inbox, "send", send)
    with pytest.raises(chats.SendFailed, match="Vinted"):
        await chats.send_draft(d.id, "Danke!")
    with session() as db:
        row = db.get(chats.Draft, d.id)
    assert row.status == "draft" and "403" in row.error
    assert await chats.mark_read("01a1-uuid") == 0


def test_the_chat_page_links_to_vinted():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", CONV)
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats/01a1-uuid").text
        assert 'href="https://www.vinted.at/inbox/01a1-uuid"' in page and "Ist versendet!" in page
        assert "Vinted" in c.get("/chats").text


async def test_sync_reads_inbox_then_changed_conversations(monkeypatch):
    import json

    class Sess:
        started = True
        opened = []

        async def read_hidden(self, url, wait_for, timeout_ms=0, keep=None, allow=None, deny=None):
            self.opened.append(url)
            assert allow("POST", "https://www.vinted.at/anything")         # Vinted's own page: nothing blocked
            body = INBOX if url.endswith("/inbox") else CONV
            key = "https://api.vinted.at/messaging/main/inbox" if url.endswith("/inbox") else \
                "https://api.vinted.at/messaging/main/conversations/01a1-uuid"
            return {"text": "", "found": True, "requests": [], "bodies": {key: json.dumps(body)}}
    sess = Sess()
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: sess)
    res = await vinted_inbox.sync(force=True)
    assert res == {"conversations": 1, "opened": 1}
    assert sess.opened == ["https://www.vinted.at/inbox", "https://www.vinted.at/inbox/01a1-uuid"]
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").messages_synced_at is not None
        assert len(list(db.exec(Message.__table__.select()))) == 3


def test_the_chat_list_filters_by_site():
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Conversation(id="wh-1", partner_name="wh_partner", ad_title="Router"))
        db.commit()
    from app.main import app
    with TestClient(app) as c:
        both = c.get("/chats").text
        only_v = c.get("/chats?site=vinted").text
        only_w = c.get("/chats?site=willhaben").text
    assert "data-chat-site" in both and "verkaeufer_x" in both and "wh_partner" in both
    assert "verkaeufer_x" in only_v and "wh_partner" not in only_v
    assert "wh_partner" in only_w and "verkaeufer_x" not in only_w


def test_the_side_of_an_items_conversation_is_the_role():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "data": {"current_user_side": "buyer", "items": []}})
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").role == "buyer"
