import json

import httpx
import pytest

from app.db import Conversation, Draft, Hit, MyAd, get_setting, session, set_setting
from app.services import feedback, ha_actions, telegram
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)

CHAT = 4242


@pytest.fixture
def tg(monkeypatch):
    """Mocked Bot API; returns the list of (method, body) calls."""
    calls, ids = [], iter(range(100, 10_000))

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "wh_test_bot"}})
        if method in ("sendMessage", "sendPhoto"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": next(ids), "chat": {"id": body["chat_id"]}}})
        return httpx.Response(200, json={"ok": True, "result": True})

    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(handler))
    telegram._pending.clear()
    yield calls
    telegram.forget()


def _buttons(body):
    return {b["text"]: b.get("callback_data") or b.get("url") for row in body["reply_markup"]["inline_keyboard"] for b in row}


def _press(data, chat=CHAT, msg_id=7):
    return {"callback_query": {"id": "cq", "data": data, "message": {"message_id": msg_id, "chat": {"id": chat}}}}


def _text(text, chat=CHAT, reply_to=None):
    msg = {"message_id": 99, "chat": {"id": chat, "first_name": "Max"}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"message": msg}


async def _linked(tg):
    """Pairing: whoever writes to the bot becomes a candidate; the Settings button picks one."""
    assert await telegram.save_token("123:abc") == "wh_test_bot"
    assert telegram.bot_url() == "https://t.me/wh_test_bot" and telegram.status()["candidates"] == []
    await telegram.handle_update(_text("/start", chat=666))
    await telegram.handle_update(_text("hello"))
    await telegram.handle_update(_text("hello again"))                  # same chat: one entry, one reply
    assert [c["chat_id"] for c in telegram.candidates()] == [CHAT, 666]
    assert [b["chat_id"] for m, b in tg if m == "sendMessage"] == [666, CHAT]
    assert "Settings" in tg[-1][1]["text"] and telegram.chat_id() is None
    codes = {c["chat_id"]: c["code"] for c in telegram.candidates()}     # each chat gets its own code, shown in Settings
    assert [b["text"][-4:] for m, b in tg if m == "sendMessage"] == [codes[666], codes[CHAT]]
    assert await telegram.link(4711) is None                             # not a candidate
    assert await telegram.link(CHAT) == "Max"
    assert telegram.chat_id() == CHAT and get_setting("telegram_chat_name") == "Max" and not telegram.candidates()
    assert tg[-1][1]["chat_id"] == CHAT and tg[-1][1]["text"].startswith("✅")
    await telegram.handle_update(_text("/start", chat=666))             # linked: strangers get silence
    assert tg[-1][1]["chat_id"] == CHAT
    tg.clear()


async def test_candidates_expire_and_relink(tg, monkeypatch):
    await _linked(tg)
    telegram.unlink()
    assert telegram.chat_id() is None and not telegram.candidates()
    await telegram.handle_update(_text("hi", chat=555))
    assert [c["chat_id"] for c in telegram.candidates()] == [555]
    now = telegram.time.monotonic()
    monkeypatch.setattr(telegram.time, "monotonic", lambda: now + telegram.CANDIDATE_TTL + 1)
    assert telegram.candidates() == [] and await telegram.link(555) is None


async def test_link_and_kinds(tg):
    assert not await telegram.send("hit", "x", "y")                     # nothing linked yet
    await _linked(tg)
    assert await telegram.send("hit", "Rad: 120 €", "KTM <20\"> (Wien)", url="https://www.willhaben.at/x",
                               image="https://img/x.jpg", data={"ad_id": "77"})
    method, body = tg[-1]
    assert method == "sendPhoto" and "<b>Rad: 120 €</b>\nKTM &lt;20\"&gt; (Wien)" == body["caption"]
    assert set(_buttons(body)) == {"⭐ Keep", "🚫 Ignore", "🔗 Open"}
    telegram.set_kinds(["message"])
    assert not await telegram.send("hit", "x", "y")
    assert await telegram.send("test", "x", "y")                        # the test push always goes through
    await telegram.handle_update(_press("t:unknown", chat=666))
    assert tg[-1] == ("answerCallbackQuery", {"callback_query_id": "cq", "text": "Not allowed."})


