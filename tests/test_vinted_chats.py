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
    "data": {"inventory_items": [{"id": 555, "title": "HomePod mini", "photos": [{"url": "https://img/inbox.webp"}]}]},
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
        ("out", None, "Danke, wann schickst du?"), ("in", None, "Ist versendet!")]
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
        return {"id": "uuid-9", "created_at": "2026-10-21T22:00:00+02:00", "message_type": "reply_plain",
                "data": {"content": text, "id": "m9"}}
    monkeypatch.setattr(vinted_inbox, "send", send)
    marked = []

    async def no_read(cid):
        marked.append(cid)
    monkeypatch.setattr(chats, "mark_read", no_read)
    out = await chats.send_draft(d.id, "Danke!")
    assert out.status == "sent" and sent == [("01a1-uuid", "Danke!")] and marked == []      # no willhaben receipt
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [
        {"id": "x-uuid", "created_at": "2026-10-21T22:00:00+02:00", "sender_id": "5", "message_type": "legacy_reply",
         "data": {"entity_type": "message", "entity": {"id": "m9", "body": "Danke!"}}}]})
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
    sess = Sess()
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: sess)

    async def catch(url, part, timeout_ms=0, extra=None):
        sess.opened.append(url)
        return json.loads(json.dumps(INBOX if url.endswith("/inbox") else CONV))
    monkeypatch.setattr(vinted_inbox, "_catch", catch)
    res = await vinted_inbox.sync(force=True)
    assert res == {"conversations": 1, "opened": 1}
    assert sess.opened == ["https://www.vinted.at/inbox", "https://www.vinted.at/inbox/01a1-uuid"]
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").messages_synced_at is not None
        assert len(list(db.exec(Message.__table__.select()))) == 3


def test_the_chat_list_filters_by_site():
    from app.db import set_setting
    set_setting("platform_vinted_enabled", "1")
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


async def test_a_new_message_from_the_other_side_is_pushed(monkeypatch):
    import copy
    import json

    from app.db import set_setting
    set_setting("notify_messages", "1")
    pushes = []

    async def notify(kind, title, message, **kw):
        pushes.append((kind, title, message))
    monkeypatch.setattr("app.services.notify.notify", notify)
    state = {"inbox": copy.deepcopy(INBOX)}

    class Sess:
        started = True
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: Sess())

    async def catch(url, part, timeout_ms=0, extra=None):
        return json.loads(json.dumps(state["inbox"] if url.endswith("/inbox") else CONV))
    monkeypatch.setattr(vinted_inbox, "_catch", catch)
    await vinted_inbox.sync(force=True)
    assert pushes == []                                                  # the first sync: no news
    state["inbox"]["conversations"][0]["last_message"] = {
        "id": "m3", "created_at": "2026-10-21T23:00:00+02:00", "sender_id": "77",
        "message_type": "legacy_last_message", "data": {"content": "Noch Fragen?"}}
    await vinted_inbox.sync(force=True)
    assert pushes == [("message", "Vinted: message from verkaeufer_x", "HomePod mini: Noch Fragen?")]



async def test_live_events_and_the_reloaded_inbox(monkeypatch):
    import json

    opened = []

    async def sync_one(cid):
        opened.append(cid)
    monkeypatch.setattr(vinted_inbox, "sync_one", sync_one)
    await vinted_inbox._on_live(json.dumps({"_state": "open", "status": 200}))
    assert vinted_inbox.live["state"] == "connected"
    await vinted_inbox._on_live(json.dumps({"message_type": "refresh_conversation",
                                            "data": {"conversation_id": "01a1-uuid", "user_msg_thread_id": 5}}))
    await vinted_inbox._on_live(json.dumps({"message_type": "refresh_inbox", "data": {}}))
    assert opened == ["01a1-uuid"]

    class Resp:
        url = "https://api.vinted.at/messaging/main/inbox?page=1"
        status = 200
        request = type("Q", (), {"method": "GET"})()

        async def text(self):
            return json.dumps(INBOX)
    await vinted_inbox._on_live_response(Resp())
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").partner_name == "verkaeufer_x"


def test_a_site_not_set_up_does_not_show():
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats").text                      # no chats, not logged in anywhere, Vinted off
    assert "data-vinted-live" not in page and "data-chat-site" not in page and "Not logged in to willhaben" not in page


def test_the_live_lines_name_their_site():
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Conversation(id="wh-1", partner_name="wh_partner"))
        db.commit()
    vinted_inbox.live.update(state="connected")
    with TestClient(app) as c:
        page = c.get("/chats").text
    live = page[page.index('id="chat-live-state"'):page.index('id="chat-filters"')]
    assert 'data-live-site="willhaben" data-live="0"' in live and "text-error" in live      # not live in tests
    assert 'data-live-site="vinted" data-vinted-live data-live="1"' in live and "text-success" in live
    assert ">offline</span>" in live and ">online</span>" in live
    vinted_inbox.live.update(state="off")


