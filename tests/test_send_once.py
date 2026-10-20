"""A draft goes out once: two taps on a push (Telegram and Home Assistant), a double click or the dashboard and a push
together cannot send it twice."""
import asyncio

import pytest

from app.db import Conversation, Draft, session
from app.services import chats, ha_actions


def _draft():
    with session() as db:
        db.add(Conversation(id="c-once", partner_name="x"))
        d = Draft(conversation_id="c-once", text="Ja, ist noch da.")
        db.add(d)
        db.commit()
        db.refresh(d)
        return d.id


class _Account:
    def __init__(self):
        self.sent = []

    async def send_message(self, conv, text):
        await asyncio.sleep(0.05)                 # the websocket round trip: the second caller comes in meanwhile
        self.sent.append(text)


@pytest.fixture
def account(monkeypatch):
    acc = _Account()
    monkeypatch.setattr(chats, "_account", lambda: acc)

    async def no_read(_):
        return None
    monkeypatch.setattr(chats, "mark_read", no_read)

    async def no_meetup(_):
        return None
    monkeypatch.setattr(chats.meetups, "on_sent", no_meetup)
    return acc


async def test_two_sends_at_once_send_one_message(account):
    did = _draft()
    res = await asyncio.gather(chats.send_draft(did, "Ja"), chats.send_draft(did, "Ja"), return_exceptions=True)
    assert account.sent == ["Ja"]
    assert sum(isinstance(r, chats.AlreadySent) for r in res) == 1
    with pytest.raises(chats.AlreadySent):          # and afterwards: it is sent
        await chats.send_draft(did, "Ja")
    assert account.sent == ["Ja"]


async def test_two_taps_on_the_push_send_once_and_report_nothing_twice(account, monkeypatch):
    did = _draft()
    pushed = []

    async def notify(kind, title, body="", **kw):
        pushed.append(title)

    async def clear(_):
        await asyncio.sleep(0.01)                   # the status check is before this await: both taps pass it
    monkeypatch.setattr("app.services.notify.notify", notify)
    monkeypatch.setattr("app.services.notify.clear_push", clear)
    action = {"action": ha_actions.action_id("SEND", did)}
    res = await asyncio.gather(ha_actions.handle(action), ha_actions.handle(dict(action)))
    assert account.sent == ["Ja, ist noch da."]
    assert sorted(res, key=str) == [None, "sent"] and pushed == ["Sent"]


async def test_a_failed_send_can_be_tried_again(account, monkeypatch):
    did = _draft()

    async def fail(conv, text):
        raise chats.SendFailed("no answer")
    monkeypatch.setattr(account, "send_message", fail)
    with pytest.raises(chats.SendFailed):
        await chats.send_draft(did, "Ja")
    monkeypatch.undo()
    acc = _Account()
    monkeypatch.setattr(chats, "_account", lambda: acc)

    async def none(*_):
        return None
    monkeypatch.setattr(chats, "mark_read", none)
    monkeypatch.setattr(chats.meetups, "on_sent", none)
    await chats.send_draft(did, "Ja")
    assert acc.sent == ["Ja"]
