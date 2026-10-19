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
    assert {model_for(p) for p in ("chat", "screen", "search", "shipping", "purchase", "style")} == {"claude-sonnet-5-5"}
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
    set_setting("chat_sync_days", "14")
    limit, since = sync_window()
    from datetime import date, timedelta
    assert limit == 50 and since.date() == date.today() - timedelta(days=14) and since.tzinfo is not None
    assert (since.hour, since.minute) == (0, 0)                       # local midnight
    set_setting("chat_sync_days", "0")
    assert sync_window()[1].date() == date.today()                    # today only
    set_setting("chat_sync_limit", "x")
    set_setting("chat_sync_days", "garbage")
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
    assert conv.ad_context["price"] == "€ 60" and conv.ad_context["description"] == "Top Zustand\nMit Ladekabel"   # as willhaben shows it
    assert conv.ad_context["source"] == "own_ad" and "paylivery" not in conv.ad_context   # fetched again next time
    other = await chats.ensure_ad_context("c2")                                            # not mine: no editor data
    assert other.ad_context is None


async def test_ad_context_follows_price_changes_and_reload(monkeypatch):
    """The ad's data is kept for AD_CONTEXT_MAX_AGE, not forever: a price change, reservation or sale on willhaben
    reaches the next draft; the chat's Reload fetches at once, also for an ad stored as gone. A refresh that fails
    keeps the data from before."""
    from datetime import timedelta
    from types import SimpleNamespace

    from app.db import Conversation, session, utcnow
    from app.services import chats
    from app.willhaben.public import WillhabenError

    state = {"price": "€ 60", "gone": False, "fail": False, "calls": 0}

    class Public:
        async def get_detail(self, ad_id):
            state["calls"] += 1
            if state["fail"]:
                raise WillhabenError("willhaben hat den VPN-Server abgewiesen")
            if state["gone"]:
                return None
            return SimpleNamespace(seller_login_id="42", seller_uuid="u-42",
                                   as_context=lambda: {"title": "Apple Watch", "price": state["price"], "paylivery": True})

    def age(minutes):
        with session() as db:
            c = db.get(Conversation, "c1")
            c.ad_context_at = utcnow() - timedelta(minutes=minutes)
            db.add(c)
            db.commit()

    async def context(force=False):
        return (await chats.ensure_ad_context("c1", force=force)).ad_context

    monkeypatch.setattr(chats, "get_public", lambda: Public())
    monkeypatch.setattr(chats, "get_session", lambda: SimpleNamespace(user={"id": 42}))
    with session() as db:       # stored before contexts carried a time (and when they were kept forever)
        db.add(Conversation(id="c1", ad_id="2092972264", ad_title="Apple Watch",
                            ad_context={"title": "Apple Watch", "price": "€ 99", "paylivery": True}))
        db.commit()
    conv = await chats.ensure_ad_context("c1")
    assert conv.ad_context["price"] == "€ 60" and conv.role == "seller" and conv.ad_context_at is not None
    state["price"] = "€ 50"
    assert (await context())["price"] == "€ 60" and state["calls"] == 1              # fresh: kept
    age(31)
    assert (await context())["price"] == "€ 50" and state["calls"] == 2              # older: fetched again
    state["price"] = "€ 40"
    assert (await context(force=True))["price"] == "€ 40" and state["calls"] == 3    # Reload: at once

    state["fail"] = True
    age(31)
    ctx = await context()
    assert ctx["price"] == "€ 40" and "source" not in ctx and state["calls"] == 4    # kept, not the editor fallback
    assert (await context())["price"] == "€ 40" and state["calls"] == 5              # tried again next time

    state["fail"], state["gone"] = False, True
    assert await context() == {"title": "Apple Watch", "status": "nicht mehr online"} and state["calls"] == 6
    age(120)
    assert (await context())["status"] == "nicht mehr online" and state["calls"] == 6    # gone stays gone ...
    state["gone"] = False
    assert (await context(force=True))["price"] == "€ 40" and state["calls"] == 7        # ... until Reload asks


