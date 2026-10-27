"""Foreign chat messages (services/translate): Claude names each message's language once and translates the foreign
ones; the bubble shows the original and the translation (10-27: a Vinted chat mixed Italian and German)."""
from datetime import timedelta

from app.db import Conversation, Message, session, utcnow
from app.llm.base import LLMResult
from app.services import translate
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)


class FakeClaude:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append(prompt)
        return LLMResult(data=self.data, model="fake")


def _chat():
    now = utcnow()
    with session() as db:
        db.add(Conversation(id="t1", ad_id="1", partner_name="x", platform="vinted"))
        db.add(Message(conversation_id="t1", ext_id="a", direction="in", text="Oggi penso proprio di sì.",
                       created_at=now - timedelta(minutes=5)))
        db.add(Message(conversation_id="t1", ext_id="b", direction="out", text="Super, danke dir!", created_at=now))
        db.add(Message(conversation_id="t1", ext_id="c", direction="unknown", kind="integration",
                       text="Sendung wurde verschickt", created_at=now))          # a notice: not asked
        db.commit()


async def test_foreign_messages_get_a_translation_once(monkeypatch):
    _chat()
    fake = FakeClaude({"items": [{"n": 1, "lang": "de", "translation": ""},
                                 {"n": 2, "lang": "it", "translation": "Heute denke ich schon."}]})
    monkeypatch.setattr(translate, "get_backend", lambda *a: fake)
    assert await translate.translate_chat("t1") == 1
    assert "1. Super, danke dir!" in fake.calls[0] and "Sendung" not in fake.calls[0]   # newest first, no notices
    with session() as db:
        rows = {m.ext_id: m for m in db.exec(translate.select(Message))}
    assert rows["a"].translation == "Heute denke ich schon." and rows["a"].lang == "it"
    assert rows["b"].translation is None and rows["b"].lang == "de"
    assert await translate.translate_chat("t1") == 0 and len(fake.calls) == 1        # asked once per message


def test_the_bubble_shows_original_and_translation(client):  # noqa: F811
    _chat()
    with session() as db:
        m = db.exec(translate.select(Message).where(Message.ext_id == "a")).one()
        m.lang, m.translation = "it", "Heute denke ich schon."
        db.add(m)
        db.commit()
    page = client.get("/chats/t1").text
    i = page.index("Oggi penso proprio di sì.")
    assert "data-translation" in page[i:i + 600] and "Heute denke ich schon." in page[i:i + 600]
    tg = client.get("/tg/chats/t1").text
    i = tg.index("Oggi penso proprio di sì.")
    assert "Heute denke ich schon." in tg[i:i + 700]
