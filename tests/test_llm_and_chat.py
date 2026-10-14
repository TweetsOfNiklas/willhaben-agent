import sys
from pathlib import Path

import pytest

from app.llm.base import LLMError
from app.llm.claude_cli import ClaudeCLI
from app.llm.prompts import REPLY_SCHEMA, reply_prompt
from app.willhaben.account import parse_conversations, parse_messages

FAKE = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]


async def test_claude_cli_flags_and_stdin(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    cli = ClaudeCLI(cmd=FAKE, model="opus", timeout_s=30)
    cli._token = "sk-ant-oat-test"
    res = await cli.complete(system="SYS", prompt="Hallo Welt", schema=REPLY_SCHEMA)
    argv = res.data["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--bare" not in argv
    assert '"reply"' in argv[argv.index("--json-schema") + 1]
    assert res.data["stdin"] == "Hallo Welt"
    assert res.data["has_token"] is True
    assert res.data["has_api_key"] is False  # subscription only, never the API key
    assert res.model == "claude-opus-5-5"


async def test_claude_cli_images_enable_read_tool(tmp_path):
    img = tmp_path / "bike.JPG"
    img.write_bytes(b"\xff\xd8\xff")
    cli = ClaudeCLI(cmd=FAKE, timeout_s=30)
    res = await cli.complete(system="S", prompt="P", schema={"type": "object"}, images=[img])
    argv = res.data["argv"]
    assert argv[argv.index("--tools") + 1] == "Read"
    assert res.data["cwd_files"] == ["foto1.jpg"]
    assert "./foto1.jpg" in res.data["stdin"]


async def test_claude_cli_error(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_ERROR", "1")
    with pytest.raises(LLMError, match="authenticate"):
        await ClaudeCLI(cmd=FAKE, timeout_s=30).complete(system="S", prompt="P", schema={})


async def test_claude_cli_missing_binary():
    with pytest.raises(LLMError, match="not found"):
        await ClaudeCLI(cmd=["definitely-not-a-claude-binary-xyz"], timeout_s=5).complete(system="S", prompt="P", schema={})


def test_parse_conversations_and_messages():
    data = {"conversations": [
        {"id": "c1", "adId": 123, "adTitle": "Rad", "partnerName": "Max", "unreadCount": 2,
         "lastMessage": {"text": "Noch da?", "createdAt": "2026-10-09T10:00:00+0200"}},
        {"conversationId": "c2", "advert": {"id": 9, "title": "Lampe"}, "partner": {"name": "Eva"}},
        {"nothing": True},
    ]}
    convs = parse_conversations(data)
    assert [c.id for c in convs] == ["c1", "c2"]
    assert convs[0].ad_id == "123" and convs[0].unread_count == 2 and convs[0].last_text == "Noch da?"
    assert convs[0].last_at.hour == 8  # UTC
    assert convs[1].ad_title == "Lampe" and convs[1].partner_name == "Eva"

    msgs = parse_messages({"messages": [
        {"id": "m2", "text": "Ja, noch da", "createdAt": 1791540000000, "senderId": "me-1"},
        {"id": "m1", "text": "Noch da?", "createdAt": 1791530000000, "senderId": "other"},
    ]}, my_ids={"me-1"})
    assert [m.ext_id for m in msgs] == ["m1", "m2"]
    assert [m.direction for m in msgs] == ["in", "out"]


def test_reply_prompt_contains_context():
    p = reply_prompt(role="seller", ad={"title": "KTM Rad", "price": "€ 200", "description": "gut"},
                     partner="Max", messages=[("in", None, "Nehmen Sie 150?")], rules="- max 10 % Rabatt",
                     examples=["Hallo, ja ist noch da."], hint="Gegenangebot 180")
    assert "Ich verkaufe" in p and "KTM Rad" in p and "Partner: Nehmen Sie 150?" in p
    assert "max 10 % Rabatt" in p and "Gegenangebot 180" in p and "Stilbeispiel" in p


def test_parse_conversations_real_shape():
    # Shape of the real chat-api/v1/conversations response (values made up).
    data = {"items": 1, "limit": 50, "offset": 0, "conversations": [{
        "conversation_id": "c0ffee-1", "client_conversation_id": None, "user_uuid": "me-uuid",
        "ad_info": {"id": 1234567890, "uuid": "ad-uuid", "user_id": 42, "user_uuid": "seller-uuid",
                    "subject": "Kinderrad 20 Zoll", "price_eur": "150", "status": "active"},
        "participant_info": {"id": "p1", "user_id": 42, "uuid": "seller-uuid", "name": "Anna"},
        "unseen": 2,
        "last_message": {"id": "m9", "is_mine": False, "read": False, "preview": "Ist es noch da?",
                         "timestamp": "2026-10-08T17:01:02Z", "attachments_count": 0},
        "total_messages_count": 3, "type": "AD", "created_at": "2026-10-08T16:00:00Z",
        "updated_at": "2026-10-08T17:01:02Z"}]}
    [c] = parse_conversations(data)
    assert (c.id, c.ad_id, c.ad_title, c.partner_name, c.unread_count, c.last_text) == (
        "c0ffee-1", "1234567890", "Kinderrad 20 Zoll", "Anna", 2, "Ist es noch da?")
    assert c.last_at.isoformat() == "2026-10-08T17:01:02+00:00"
    assert c.role == "buyer"
    data["conversations"][0]["ad_info"]["user_uuid"] = "me-uuid"
    assert parse_conversations(data)[0].role == "seller"


def test_parse_messages_real_shape():
    data = {"items": 3, "conversation_info": {"conversation_id": "c1"}, "messages": [
        {"message_id": "m1", "type": "integration_message", "text": "PayLivery: sicher bezahlen", "is_mine": False,
         "read": True, "created_at": "2026-10-08T16:00:00Z", "parameters": {}},
        {"message_id": "m2", "type": "text", "text": "Ist es noch da?", "is_mine": False, "read": True,
         "created_at": "2026-10-08T16:01:00Z", "parameters": {}},
        {"message_id": "m3", "type": "text", "text": "Ja", "is_mine": True, "read": False,
         "created_at": "2026-10-08T16:02:00Z", "parameters": {}},
        {"message_id": "m4", "type": "system_message", "text": "Anzeige verkauft", "is_mine": False,
         "read": True, "created_at": "2026-10-08T16:03:00Z", "parameters": {}}]}
    msgs = parse_messages(data, set())
    assert [(m.ext_id, m.direction) for m in msgs] == [("m1", "unknown"), ("m2", "in"), ("m3", "out"), ("m4", "unknown")]


def test_upsert_rekeys_fallback_ids():
    from datetime import datetime, timezone

    from sqlmodel import select

    from app.db import Message, session
    from app.services.chats import _upsert_message

    t = datetime(2026, 10, 8, 16, 1, tzinfo=timezone.utc)
    with session() as db:
        _upsert_message(db, "c1", "0123456789abcdef", "in", "PayLivery-Hinweis", t)   # old hash id, wrong direction
        _upsert_message(db, "c1", "last-abcdef123456", "in", "Ist es noch da?", t)
        db.commit()
    with session() as db:
        _upsert_message(db, "c1", "m1", "unknown", "PayLivery-Hinweis", t, authoritative=True)
        _upsert_message(db, "c1", "m2", "in", "Ist es noch da?", t, authoritative=True)
        _upsert_message(db, "c1", "m3", "in", "Ist es noch da?", t, authoritative=True)  # same text, own row
        db.commit()
        rows = sorted((m.ext_id, m.direction) for m in db.exec(select(Message)))
    assert rows == [("m1", "unknown"), ("m2", "in"), ("m3", "in")]


def test_default_models_per_purpose():
    """Opus only for listings by default; a choice in Settings still wins."""
    from app.db import set_setting
    from app.llm.base import PURPOSES, model_for

    for p in PURPOSES:
        set_setting(PURPOSES[p][0], "")
    assert model_for("ads") == "claude-opus-5-5"
    assert {model_for(p) for p in ("chat", "screen", "search", "shipping", "purchase")} == {"claude-sonnet-5-5"}
    set_setting("llm_model_chat", "claude-haiku-5-5")
    assert model_for("chat") == "claude-haiku-5-5"
    set_setting("llm_model_shipping", "claude-haiku-5-5")
    set_setting("llm_model_purchase", "claude-fable-5-1")
    assert (model_for("shipping"), model_for("purchase"), model_for("chat")) == \
        ("claude-haiku-5-5", "claude-fable-5-1", "claude-haiku-5-5")
    for p in ("chat", "shipping", "purchase"):
        set_setting(f"llm_model_{p}", "")


def test_chat_sync_window():
    from app.db import set_setting
    from app.services.chats import sync_window
    assert sync_window() == (10, None)                 # defaults: 10 latest, no cutoff
    set_setting("chat_sync_limit", "500")
    set_setting("chat_sync_since", "2026-09-01")
    limit, since = sync_window()
    assert limit == 50 and since.date().isoformat() == "2026-09-01" and since.tzinfo is not None
    set_setting("chat_sync_limit", "x")
    set_setting("chat_sync_since", "garbage")
    assert sync_window() == (10, None)


def test_style_examples_are_only_my_own_words():
    """Style examples: messages the owner typed themselves, newest first; never the partner's, not Claude's sent
    drafts (those would teach Claude its own style), 👍-rated drafts only fill up when there are too few."""
    from datetime import datetime, timedelta, timezone

    from app.db import Conversation, Draft, session
    from app.services.chats import _style_examples, _upsert_message

    t0 = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
    with session() as db:
        db.add(Conversation(id="c1", role="seller"))
        _upsert_message(db, "c1", "p1", "in", "Hallo! Ich verstehe, dass Sie sparen müssen. Wäre 40 € ok?", t0)
        _upsert_message(db, "c1", "m1", "out", "passt, 45 geht aber nicht drunter", t0 + timedelta(minutes=1))
        _upsert_message(db, "c1", "m2", "out", "Gerne, der Artikel ist noch verfügbar.", t0 + timedelta(minutes=2))  # sent draft
        _upsert_message(db, "c1", "m3", "out", "ok", t0 + timedelta(minutes=3))                                     # too short
        _upsert_message(db, "c1", "m4", "unknown", "PayLivery-Hinweis für Verkäufer", t0 + timedelta(minutes=4))
        _upsert_message(db, "c1", "m5", "out", "morgen 18h beim billa?", t0 + timedelta(minutes=5))
        db.add(Draft(conversation_id="c1", text="Gerne, der Artikel ist noch verfügbar.", status="sent", sent_at=t0,
                     feedback="up"))
        db.add(Draft(conversation_id="c1", text="Entwurf, nie gesendet", status="draft"))
        db.commit()
    assert _style_examples() == ["morgen 18h beim billa?", "passt, 45 geht aber nicht drunter",
                                 "Gerne, der Artikel ist noch verfügbar."]     # the 👍 draft only fills up at the end
    assert _style_examples(limit=2) == ["morgen 18h beim billa?", "passt, 45 geht aber nicht drunter"]


async def test_ad_context_falls_back_to_my_own_ad(monkeypatch):
    """willhaben blocks the VPN server, so the public ad page is unreachable: for my own ad the asking price and
    description come from the ad editor over the logged-in session, so Claude never has to guess the price."""
    from app.db import Conversation, MyAd, session
    from app.services import chats
    from app.willhaben import insert
    from app.willhaben.public import WillhabenError

    class Public:
        async def get_detail(self, ad_id):
            raise WillhabenError("willhaben hat den VPN-Server abgewiesen")

    async def get_own_ad(s, ad_id):
        assert ad_id == "2092972264"
        return {"heading": "Apple Watch Series 6 44mm", "price": 60, "description": "<p>Top Zustand</p><p>Mit Ladekabel</p>"}

    monkeypatch.setattr(chats, "get_public", lambda: Public())
    monkeypatch.setattr(chats, "get_session", lambda: object())
    monkeypatch.setattr(insert, "get_own_ad", get_own_ad)
    with session() as db:
        db.add(MyAd(ad_id="2092972264", heading="Apple Watch Series 6 44mm", status="active"))
        db.add(Conversation(id="c1", ad_id="2092972264", ad_title="Apple Watch Series 6 44mm", role="seller"))
        db.add(Conversation(id="c2", ad_id="1", ad_title="Fremde Anzeige", role="buyer"))
        db.commit()
    conv = await chats.ensure_ad_context("c1")
    assert conv.ad_context["price"] == "€ 60" and conv.ad_context["description"] == "Top Zustand\n\nMit Ladekabel"
    assert conv.ad_context["source"] == "own_ad" and "paylivery" not in conv.ad_context   # fetched again next time
    other = await chats.ensure_ad_context("c2")                                            # not mine: no editor data
    assert other.ad_context is None


def test_reply_prompt_hint_outranks_everything_and_names_unknown_price():
    from app.llm.prompts import REPLY_SYSTEM
    p = reply_prompt(role="seller", ad={"title": "Apple Watch"}, partner="Max", messages=[("in", None, "letzter Preis?")],
                     rules="- max 10 % Rabatt", examples=[], hint="nenn 55 €")
    assert "Preis: unbekannt" in p and "keinen Betrag" in p
    assert p.index("Vorgabe der Person für genau diese Antwort") < p.index("Aufgabe:") and "nenn 55 €" in p
    assert "Vorrang vor allem anderen" in REPLY_SYSTEM and "[Betrag]" in REPLY_SYSTEM and "wie ein Mensch" in REPLY_SYSTEM