async def test_draft_buttons_use_the_ha_action_path(tg, monkeypatch):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c1", ad_id="5", ad_title="Rad", partner_name="Max"))
        db.add(Draft(id=11, conversation_id="c1", text="Ja, noch da."))
        db.add(Draft(id=12, conversation_id="c1", text="[Uhrzeit] passt"))
        db.commit()
    sent = []

    async def fake_send(draft_id, text):
        sent.append((draft_id, text))
        with session() as db:
            d = db.get(Draft, draft_id)
            d.status, d.text = "sent", text
            db.add(d)
            db.commit()
        return d

    monkeypatch.setattr("app.services.chats.send_draft", fake_send)
    await telegram.send("auto_reply_pending", "Antwort freigeben? Max", "…", tag=ha_actions.tag_for(11),
                        data={"conversation_id": "c1", "draft_id": 11}, actions=ha_actions.draft_actions(11, True))
    b = _buttons(tg[-1][1])
    assert list(b) == ["✅ Send", "↩️ Direct answer", "✍️ Revise with Claude", "🗑 Discard"]

    # "Direct answer": ForceReply prompt, the reply text is sent through ha_actions.handle
    await telegram.handle_update(_press(b["↩️ Direct answer"]))
    prompt = tg[-1]
    assert prompt[0] == "sendMessage" and prompt[1]["reply_markup"]["force_reply"] is True
    await telegram.handle_update(_text("Ja, Abholung heute ab 18 Uhr.", reply_to=next(iter(telegram._pending))))
    assert sent == [(11, "Ja, Abholung heute ab 18 Uhr.")]

    # second tap on an old button: nothing is sent again
    await telegram.handle_update(_press(b["✅ Send"]))
    assert sent == [(11, "Ja, Abholung heute ab 18 Uhr.")] and "Already done." in tg[-2][1]["text"]

    await telegram.send("auto_reply_pending", "x", "y", actions=ha_actions.draft_actions(12, False))
    b12 = _buttons(tg[-1][1])
    assert "✅ Send" not in b12                                         # placeholders: no plain send
    await telegram.handle_update(_press(b12["🗑 Discard"]))
    with session() as db:
        assert db.get(Draft, 12).status == "discarded"


async def test_message_reply_feedback_auto_and_hits(tg, monkeypatch):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c2", ad_id="9", ad_title="Stuhl", partner_name="Eva"))
        db.add(Draft(id=21, conversation_id="c2", text="Ja, ist noch da.", status="sent", auto=True))
        db.add(MyAd(ad_id="9", heading="Stuhl", auto_reply=True, auto_reply_instructions="Preis fix 20 €"))
        db.add(Hit(agent_id=1, ad_id="77", heading="Rad", url="https://x"))
        db.commit()
    sent = []

    async def fake_send(draft_id, text):
        sent.append((draft_id, text))

    monkeypatch.setattr("app.services.chats.send_draft", fake_send)

    # new chat message -> "Reply": typed text (without using Telegram's reply) becomes a draft and is sent
    await telegram.send("message", "Nachricht von Eva", "Stuhl: noch da?", data={"conversation_id": "c2", "ad_id": "9"})
    b = _buttons(tg[-1][1])
    assert set(b) == {"✍️ Draft with Claude", "↩️ Reply"}
    await telegram.handle_update(_press(b["↩️ Reply"]))
    await telegram.handle_update(_text("Ja, heute abholbar."))
    with session() as db:
        d = db.exec(__import__("sqlmodel").select(Draft).where(Draft.model == "telegram")).one()
    assert sent == [(d.id, "Ja, heute abholbar.")] and d.conversation_id == "c2"

    # automatic answer sent -> 👎 + note feeds the next automatic answers
    await telegram.send("auto_reply", "Automatisch geantwortet: Eva", "…",
                        data={"conversation_id": "c2", "ad_id": "9", "draft_id": 21})
    b = _buttons(tg[-1][1])
    await telegram.handle_update(_press(b["👎 Bad answer"]))
    await telegram.handle_update(_text("Nie Versand zusagen", reply_to=next(iter(telegram._pending))))
    with session() as db:
        assert (db.get(Draft, 21).feedback, db.get(Draft, 21).feedback_note) == ("down", "Nie Versand zusagen")
    block = feedback.prompt_block()
    assert "Rückmeldungen von mir" in block and "Nie Versand zusagen" in block and "Ja, ist noch da." in block

    # auto answers off for the ad keeps the per-ad instructions
    await telegram.handle_update(_press(b["⏸ Auto answers off for this ad"]))
    with session() as db:
        ad = db.get(MyAd, "9")
    assert ad.auto_reply is False and ad.auto_reply_instructions == "Preis fix 20 €"

    await telegram.send("hit", "Rad", "x", data={"ad_id": "77"})
    await telegram.handle_update(_press(_buttons(tg[-1][1])["⭐ Keep"]))
    with session() as db:
        assert db.exec(__import__("sqlmodel").select(Hit)).one().state == "saved"


