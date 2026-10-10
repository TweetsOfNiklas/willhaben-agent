"""PayLivery stage hooks (paylivery.on_stage): fired after a stage change from a notice (scan, both roles) or from an
own successful browser action (_record, ok=True only). Hooks run as background tasks; the tests await them explicitly.
"""
import asyncio
from datetime import timedelta

import pytest

from app.db import Conversation, Message, session, utcnow
from app.services import paylivery


@pytest.fixture
def hooks(monkeypatch):
    """A clean hook list for one test; returns the list of calls the hooks received."""
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])
    calls = []

    async def record(conv, stage, state):
        calls.append((conv.id, conv.role, stage, state.get("stage")))

    paylivery.on_stage(record)
    return calls


async def _settle():
    """Wait for every scheduled hook task (they are fire-and-forget in the code)."""
    while paylivery._hook_tasks:
        await asyncio.gather(*list(paylivery._hook_tasks), return_exceptions=True)


@pytest.fixture
def buyer_chat():
    with session() as db:
        db.add(Conversation(id="cb1", ad_id="77", role="buyer", ad_title="Kinderbuch", partner_name="Max"))
        db.commit()


def _notice(ext_id, text, cid="cb1", minutes_ago=5):
    with session() as db:
        db.add(Message(conversation_id=cid, ext_id=ext_id, direction="unknown", text=text, kind="integration",
                       created_at=utcnow() - timedelta(minutes=minutes_ago)))
        db.commit()


async def test_hook_fires_for_buyer_notice_stage_change(buyer_chat, hooks):
    _notice("n1", "Preisvorschlag wurde angenommen")
    assert await paylivery.scan("cb1") is None            # buyer chats return no shipment, but the state is applied
    await _settle()
    assert hooks == [("cb1", "buyer", "offer_accepted", "offer_accepted")]
    with session() as db:
        assert db.get(Conversation, "cb1").paylivery["stage"] == "offer_accepted"


async def test_hook_gets_the_chat_and_the_new_state(buyer_chat, monkeypatch):
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])
    got = []

    async def capture(conv, stage, state):
        got.append((conv, stage, state))

    paylivery.on_stage(capture)
    _notice("n1", "Preisvorschlag wurde abgelehnt")
    await paylivery.scan("cb1")
    await _settle()
    (conv, stage, state), = got
    assert isinstance(conv, Conversation) and conv.id == "cb1" and conv.ad_id == "77"
    assert stage == "offer_declined" and state["stage"] == "offer_declined" and state["role"] == "buyer"
    assert state["applied"] == ["n1"]


async def test_same_stage_again_does_not_fire(buyer_chat, hooks):
    _notice("n1", "Preisvorschlag wurde angenommen", minutes_ago=10)
    _notice("n2", "Preisvorschlag ist angenommen", minutes_ago=5)     # same stage, second notice
    await paylivery.scan("cb1")
    await _settle()
    assert len(hooks) == 1
    await paylivery.scan("cb1")                                       # already applied: nothing again
    await _settle()
    assert len(hooks) == 1


async def test_failing_hook_is_logged_and_does_not_block_others(buyer_chat, monkeypatch, caplog):
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])
    calls = []

    async def broken(conv, stage, state):
        raise RuntimeError("boom")

    async def good(conv, stage, state):
        calls.append(stage)

    paylivery.on_stage(broken)
    paylivery.on_stage(good)
    _notice("n1", "Preisvorschlag wurde angenommen")
    with caplog.at_level("WARNING", logger="app.services.paylivery"):
        assert await paylivery.scan("cb1") is None                    # the sync is not interrupted
        await _settle()
    assert calls == ["offer_accepted"]
    assert any("stage hook" in r.getMessage() and "boom" in r.getMessage() for r in caplog.records)
    with session() as db:
        assert db.get(Conversation, "cb1").paylivery["stage"] == "offer_accepted"


async def test_record_success_fires_hook(buyer_chat, hooks):
    paylivery._record("cb1", "offer", True, "Vorschlag gesendet", 80.0)
    await _settle()
    assert hooks == [("cb1", "buyer", "offer_sent", "offer_sent")]
    with session() as db:
        st = db.get(Conversation, "cb1").paylivery
    assert st["stage"] == "offer_sent" and st["offer"] == 80.0


async def test_record_failure_does_not_fire_hook(buyer_chat, hooks):
    paylivery._record("cb1", "offer", False, "Feld nicht gefunden", None)
    await _settle()
    assert hooks == []
    with session() as db:
        st = db.get(Conversation, "cb1").paylivery or {}
    assert "stage" not in st and st["history"][-1]["text"].startswith("offer:")


async def test_record_hook_error_keeps_the_state(buyer_chat, monkeypatch):
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])

    async def broken(conv, stage, state):
        raise RuntimeError("boom")

    paylivery.on_stage(broken)
    paylivery._record("cb1", "offer", True, "ok", 5.0)
    await _settle()
    with session() as db:
        assert db.get(Conversation, "cb1").paylivery["stage"] == "offer_sent"


def test_record_without_event_loop_skips_hooks_quietly(buyer_chat, monkeypatch):
    """Sync callers (no running loop) do not crash; their hooks are not scheduled."""
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])
    calls = []

    async def hook(conv, stage, state):
        calls.append(stage)

    paylivery.on_stage(hook)
    paylivery._record("cb1", "offer", True, "Vorschlag gesendet", 80.0)
    assert calls == [] and not paylivery._hook_tasks
    with session() as db:
        assert db.get(Conversation, "cb1").paylivery["stage"] == "offer_sent"


async def test_on_stage_registers_once(monkeypatch):
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])

    async def hook(conv, stage, state):
        return None

    assert paylivery.on_stage(hook) is hook
    paylivery.on_stage(hook)
    assert paylivery.STAGE_HOOKS == [hook]
