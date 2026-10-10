"""AccountClient.create_conversation: the chat WebSocket frame of the web app bundle (live unverified).

No network: the BrowserSession is replaced by a fake with `ws_request` and `api_json` (same pattern as
tests/test_auto_reply.py). The access-log test uses the real BrowserSession with a fake page (tests/test_access_stats.py).
"""
import uuid

import pytest

from app.willhaben.account import CHAT_WS, AccountClient, SendFailed
from tests.test_access_stats import _counts, _session

TEXT = "  Hallo, ist der Artikel noch zu haben? Ich würde 80 € zahlen.  "


class _FakeSession:
    """`ws_results`: one answer per call (a dict, or a callable(frame) -> dict). `lists`: one conversation list per poll."""

    def __init__(self, ws_results, lists=None):
        self.ws_results = list(ws_results)
        self.lists = list(lists or [])
        self.frames = []
        self.list_calls = 0

    async def ws_request(self, url, frame, request_id, *, req_type, timeout_ms=20000):
        self.frames.append((url, frame, request_id, req_type))
        res = self.ws_results.pop(0) if len(self.ws_results) > 1 else self.ws_results[0]
        res = res(frame) if callable(res) else res
        return {**res, "request_id": request_id}

    async def api_json(self, method, path, body=None):
        assert method == "GET" and path == "/webapi/chat-api/v1/conversations?limit=20&offset=0"
        self.list_calls += 1
        data = self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]
        return data


def _entry(cid="c9", ad_id="77", preview="Hallo, ist der Artikel noch zu haben? Ich würde 80 € zahlen.", mine=True):
    return {"conversation_id": cid, "ad_info": {"id": ad_id, "subject": "Kinderbuch"},
            "participant_info": {"name": "Max"}, "unseen": 0,
            "last_message": {"is_mine": mine, "preview": preview, "timestamp": 1790000000000}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr("app.willhaben.account.asyncio.sleep", instant)


async def test_frame_and_id_from_response():
    s = _FakeSession([{"status": "success", "conversation_id": "c-new"}])
    cid = await AccountClient(s).create_conversation("77", TEXT)
    assert cid == "c-new"
    url, frame, rid, req_type = s.frames[0]
    assert url == CHAT_WS and req_type == "create_conversation"
    assert frame["req_type"] == "create_conversation" and frame["request_id"] == rid
    assert set(frame) == {"req_type", "request_id", "payload"}
    payload = frame["payload"]
    assert payload["ad_id"] == "77"
    assert payload["message"] == TEXT.strip()
    assert str(uuid.UUID(payload["client_conversation_id"])) == payload["client_conversation_id"]
    assert str(uuid.UUID(rid)) == rid and s.list_calls == 0          # no polling when the answer carries the id


async def test_id_in_payload_or_response_is_taken():
    s = _FakeSession([{"status": "ack", "payload": {"conversation_id": "c-p"}}])
    assert await AccountClient(s).create_conversation("77", "Hallo") == "c-p"
    s = _FakeSession([{"status": "success", "response": {"conversation_id": "c-r"}}])
    assert await AccountClient(s).create_conversation("77", "Hallo") == "c-r"
    assert s.list_calls == 0


async def test_id_found_via_conversation_list_after_empty_polls():
    s = _FakeSession([{"status": "success"}],
                     lists=[{"conversations": []}, {"conversations": [_entry(ad_id="12")]},
                            {"conversations": [_entry(cid="c9")]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"
    assert s.list_calls == 3


async def test_truncated_preview_still_matches():
    preview = TEXT.strip()[:30] + "…"
    s = _FakeSession([{"status": "success"}], lists=[{"conversations": [_entry(preview=preview)]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"


@pytest.mark.parametrize("entry", [
    _entry(ad_id="12"),                       # another ad
    _entry(mine=False),                       # newest message is the seller's
    _entry(preview="Ganz anderer Text"),      # not our message
    _entry(preview=None),                     # no preview to compare with
])
async def test_non_matching_entries_are_not_taken(entry):
    s = _FakeSession([{"status": "success"}], lists=[{"conversations": [entry]}])
    with pytest.raises(SendFailed, match="did not show up"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.list_calls == 3                  # all three polls were made


async def test_willhaben_error_is_send_failed():
    s = _FakeSession([{"status": "error", "response": "ad not available"}])
    with pytest.raises(SendFailed, match="rejected the new conversation: ad not available"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.list_calls == 0


async def test_connection_failure_without_ack_is_send_failed():
    s = _FakeSession([{"status": "ws_error", "acked": False}])
    with pytest.raises(SendFailed, match="connection failed"):
        await AccountClient(s).create_conversation("77", TEXT)


async def test_ack_then_connection_loss_still_checks_the_list():
    s = _FakeSession([{"status": "closed", "acked": True, "code": 1006}],
                     lists=[{"conversations": [_entry()]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"


@pytest.mark.parametrize("text", ["", "   \n"])
async def test_empty_text_is_value_error(text):
    s = _FakeSession([{"status": "success", "conversation_id": "c1"}])
    with pytest.raises(ValueError):
        await AccountClient(s).create_conversation("77", text)
    assert s.frames == []                     # nothing is sent


async def test_counts_as_a_chat_send_in_the_access_log():
    s = _session([{"status": "success", "conversation_id": "c1"}])
    await s.ws_request(CHAT_WS, {"req_type": "create_conversation"}, "rid", req_type="create_conversation")
    assert _counts() == {("chat", "send", "ok"): 1}