async def test_same_tag_edits_and_clear(tg):
    await _linked(tg)
    assert await telegram.send("auto_reply_pending", "Antwort freigeben?", "a", tag="wh-draft-5")
    assert await telegram.send("auto_reply", "Gesendet", "b", tag="wh-draft-5")
    assert tg[-1][0] == "editMessageText" and tg[-1][1]["text"] == "<b>Gesendet</b>\nb"
    await telegram.clear("wh-draft-5")
    assert tg[-1][0] == "editMessageReplyMarkup"
    set_setting("telegram_enabled", "0")
    assert not await telegram.send("test", "x", "y")


def test_settings_panel_and_routes(client, monkeypatch):  # noqa: F811
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[1])
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "wh_test_bot"}})
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": CHAT}}})
        return httpx.Response(200, json={"ok": True, "result": True})

    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(handler))
    page = client.get("/settings/telegram").text
    assert "/newbot" in page
    r = client.post("/settings/telegram/token", data={"token": "123:abc"}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings/telegram?") and telegram.token() == "123:abc"
    assert "setMyCommands" in calls and "deleteWebhook" in calls
    page = client.get("/settings/telegram").text
    assert "@wh_test_bot" in page and 'id="tg-link"' in page and "https://t.me/wh_test_bot" in page and "<svg" in page
    assert client.get("/settings/telegram/status").json() == {"linked": False, "chat_name": "", "candidates": []}
    telegram._candidates[CHAT] = ("Max <b>", telegram.time.monotonic(), "0042")
    st = client.get("/settings/telegram/status").json()
    assert st["candidates"][0]["chat_id"] == CHAT and st["candidates"][0]["name"] == "Max <b>"
    page = client.get("/settings/telegram").text
    assert "New chat: Max &lt;b&gt; — is that you?" in page and "code 0042" in page and f'data-tg-fp="{CHAT}"' in page
    r = client.post("/settings/telegram/link", data={"chat_id": "999"}, follow_redirects=False)
    assert "expired" in r.headers["location"] and telegram.chat_id() is None
    r = client.post("/settings/telegram/link", data={"chat_id": str(CHAT)}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings/telegram?") and telegram.chat_id() == CHAT and calls[-1] == "sendMessage"
    assert client.get("/settings/telegram/status").json()["linked"]
    client.post("/settings/telegram/kinds", data={"enabled": "on", "kind": ["message", "hit"]})
    assert telegram.enabled_kinds() == {"message", "hit"} and telegram.active()
    client.post("/settings/telegram/test")
    assert calls[-1] == "sendMessage"
    client.post("/settings/telegram/kinds", data={"kind": []})
    assert not telegram.active() and telegram.enabled_kinds() == set()
    client.post("/settings/telegram/forget")
    assert telegram.token() == "" and not telegram.status()["configured"]


async def test_typing_while_claude_writes(tg, monkeypatch):
    async with telegram.typing():                       # not linked: no API calls
        pass
    assert not tg
    await _linked(tg)
    monkeypatch.setattr(telegram, "TYPING_EVERY_S", 0.01)
    async with telegram.typing():
        await __import__("asyncio").sleep(0.05)
    n = len([c for c in tg if c[0] == "sendChatAction"])
    assert n >= 2 and tg[0][1] == {"chat_id": CHAT, "action": "typing"}
    await __import__("asyncio").sleep(0.05)
    assert len([c for c in tg if c[0] == "sendChatAction"]) == n     # stopped after the block


def test_draft_reply_shows_typing(client, monkeypatch):  # noqa: F811
    from tests.test_dashboard import _conversation

    seen = []

    class Spy:
        async def __aenter__(self):
            seen.append("start")

        async def __aexit__(self, *exc):
            seen.append("stop")

    monkeypatch.setattr(telegram, "typing", lambda: Spy())
    _conversation()
    client.post("/chats/c1/draft", data={"hint": ""})
    assert seen == ["start", "stop"] and client.fake.calls


async def test_bot_token_never_logged(tg, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    await telegram.save_token("8774:SECRET-token")
    await telegram.api("getMe")
    assert "SECRET-token" not in caplog.text
