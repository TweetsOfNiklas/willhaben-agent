"""The sidebar's chat badges: messages not yet looked at in the app (whatever the site says: an automatic answer marks
them read there first) and the chats that need me (the other person wrote last, or a draft waits). An answer on its
way stands in the thread with its countdown, not as a notice at the edge."""
from datetime import timedelta

from fastapi.testclient import TestClient

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