def test_reply_prompt_hint_outranks_everything_and_names_unknown_price():
    from app.llm.prompts import REPLY_SYSTEM
    p = reply_prompt(role="seller", ad={"title": "Apple Watch"}, partner="Max", messages=[("in", None, "letzter Preis?")],
                     rules="- max 10 % Rabatt", examples=[], hint="nenn 55 €")
    assert "Preis: unbekannt" in p and "keinen Betrag" in p
    assert p.index("Vorgabe der Person für genau diese Antwort") < p.index("Aufgabe:") and "nenn 55 €" in p
    assert "Vorrang vor allem anderen" in REPLY_SYSTEM and "[Betrag]" in REPLY_SYSTEM and "wie ein Mensch" in REPLY_SYSTEM


def test_reply_prompt_style_profile_and_other_chats_blocks():
    """The style profile (Settings → My rules → Writing style) and what I told the other buyers of the same ad go into
    the prompt as their own blocks; without them nothing is added."""
    from app.llm.prompts import MAX_OTHERS
    base = dict(role="seller", ad={"title": "Mellanox ConnectX-3"}, partner="Max", messages=[("in", None, "Wieviele hast du?")],
                rules="- Du-Form", examples=["passt, 45 geht aber nicht drunter"], hint=None)
    p = reply_prompt(**base, style="- kurz, Du, kein Gruß\n- kleingeschrieben", others=["hab nur die eine karte", "ja funktioniert"])
    assert "Mein Schreibstil (aus meinen eigenen Nachrichten abgeleitet" in p and "- kleingeschrieben" in p
    assert "Was ich anderen Interessenten zu dieser Anzeige geschrieben habe" in p and "- hab nur die eine karte" in p
    assert "nicht weitergeben" in p and "die anderen Chats nicht erwähnen" in p
    assert p.index("Verlauf (älteste zuerst):") < p.index("Was ich anderen Interessenten") < p.index("Meine Regeln:")
    assert p.index("Mein Schreibstil") < p.index("Nachrichten, die ich selbst in anderen Chats")
    plain = reply_prompt(**base)
    assert "Mein Schreibstil" not in plain and "Was ich anderen Interessenten" not in plain
    many = reply_prompt(**base, others=[f"fakt nummer {i}" for i in range(12)])
    assert many.count("- fakt nummer") == MAX_OTHERS


