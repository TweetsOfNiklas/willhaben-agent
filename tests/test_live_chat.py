import asyncio
import json

from app.services import chats
from app.willhaben.session import BrowserSession


async def test_frames_routed_by_conversation():
    s, got = BrowserSession(), []

    async def cb(cid, frame):
        got.append((cid, frame.get("type")))

    s._chat_cb = cb
    await s._on_chat_frame(json.dumps({"_state": "open"}))
    assert s.chat_live["state"] == "connected" and s.chat_live_ok()
    await s._on_chat_frame(json.dumps({"req_type": "ping", "status": "success"}))
    await s._on_chat_frame(json.dumps({"type": "update_presence", "payload": {"user_uuid": "u"}}))
    await s._on_chat_frame(json.dumps({"type": "send_message", "conversation_id": "c1", "payload": {"text": "hi"}}))
    await s._on_chat_frame(json.dumps({"type": "x", "payload": {"conversation_id": "c2"}}))
    await s._on_chat_frame("not json")
    assert got == [("c1", "send_message"), ("c2", "x")] and s.chat_live["events"] == 2
    await s._on_chat_frame(json.dumps({"_state": "closed", "code": 1006}))
    assert s.chat_live["state"] == "getrennt (1006)" and not s.chat_live_ok()


async def test_live_events_coalesce_into_few_syncs(monkeypatch):
    calls = []

    async def fake_sync(force=False):
        calls.append(asyncio.get_running_loop().time())
        return {}

    monkeypatch.setattr(chats, "sync", fake_sync)
    monkeypatch.setattr(chats, "LIVE_MIN_GAP_S", 0.3)
    monkeypatch.setattr(chats, "_live", {"pending": False, "task": None, "last": 0.0})

    real_sleep = asyncio.sleep
    monkeypatch.setattr(chats.asyncio, "sleep", lambda s: real_sleep(min(s, 0.3)))
    for _ in range(5):                                   # a burst of frames
        await chats.on_live_event("c1", {})
    await chats._live["task"]
    assert len(calls) == 1
    await chats.on_live_event("c1", {})                  # next message later on
    await chats._live["task"]
    assert len(calls) == 2 and calls[1] - calls[0] >= 0.29
