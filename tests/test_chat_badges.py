"""The sidebar's chat badges: messages not yet looked at in the app (whatever the site says: an automatic answer marks
them read there first) and the chats that need me (the other person wrote last, or a draft waits). An answer on its
way stands in the thread with its countdown, not as a notice at the edge."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Conversation, Draft, Message, session, set_setting, utcnow
from app.services import chats

T0 = utcnow().replace(microsecond=0)


def _chat(cid, *msgs, **kw):
    with session() as db:
        db.add(Conversation(id=cid, partner_name="Käufer", last_message_at=T0, **kw))
        for i, (direction, minutes) in enumerate(msgs):
            db.add(Message(conversation_id=cid, ext_id=f"{cid}-{i}", direction=direction, text=f"m{i}",
                           created_at=T0 + timedelta(minutes=minutes)))
        db.commit()


def _counts():
    with session() as db:
        return chats.badge_counts(db)


def test_new_messages_count_until_the_chat_is_opened_here(monkeypatch):
    set_setting("chats_app_seen_since", (T0 - timedelta(hours=1)).isoformat())
    _chat("old", ("in", -120), ("out", -110))          # before the app looked: history, no badge
    _chat("c1", ("out", -9), ("in", -5), ("in", -4))   # two new from the other person; the site says read (0)
    _chat("c2", ("in", -3), ("out", 1))                # I answered (on the phone): read
    with session() as db:
        db.add(Draft(conversation_id="c2", text="Noch eine Frage?", status="draft"))
        db.commit()
    assert _counts() == {"chats": 2, "chats_action": 2}        # c1: they wrote last; c2: a draft waits
    chats.seen_in_app("c1")
    assert _counts() == {"chats": 0, "chats_action": 2}
    with session() as db:
        db.add(Message(conversation_id="c1", ext_id="c1-new", direction="in", text="Hallo?",
                       created_at=utcnow() + timedelta(minutes=1)))
        db.commit()
    assert _counts()["chats"] == 1


def test_the_badges_reach_the_sidebar_and_opening_the_chat_clears_one():
    set_setting("chats_app_seen_since", (T0 - timedelta(hours=1)).isoformat())
    _chat("c1", ("in", -1))
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/jobs").json()["counts"]["chats"] == 1
        page = c.get("/hits").text
        assert 'data-nav-count="chats_action"' in page and 'data-nav-count="chats"' in page
        c.get("/chats/c1")
        assert c.get("/jobs").json()["counts"] == {**c.get("/jobs").json()["counts"], "chats": 0, "chats_action": 1}


def test_an_answer_on_its_way_stands_in_the_thread_with_its_countdown():
    _chat("c1", ("in", -1))
    from app.main import app
    with TestClient(app) as c:      # started first: its start-up puts drafts left "sending" back to open ones
        with session() as db:
            d = Draft(conversation_id="c1", text="Ja, noch da.", status="sending")
            db.add(d)
            db.commit()
            did = d.id
        page = c.get("/chats/c1").text
    thread = page[page.index('id="chat-messages"'):page.index('id="chat-composer"')]
    assert f'data-undo-here="draft:{did}"' in thread and "Ja, noch da." in thread and "data-sending" in thread


def test_an_automatic_answer_does_not_count_as_read():
    set_setting("chats_app_seen_since", (T0 - timedelta(hours=1)).isoformat())
    _chat("c1", ("in", -5), ("out", -2))
    with session() as db:
        db.add(Draft(conversation_id="c1", text="m1", status="sent", auto=True))     # the app answered by itself
        db.commit()
    assert _counts() == {"chats": 1, "chats_action": 0}       # answered, but nobody looked at it yet


def test_the_list_marks_unread_in_blue_and_awaiting_my_reply_with_a_yellow_dot():
    set_setting("chats_app_seen_since", (T0 - timedelta(hours=1)).isoformat())
    _chat("c1", ("in", -5))                    # new and unanswered: both
    _chat("c2", ("in", -9), ("out", -8))       # answered: neither
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats").text.split('id="chat-list-items"')[1]
    rows = {cid: page[page.index(f"/chats/{cid}"):][:4000].split("</a>")[0] for cid in ("c1", "c2")}
    assert "badge-primary" in rows["c1"] and "data-awaiting" in rows["c1"]
    assert "badge-primary" not in rows["c2"] and "data-awaiting" not in rows["c2"]


@pytest.mark.parametrize("text,closes", [
    ("Danke, passt!", True), ("Ist angekommen, danke 👍", True), ("Ok super, bis morgen", True),
    ("Vielen Dank und schönen Abend", True), ("Thanks, have a nice day", True), ("Passt lg", True),
    ("Passt, wann kann ich kommen?", False), ("Danke, geht auch 40?", False), ("Ok", True),
    ("Ok, ich nehme es um 50", False), ("Noch da", False), ("", False)])
def test_a_message_that_only_thanks_or_says_goodbye_ends_the_chat(text, closes):
    assert chats.closes_chat(text) is closes


def test_a_finished_chat_needs_no_answer_until_they_write_again():
    set_setting("chats_app_seen_since", (T0 - timedelta(hours=1)).isoformat())
    _chat("c1", ("out", -9), ("in", -5))
    _chat("c2", ("out", -9), ("in", -5))
    with session() as db:
        db.get(Message, db.exec(select(Message.id).where(Message.ext_id == "c2-1")).one()).text = "Danke, passt!"
        db.commit()
    assert _counts()["chats_action"] == 1                 # c2 ended by their thanks
    from app.main import app
    with TestClient(app) as c:
        c.post("/chats/c1/finished", data={"finished": "1"})
        assert 'data-finished="1"' in c.get("/chats/c1").text
    assert _counts()["chats_action"] == 0
    with session() as db:
        db.add(Message(conversation_id="c1", ext_id="c1-later", direction="in", text="Doch noch eine Frage",
                       created_at=utcnow() + timedelta(minutes=1)))
        db.commit()
    assert _counts()["chats_action"] == 1                 # they wrote again: open again


def test_my_own_text_goes_out_directly(monkeypatch):
    """Instead of a Claude draft: the text I typed, sent as it is, right away."""
    queued = []
    monkeypatch.setattr(chats, "enqueue_send", lambda did, text, now=False: queued.append((did, text, now)) or True)
    _chat("c1", ("in", -1))
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats/c1").text
        assert "data-send-direct" in page and "data-send-now" not in page
        c.post("/chats/c1/send-direct", data={"hint": "Passt, bis morgen"}, follow_redirects=False)
    with session() as db:
        d = db.exec(select(Draft).where(Draft.conversation_id == "c1")).one()
    assert d.text == "Passt, bis morgen" and d.model == "manual" and queued == [(d.id, "Passt, bis morgen", True)]


def test_mark_as_unread_until_opened_again_and_the_live_line_in_the_chat():
    set_setting("chats_app_seen_since", (T0 - timedelta(hours=1)).isoformat())
    _chat("c1", ("in", -9), ("out", -5))                      # answered: read
    assert _counts()["chats"] == 0
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats/c1").text
        assert "data-mark-unread" in page and 'data-chat-live="0"' in page and "willhaben offline" in page
        r = c.post("/chats/c1/unread", follow_redirects=False)
        assert r.headers["location"].startswith("/chats") and "/c1" not in r.headers["location"]
        assert _counts()["chats"] == 1
        c.get("/chats/c1")                                     # opened: read again
    assert _counts()["chats"] == 0


def test_opening_a_chat_keeps_the_lists_filter():
    _chat("c1", ("in", -1), role="seller")
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats?role=seller&show=reply").text
        assert 'href="/chats/c1?role=seller&amp;show=reply"' in page
        opened = c.get("/chats/c1?role=seller&show=reply").text
        assert 'href="/chats/c1?role=seller&amp;show=reply"' in opened         # the list beside it: still filtered


def test_each_chat_shows_its_items_picture_in_the_list():
    from app.db import MyAd
    _chat("c1", ("in", -1), ad_id="77", ad_context={"images": ["https://img/item.webp"]})
    _chat("c2", ("in", -1), ad_id="88")
    with session() as db:
        db.add(MyAd(ad_id="88", heading="Lampe", image_url="https://img/mine.jpg"))
        db.commit()
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats").text
    assert 'src="https://img/item.webp"' in page and 'src="https://img/mine.jpg"' in page and "data-chat-thumb" in page