def test_others_said_are_my_own_facts_from_the_ads_other_chats():
    """chats._others_said: my own messages of the last 30 days in the other seller chats of the ad (every copy of a
    renewed ad), newest first, never this chat, never the partner's words, never willhaben notices, never another
    ad's chats, never a sold/reserved notice (availability comes from the ad) and never a line with contact data."""
    from datetime import timedelta

    from app.db import Conversation, Draft, MyAd, session, utcnow
    from app.services.chats import _others_said, _upsert_message

    t0 = utcnow() - timedelta(hours=2)
    with session() as db:
        db.add(MyAd(ad_id="111", heading="Mellanox", replaced_by="333"))
        db.add(MyAd(ad_id="333", heading="Mellanox (renewed)"))
        db.add(MyAd(ad_id="222", heading="Lampe"))
        for cid, ad, role in (("c1", "111", "seller"), ("c2", "111", "seller"), ("c3", "333", "seller"),
                              ("c4", "222", "seller"), ("c5", "111", "buyer")):
            db.add(Conversation(id=cid, ad_id=ad, role=role))
        _upsert_message(db, "c1", "a1", "out", "das ist mein eigener chat, zählt nicht", t0)
        _upsert_message(db, "c2", "b1", "in", "Funktioniert die Karte?", t0 + timedelta(minutes=1))
        _upsert_message(db, "c2", "b2", "out", "ja funktioniert, hab sie bis gestern im server gehabt", t0 + timedelta(minutes=2))
        _upsert_message(db, "c2", "b3", "out", "ok", t0 + timedelta(minutes=3))                       # too short
        _upsert_message(db, "c2", "b4", "unknown", "PayLivery-Hinweis für Verkäufer", t0 + timedelta(minutes=4))
        _upsert_message(db, "c3", "d1", "out", "hab nur die eine karte", t0 + timedelta(minutes=5))   # the renewed copy
        _upsert_message(db, "c4", "e1", "out", "die lampe ist noch da", t0 + timedelta(minutes=6))    # another ad
        _upsert_message(db, "c5", "f1", "out", "ich würde die karte selbst kaufen", t0 + timedelta(minutes=7))   # me as buyer
        _upsert_message(db, "c2", "b5", "out", "vor langer zeit geschrieben, zählt nicht mehr", t0 - timedelta(days=45))
        _upsert_message(db, "c2", "b6", "out", "lieferadresse: hauptstraße 5, 1100 wien, tel 0664 1234567", t0 - timedelta(minutes=5))
        reserved = "Hallo, danke für dein Interesse! Der Artikel ist gerade reserviert."
        _upsert_message(db, "c3", "d0", "out", reserved, t0 - timedelta(minutes=1))
        db.add(Draft(conversation_id="c3", text=reserved, status="sent", intent="reserved_notice", sent_at=t0))
        db.commit()
    assert _others_said("c1", "111") == ["hab nur die eine karte", "ja funktioniert, hab sie bis gestern im server gehabt"]
    assert _others_said("c1", "111", limit=1) == ["hab nur die eine karte"]
    assert _others_said("c2", "111") == ["hab nur die eine karte", "das ist mein eigener chat, zählt nicht"]
    assert _others_said("c1", None) == [] and _others_said("c4", "222") == []


def test_style_examples_put_the_profiles_examples_first():
    """style.examples(): the characteristic messages chosen with the profile, then the newest own messages, each once,
    capped at MAX_STYLE_EXAMPLES."""
    import json
    from datetime import datetime, timedelta, timezone

    from app.db import Conversation, session, set_setting
    from app.services import style
    from app.services.chats import _upsert_message

    t0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    with session() as db:
        db.add(Conversation(id="c1", role="seller"))
        for i in range(10):
            _upsert_message(db, "c1", f"m{i}", "out", f"eigene nachricht nummer {i}", t0 + timedelta(minutes=i))
        db.commit()
    assert style.profile() == "" and style.meta() == {} and style.examples()[0] == "eigene nachricht nummer 9"
    set_setting("reply_style", "  - kurz und per Du\n")
    set_setting("reply_style_meta", json.dumps({"at": "2026-10-18T10:00:00", "count": 10,
                                                "examples": ["eigene nachricht nummer 2", " passt so ", "", 7]}))
    assert style.profile() == "- kurz und per Du" and style.meta()["count"] == 10
    ex = style.examples()
    assert ex[0] == "eigene nachricht nummer 2" and len(ex) == 8 and len(set(ex)) == 8     # "passt so" is no message of mine
    assert ex[1] == "eigene nachricht nummer 9" and "eigene nachricht nummer 2" not in ex[1:]
    set_setting("reply_style", "")
    assert style.examples()[0] == "eigene nachricht nummer 9"           # profile cleared: the derived examples go with it
    set_setting("reply_style", "- kurz und per Du")
    set_setting("reply_style_meta", "not json")
    assert style.meta() == {} and style.panel()["profile"] == "- kurz und per Du" and style.panel()["meta"] == {}


