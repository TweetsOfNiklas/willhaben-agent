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
