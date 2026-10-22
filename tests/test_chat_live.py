"""Live chat page: fingerprints from the local database tell the open page which part to swap (no reload,
no willhaben request)."""
import html
import json
from datetime import datetime, timezone

from app import db as dbmod
from app.services import chats
from tests.test_dashboard import _conversation, client  # noqa: F401  (pytest fixture)


def _add(obj):
    with dbmod.session() as db:
        db.add(obj)
        db.commit()


def test_fingerprints_change_only_where_something_changed():
    _conversation()
    before = chats.live_state("c1")
    assert set(before) == {"list", "messages", "composer", "context", "presence"}
    assert chats.live_state("c1") == before                                   # stable while nothing happens

    _add(dbmod.Message(conversation_id="c1", ext_id="m2", direction="in", text="Noch da?",
                       created_at=datetime(2026, 10, 9, 9, tzinfo=timezone.utc)))
    after = chats.live_state("c1")
    assert after["messages"] != before["messages"]
    assert after["composer"] == before["composer"] and after["context"] == before["context"]

    _add(dbmod.Draft(conversation_id="c1", text="Ja, noch da."))              # a new draft: composer + list badge
    drafted = chats.live_state("c1")
    assert drafted["composer"] != after["composer"] and drafted["list"] != after["list"]

    with dbmod.session() as db:                                             # read elsewhere: only the list
        conv = db.get(dbmod.Conversation, "c1")
        conv.unread_count = 0
        db.add(conv)
        db.commit()
    read = chats.live_state("c1")
    assert read["list"] != drafted["list"] and read["messages"] == drafted["messages"]

    _add(dbmod.Shipment(conversation_id="c1", status="draft"))              # the context column (parcel card)
    assert chats.live_state("c1")["context"] != read["context"]
    assert set(chats.live_state()) == {"list"} and set(chats.live_state("nope")) == {"list"}


def test_pages_carry_the_baseline_and_the_route_answers(client):  # noqa: F811
    _conversation()
    r = client.get("/chats/live-state", params={"c": "c1"})
    assert r.status_code == 200 and set(r.json()) == {"list", "messages", "composer", "context", "presence"}   # not taken as a chat id
    page = client.get("/chats/c1").text
    for part in ('id="chat-messages"', 'id="chat-composer"', 'id="chat-context"', 'id="chat-list-items"',
                 'id="chat-live-state"', 'data-conv="c1"', "/chats/live-state"):
        assert part in page, part
    state = json.loads(html.unescape(page.split('data-state="', 1)[1].split('"', 1)[0]))
    assert state["messages"] == chats.live_state("c1")["messages"]
    listing = client.get("/chats").text
    assert 'data-conv=""' in listing and "chat-messages" not in listing.split('id="chat-live"')[0]