def test_the_chat_settings_save_the_days():
    from fastapi.testclient import TestClient

    from app.db import get_setting
    from app.main import app

    client = TestClient(app)

    page = client.get("/settings/chats").text
    assert 'name="chat_sync_days"' in page and 'type="date"' not in page
    client.post("/settings", data={"section": "chats", "chat_sync_limit": "10", "chat_sync_days": "30"})
    assert get_setting("chat_sync_days") == "30"
    client.post("/settings", data={"section": "chats", "chat_sync_limit": "10", "chat_sync_days": "abc"})
    assert get_setting("chat_sync_days") == "30"                      # not a number: kept
    client.post("/settings", data={"section": "chats", "chat_sync_limit": "10", "chat_sync_days": ""})
    assert get_setting("chat_sync_days") == ""


def test_chats_older_than_the_window_leave_the_list():
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app.db import Conversation, session, set_setting, utcnow
    from app.main import app

    with session() as db:
        db.add(Conversation(id="new1", ad_title="Neues Gespräch", partner_name="A", last_message_at=utcnow()))
        db.add(Conversation(id="old1", ad_title="Altes Gespräch", partner_name="B",
                            last_message_at=utcnow() - timedelta(days=120)))
        db.commit()
    client = TestClient(app)
    assert "Altes Gespräch" in client.get("/chats").text                  # no limit set
    set_setting("chat_sync_days", "90")
    page = client.get("/chats").text
    assert "Neues Gespräch" in page and "Altes Gespräch" not in page and "Show 1 hidden or older chats" in page
    assert "Altes Gespräch" in client.get("/chats?show_hidden=1").text


def test_reply_system_answers_what_was_asked_and_keeps_rules_as_boundaries():
    """The two failures of 2026-10-18: a draft asked „Hast du noch Interesse?“ after the buyer wrote „Hätte Interesse!“,
    and „Ja, sie funktioniert. Ist aber ein Privatverkauf, also ohne Garantie oder Rücknahme.“ recited a rule nobody
    asked about. The system prompt now says what to answer, that rules are boundaries, how far a hint binds and that
    alternatives name the decision only; the automatic and the buyer prompts inherit it."""
    from app.db import DEFAULT_SETTINGS
    from app.llm.prompts import AUTO_REPLY_SYSTEM, PURCHASE_SYSTEM, REPLY_SCHEMA, REPLY_SYSTEM

    for text in ("Antworte auf das, was der Partner zuletzt geschrieben hat, und nicht mehr",
                 "fragst du nicht noch einmal danach",
                 "Eine Schlussfrage stellst du nur, wenn der nächste Schritt die Entscheidung des Partners braucht",
                 "sind aber Grenzen und kein\n  Text zum Aufsagen",
                 "Garantie, Rücknahme, Privatverkauf oder „wie gesehen“ schreibst du nie von\n  dir aus",
                 "Abweichend davon nennst du Abholung, Versand und Zahlungsweg (auch PayLivery)",
                 "Vorrang vor allem anderen", "setzt du genau so um, wie sie dastehen",
                 "nur die Formulierung folgt weiter den Regeln hier",
                 "instruction, die nur die Entscheidung\n  in einem Satz nennt",
                 "keine Garantie- oder\n  Privatverkaufshinweise, keine schon beantworteten Fragen"):
        assert text in REPLY_SYSTEM, text
        assert text in AUTO_REPLY_SYSTEM and text in PURCHASE_SYSTEM, text
    assert "ein Stichwort reicht nicht" not in REPLY_SYSTEM      # a typed „ohne Garantie“ stays binding (chats.revise)
    assert "Automatischer Modus: Deine Antwort wird ohne weitere Prüfung gesendet" in AUTO_REPLY_SYSTEM   # the .replace() still hits
    desc = REPLY_SCHEMA["properties"]["alternatives"]["items"]["properties"]["instruction"]["description"]
    assert desc.startswith("Nur die Entscheidung in einem kurzen Satz") and "keine schon beantworteten Fragen" in desc
    first = DEFAULT_SETTINGS["reply_rules"].splitlines()[0]
    assert first == "- Privatverkauf, daher keine Garantie oder Rücknahme (nur erwähnen, wenn jemand danach fragt)."