def test_a_legacy_reply_is_a_written_message():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [
        {"id": "r1", "created_at": "2026-10-21T21:56:44+02:00", "sender_id": "5", "message_type": "legacy_reply",
         "data": {"entity_type": "message", "created_at_ts": "x", "entity": {"body": "Ist das noch da?", "id": 1,
                                                                                "user_id": 5, "photos": []}}},
        {"id": "r2", "created_at": "2026-10-21T22:30:00+02:00", "sender_id": "77", "message_type": "legacy_reply",
         "data": {"entity_type": "message", "entity": {"body": "Ja!", "id": 2, "user_id": 77}}}]})
    assert [(m.direction, m.kind, m.text) for m in chats.messages_of("01a1-uuid")] == [
        ("out", None, "Ist das noch da?"), ("in", None, "Ja!")]


async def test_the_item_card_comes_from_vinteds_item_page(monkeypatch):
    from app import platforms
    from app.platforms.types import AdDetail
    vinted_inbox.apply_inbox(INBOX)
    asked = []

    class V:
        async def detail(self, ad_id, url=None):
            asked.append((ad_id, url))
            return AdDetail(ad_id=ad_id, heading="HomePod mini", description="Wie neu", price=50.0,
                            price_display="€ 50,00", status="sold", published_at=None, first_published_at=None,
                            end_date=None, url="https://www.vinted.at/items/555", images=["https://img/1.webp"],
                            platform="vinted")
    real_get = platforms.get
    monkeypatch.setattr(platforms, "get", lambda pid: V() if pid == "vinted" else real_get(pid))
    conv = await chats.ensure_ad_context("01a1-uuid")
    assert asked == [("555", "https://www.vinted.at/items/555")]
    assert conv.ad_context["price"] == "€ 50,00" and conv.ad_context["images"] == ["https://img/1.webp"]
    assert conv.ad_context["platform"] == "vinted"
    await chats.ensure_ad_context("01a1-uuid")
    assert len(asked) == 1                                               # fresh: not asked again
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    with TestClient(app) as c:
        page = c.get("/chats/01a1-uuid").text
    assert 'src="https://img/1.webp"' in page and "€ 50,00" in page


def test_each_chat_carries_its_sites_coloured_mark():
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Conversation(id="wh-1", partner_name="wh_partner"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/chats").text
    assert 'data-site-mark="vinted"' in page and 'data-site-mark="willhaben"' in page
    assert "color: #0f9fa8" in page and "color: #2f7fd8" in page


async def test_a_sold_items_card_keeps_the_inbox_photo(monkeypatch):
    from app import platforms
    from app.platforms.types import AdDetail
    vinted_inbox.apply_inbox(INBOX)

    class V:
        async def detail(self, ad_id, url=None):
            return AdDetail(ad_id=ad_id, heading="HomePod mini", description="", price=50.0, price_display="50 €",
                            status="sold", published_at=None, first_published_at=None, end_date=None, url=url,
                            images=[], platform="vinted")
    real_get = platforms.get
    monkeypatch.setattr(platforms, "get", lambda pid: V() if pid == "vinted" else real_get(pid))
    conv = await chats.ensure_ad_context("01a1-uuid")
    assert conv.ad_context["images"] == ["https://img/inbox.webp"] and conv.ad_context["status"] == "sold"


def test_an_orders_side_comes_from_the_order():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", CONV, {"escrow_order": {"current_user_side": "buyer"}})
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").role == "buyer"


def test_no_side_no_tag():
    vinted_inbox.apply_inbox(INBOX)
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    with TestClient(app) as c:
        page = c.get("/chats").text
    row = page[page.index("verkaeufer_x"):][:900]
    assert "Selling" not in row and "Buying" not in row


def test_an_event_with_a_new_id_on_every_load_is_stored_once():
    vinted_inbox.apply_inbox(INBOX)
    ev = {"created_at": "2026-10-21T18:44:03+02:00", "sender_id": "1", "message_type": "legacy_action",
          "data": {"event_type": "success", "entity_type": "action_message", "entity": {"title": "Gekauft!", "subtitle": "x"}}}
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [{**ev, "id": "a1"}]})
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [{**ev, "id": "b2"}]})
    assert [m.text for m in chats.messages_of("01a1-uuid")] == ["Gekauft! – x"]


def test_old_unstable_rows_are_replaced():
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Message(conversation_id="01a1-uuid", ext_id="vinted-oldid", direction="unknown", text="Gekauft! – x",
                       kind="integration"))
        db.commit()
    ev = {"id": "c3", "created_at": "2026-10-21T18:44:03+02:00", "sender_id": "1", "message_type": "legacy_action",
          "data": {"event_type": "success", "entity": {"title": "Gekauft!", "subtitle": "x"}}}
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [ev]})
    assert [m.ext_id.startswith("vinted-ev-") for m in chats.messages_of("01a1-uuid")] == [True]
