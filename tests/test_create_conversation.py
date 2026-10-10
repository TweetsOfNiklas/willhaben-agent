"""AccountClient.create_conversation: the ad page's contact form (`POST iad-messaging/sendrequest/chat`).

No network: the BrowserSession is replaced by a fake with `api` (the POST, the profile) and `api_json` (the
conversation list; same pattern as tests/test_auto_reply.py). The access-log test uses the real BrowserSession with a
fake page (tests/test_access_stats.py).
"""
import json

import pytest

from app.willhaben.account import SEND_REQUEST_CHAT, AccountClient, SendFailed
from app.willhaben.session import ApiResponse, NotLoggedIn
from tests.test_access_stats import _counts, _session

TEXT = "  Hallo, ist der Artikel noch zu haben? Ich würde 80 € zahlen.  "
ME = {"userData": {"loginId": 42, "uuid": "u-1", "firstName": "Niklas", "lastName": "Ruby",
                   "emailAddress": "buyer@example.org"}}


class _FakeSession:
    """`post`: (status, body) of the contact request. `lists`: one conversation list per poll."""

    def __init__(self, post=(200, {"sent": True, "conversationId": "c-new"}), lists=None, me=ME):
        self.post = post
        self.me = me
        self.lists = list(lists or [])
        self.posts = []
        self.list_calls = 0

    async def api(self, method, path, body=None, accept="application/json"):
        if method == "GET":
            assert path == "/webapi/userprofile-service/userprofile/me"
            return ApiResponse(200, json.dumps(self.me), path)
        assert method == "POST" and path == SEND_REQUEST_CHAT
        self.posts.append(body)
        status, data = self.post
        return ApiResponse(status, data if isinstance(data, str) else json.dumps(data), path)

    async def api_json(self, method, path, body=None):
        if path.endswith("/userprofile/me"):
            return self.me
        assert method == "GET" and path == "/webapi/chat-api/v1/conversations?limit=20&offset=0"
        self.list_calls += 1
        return self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]


def _entry(cid="c9", ad_id="77", preview="Hallo, ist der Artikel noch zu haben? Ich würde 80 € zahlen.", mine=True):
    return {"conversation_id": cid, "ad_info": {"id": ad_id, "subject": "Kinderbuch"},
            "participant_info": {"name": "Max"}, "unseen": 0,
            "last_message": {"is_mine": mine, "preview": preview, "timestamp": 1790000000000}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr("app.willhaben.account.asyncio.sleep", instant)


async def test_body_like_the_contact_form_and_id_from_response():
    s = _FakeSession()
    assert await AccountClient(s).create_conversation("77", TEXT) == "c-new"
    assert s.posts == [{"fromFullName": "Niklas Ruby", "firstName": "Niklas", "lastName": "Ruby",
                        "shareTenantProfile": False, "from": "buyer@example.org", "adId": 77,
                        "copyToSender": False, "showTelephoneNumber": False, "telephone": "",
                        "selectedContactSuggestions": [], "mailContent": TEXT.strip()}]
    assert s.list_calls == 0                  # no polling when the answer carries the id


async def test_profile_without_name_leaves_the_name_fields_out():
    s = _FakeSession(me={"userData": {"loginId": 42, "emailAddress": "buyer@example.org"}})
    await AccountClient(s).create_conversation("77", TEXT)
    body = s.posts[0]
    assert body["fromFullName"] == "" and "firstName" not in body and "lastName" not in body


async def test_id_found_via_conversation_list_after_empty_polls():
    s = _FakeSession(post=(200, {"sent": True}),
                     lists=[{"conversations": []}, {"conversations": [_entry(ad_id="12")]},
                            {"conversations": [_entry(cid="c9")]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"
    assert s.list_calls == 3


async def test_truncated_preview_still_matches():
    preview = TEXT.strip()[:30] + "…"
    s = _FakeSession(post=(200, ""), lists=[{"conversations": [_entry(preview=preview)]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"


@pytest.mark.parametrize("entry", [
    _entry(ad_id="12"),                       # another ad
    _entry(mine=False),                       # newest message is the seller's
    _entry(preview="Ganz anderer Text"),      # not our message
    _entry(preview=None),                     # no preview to compare with
])
async def test_non_matching_entries_are_not_taken(entry):
    s = _FakeSession(post=(200, {"sent": True}), lists=[{"conversations": [entry]}])
    with pytest.raises(SendFailed, match="did not show up"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.list_calls == 3                  # all three polls were made


@pytest.mark.parametrize("post", [(400, {"errorMessage": "Anzeige nicht verfügbar"}), (200, {"sent": False})])
async def test_willhaben_refusal_is_send_failed(post):
    s = _FakeSession(post=post)
    with pytest.raises(SendFailed, match="rejected the new conversation"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.list_calls == 0


async def test_logged_out_is_not_logged_in():
    s = _FakeSession(post=(401, ""))
    with pytest.raises(NotLoggedIn):
        await AccountClient(s).create_conversation("77", TEXT)


@pytest.mark.parametrize("text", ["", "   \n"])
async def test_empty_text_is_value_error(text):
    s = _FakeSession()
    with pytest.raises(ValueError):
        await AccountClient(s).create_conversation("77", text)
    assert s.posts == []                      # nothing is sent


async def test_counts_as_a_send_in_the_access_log():
    s = _session([{"status": 200, "text": '{"sent": true, "conversationId": "c1"}', "url": "x"}])
    await s.api("POST", SEND_REQUEST_CHAT, {"adId": 1})
    assert _counts() == {("account", "send", "ok"): 1}
