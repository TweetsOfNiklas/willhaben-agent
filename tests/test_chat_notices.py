"""willhaben's own notices in a chat (PayLivery, shipping, status) are no message from the other person: no push, no
draft, no address check, and the chat page shows them as notices – also rows stored before notices got their kind."""
from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Conversation, Message, session, utcnow
from app.platforms.text import clean_text
from app.services import chats, shipping
from app.willhaben.account import ChatMessage, ConversationSummary

T0 = utcnow() - timedelta(hours=2)


def _setup(monkeypatch, state):
    class _Sess:
        started = logged_in = True

    class _Acct:
        async def conversations(self, limit=50):
            last = state["history"][-1]
            return [ConversationSummary(id="c1", ad_id="9", ad_title="Raspberry Pi 4", partner_name="Lukas",
                                        unread_count=state["unread"], last_text=last.text or "", last_at=last.created_at,
                                        raw={}, role="seller", last_is_mine=False)]   # notices come as is_mine false

        async def messages(self, conversation_id):
            return list(state["history"])

    pushed, checked = [], []

    async def notify(kind, title, message, **kw):
        pushed.append(message)
        return True

    async def on_incoming(conv):
        checked.append(conv.last_message_text)

    monkeypatch.setattr(chats, "get_session", lambda: _Sess())
    monkeypatch.setattr(chats, "_account", lambda: _Acct())
    monkeypatch.setattr(chats, "notify", notify)
    monkeypatch.setattr(shipping, "on_incoming", on_incoming)
    return pushed, checked


def _msg(i, text, direction="in", kind=None, minutes=0):
    return ChatMessage(ext_id=f"m{i}", text=text, created_at=T0 + timedelta(minutes=minutes), direction=direction,
                       kind=kind)


async def test_a_paylivery_notice_is_no_message_from_the_buyer(monkeypatch):
    state = {"unread": 0, "history": [_msg(1, "Ist der Pi noch da?"), _msg(2, "Ja", "out", minutes=1)]}
    pushed, checked = _setup(monkeypatch, state)
    await chats.sync()                                               # first sync: nothing is news

    state["unread"] = 1
    state["history"].append(_msg(3, "Der Kauf wurde freigegeben und ist nun zum Versand bereit.", "unknown",
                                 "integration", minutes=5))
    await chats.sync()
    assert pushed == [] and checked == []                            # 10-11: pushed as "Nachricht von Lukas"

    state["unread"] = 2                                              # the buyer writes, then another notice comes
    state["history"] += [_msg(4, "Adresse: Hauptstraße 1, 1010 Wien", minutes=6),
                         _msg(5, "Wir haben dem Käufer mitgeteilt, dass der Artikel unterwegs ist.", "unknown",
                              "integration", minutes=7)]
    await chats.sync()
    assert len(pushed) == 1 and pushed[0].endswith(": Adresse: Hauptstraße 1, 1010 Wien")   # their text, not the notice
    assert checked == ["Adresse: Hauptstraße 1, 1010 Wien"]


async def test_my_own_answer_is_no_incoming_message(monkeypatch):
    state = {"unread": 1, "history": [_msg(1, "?")]}
    pushed, _ = _setup(monkeypatch, state)
    await chats.sync()
    state["history"].append(_msg(2, "Hallo, ja, ist noch zu haben.", "out", minutes=1))   # their "?" still unread
    await chats.sync()
    assert pushed == []


def test_old_notice_rows_show_as_notices():
    """Rows stored before notices got a kind only carry direction "unknown" (154 of them on 2026-10-13)."""
    with session() as db:
        db.add(Conversation(id="c1", partner_name="Lukas", ad_title="Pi", last_message_at=utcnow()))
        db.add(Message(conversation_id="c1", ext_id="n1", direction="unknown", created_at=T0,
                       text="willhaben PayLivery - Online-Zahlung und Versand"))
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in", created_at=T0 + timedelta(minutes=1),
                       text="Ist der Pi noch da?"))
        db.commit()
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/chats/c1").text
    notice = page.index("willhaben PayLivery - Online-Zahlung und Versand")
    assert page.rfind("data-notice", 0, notice) > page.rfind("chat-bubble", 0, notice)
    assert "chat-start" in page[page.index("Ist der Pi noch da?") - 400:page.index("Ist der Pi noch da?")]


def test_clean_text_drops_a_tag_cut_off_at_the_end():
    assert clean_text("Verkauf wegen Nichtgebrauch. <br...") == "Verkauf wegen Nichtgebrauch. …"
    assert clean_text("Akku <Ladung ok") == "Akku <Ladung ok"            # not a tag: stays


def test_clean_text_list_items_in_lists_of_their_own():
    html = "<p>Daten:</p><ul><li>A</li></ul><ul><li>B</li></ul><p>Ende</p><p><br></p><p>Gruß</p>"
    assert clean_text(html) == "Daten:\n• A\n• B\nEnde\n\nGruß"            # as willhaben shows it
    assert clean_text("<p>Daten:</p><p><br></p><ul><li>A</li></ul>") == "Daten:\n\n• A"


async def test_a_notice_unread_again_is_no_message_from_the_buyer(monkeypatch):
    """10-26: after "Artikel versendet" willhaben counted the notice unread again (0 -> 1, no new message): the chat
    is not fetched again then, but its stored history already ends with the notice – no push, no draft offer."""
    state = {"unread": 0, "history": [_msg(1, "Wann kommt das Paket?"), _msg(2, "Morgen", "out", minutes=1),
                                      _msg(3, "Dankeschön", minutes=2)]}
    pushed, checked = _setup(monkeypatch, state)
    await chats.sync()
    state["unread"] = 1
    state["history"].append(_msg(4, "Wir haben dem Käufer mitgeteilt, dass der Artikel unterwegs ist.", "unknown",
                                 "integration", minutes=5))
    await chats.sync()                                               # new: fetched, the notice is seen as one
    state["unread"] = 0
    await chats.sync()                                               # read on willhaben
    state["unread"] = 1
    await chats.sync()                                               # unread again, nothing new: was pushed
    assert pushed == [] and checked == []
