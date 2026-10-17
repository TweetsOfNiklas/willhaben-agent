"""Writing style (Settings → My rules → Writing style, app/services/style.py, app/style_routes.py): the owner's own
messages, the profile Claude derives from them, the background job and the card on the rules page."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import i18n
from app.db import Conversation, Draft, get_setting, session, set_setting
from app.llm import prompts
from app.llm.base import LLMError, LLMResult
from app.services import chats, jobs, style
from tests.conftest import finish_jobs, wait_jobs

T0 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
EN = {"accept-language": "en"}


@pytest.fixture(autouse=True)
def english():
    i18n.set_request_lang("en")


class _Backend:
    """Stands in for Claude: answers `data` (merged over a sane default), or raises `error`."""
    model = "fake-style"

    def __init__(self):
        self.data: dict = {}
        self.error: Exception | None = None
        self.calls: list[dict] = []
        self.purposes: list = []
        self.gate: asyncio.Event | None = None     # an async test holds the job here

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        base = {"profile": "- Du-Form\n- kurz, ohne Gruß", "examples": [], "note": ""}
        return LLMResult(data={**base, **self.data}, model=self.model)


@pytest.fixture
def backend(monkeypatch):
    b = _Backend()
    monkeypatch.setattr("app.services.style.get_backend", lambda purpose=None: (b.purposes.append(purpose), b)[1])
    return b


def _messages(n: int, role: str = "seller", prefix: str = "nachricht") -> list[str]:
    """n own messages, the oldest first ("nachricht 0" … ), in one chat."""
    texts = [f"{prefix} {i}" for i in range(n)]
    with session() as db:
        if db.get(Conversation, "c1") is None:
            db.add(Conversation(id="c1", role=role))
        for i, text in enumerate(texts):
            chats._upsert_message(db, "c1", f"m{prefix}{i}", "out", text, T0 + timedelta(minutes=i))
        db.commit()
    return texts


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _card(html: str) -> str:
    return html[html.index('<section id="style-card"'):html.index("</section>", html.index('<section id="style-card"'))]


def _derive_button(html: str) -> str:
    card = _card(html)
    return re.search(r'<button class="btn btn-sm[^>]*>', card).group(0)


# ------------------------------------------------------------------ the owner's own messages
def test_own_messages_are_only_my_own_words_newest_first():
    with session() as db:
        db.add(Conversation(id="c1", role="seller"))
        db.add(Conversation(id="c2", role="buyer"))
        up = chats._upsert_message
        up(db, "c1", "p1", "in", "Hallo! Ich verstehe, dass Sie sparen müssen. Wäre 40 € ok?", T0)             # the partner
        up(db, "c1", "m1", "out", "passt,   45 geht aber\nnicht drunter", T0 + timedelta(minutes=1))          # spaces
        up(db, "c1", "m2", "out", "Gerne, der Artikel ist noch verfügbar.", T0 + timedelta(minutes=2))        # sent draft
        up(db, "c1", "m3", "out", "ok", T0 + timedelta(minutes=3))                                            # short stays
        up(db, "c1", "m4", "out", "PayLivery-Hinweis für Verkäufer", T0 + timedelta(minutes=4), kind="integration")
        up(db, "c1", "m5", "out", "   ", T0 + timedelta(minutes=5))                                           # empty
        up(db, "c2", "m6", "out", "morgen 18h beim billa?", T0 + timedelta(minutes=6))                        # buyer side
        up(db, "c2", "m7", "out", "ok", T0 + timedelta(minutes=7))                                            # twice: once
        up(db, "c1", "m8", "out", "Nur ein Entwurf, nicht gesendet", T0 + timedelta(minutes=8))
        up(db, "c1", "m9", "unknown", "Systemtext ohne Richtung", T0 + timedelta(minutes=9))
        db.add(Draft(conversation_id="c1", text="Gerne,  der Artikel ist noch   verfügbar.", status="sent", sent_at=T0))
        db.add(Draft(conversation_id="c1", text="Nur ein Entwurf, nicht gesendet", status="draft"))
        db.commit()
    assert style.own_messages() == ["Nur ein Entwurf, nicht gesendet", "ok", "morgen 18h beim billa?",
                                    "passt, 45 geht aber nicht drunter"]
    assert style.own_messages(limit=2) == ["Nur ein Entwurf, nicht gesendet", "ok"]


def test_own_messages_limit_defaults_to_the_maximum():
    _messages(style.MAX_MESSAGES + 7)
    msgs = style.own_messages()
    assert len(msgs) == style.MAX_MESSAGES and msgs[0] == f"nachricht {style.MAX_MESSAGES + 6}"


def test_panel_counts_and_flags():
    sp = style.panel()
    assert (sp["profile"], sp["meta"], sp["count"], sp["min"], sp["enough"], sp["running"]) == \
        ("", {}, 0, style.MIN_MESSAGES, False, False)
    _messages(style.MIN_MESSAGES)
    assert style.panel()["count"] == style.MIN_MESSAGES and style.panel()["enough"] is True


# ------------------------------------------------------------------ deriving
async def test_derive_stores_profile_and_meta(backend):
    texts = _messages(12)
    newest_first = texts[::-1]
    backend.data = {"profile": "  - Du-Form\r\n- kurz, ohne Gruß\r\n", "note": "  eher einheitlich  ",
                    "examples": [newest_first[3], "Frei erfunden, das habe ich nie geschrieben", newest_first[1],
                                 newest_first[3], f" {newest_first[0]}\n"] + newest_first[4:11]}
    meta = await style.derive()
    assert backend.purposes == ["style"] and len(backend.calls) == 1
    call = backend.calls[0]
    assert call["system"] == style.STYLE_SYSTEM and call["schema"] == style.STYLE_SCHEMA
    assert call["prompt"].startswith("Nachrichten, die ich selbst geschrieben habe (neueste zuerst, 12 Stück):\n1. nachricht 11\n2. nachricht 10\n")
    assert call["prompt"].endswith("12. nachricht 0\n\nAufgabe: Beschreibe meinen Schreibstil und wähle die typischsten Nachrichten als Beispiele.")
    assert get_setting("reply_style") == "- Du-Form\n- kurz, ohne Gruß" and style.profile() == "- Du-Form\n- kurz, ohne Gruß"
    # verbatim members of the input only, each once, in Claude's order, at most MAX_STYLE_EXAMPLES
    want = [newest_first[3], newest_first[1], newest_first[0]] + newest_first[4:9]
    assert len(want) == prompts.MAX_STYLE_EXAMPLES
    stored = style.meta()
    assert stored == meta and stored["examples"] == want
    assert stored["count"] == 12 and stored["model"] == "fake-style" and stored["note"] == "eher einheitlich"
    assert datetime.fromisoformat(stored["at"]).tzinfo is not None
    assert style.examples()[:len(want)] == want          # the reply prompt's examples start with them


async def test_derive_caps_the_profile_and_the_prompt_input(backend):
    _messages(style.MAX_MESSAGES + 30)
    backend.data = {"profile": "- " + "x" * (style.MAX_PROFILE * 2), "note": "n" * 500}
    meta = await style.derive()
    assert len(get_setting("reply_style")) == style.MAX_PROFILE and meta["count"] == style.MAX_MESSAGES
    assert len(meta["note"]) == style.MAX_NOTE and meta["examples"] == []
    assert backend.calls[0]["prompt"].count("\n") == style.MAX_MESSAGES + 2      # header + lines + blank + task


async def test_derive_needs_enough_messages(backend):
    _messages(style.MIN_MESSAGES - 1)
    set_setting("reply_style", "- mein Text")
    with pytest.raises(LLMError, match=f"Only {style.MIN_MESSAGES - 1} of your own messages so far – at least {style.MIN_MESSAGES} are needed"):
        await style.derive()
    assert backend.calls == [] and get_setting("reply_style") == "- mein Text" and get_setting("reply_style_meta") == ""


@pytest.mark.parametrize("answer", [{"error": LLMError("Claude timed out")}, {"data": {"profile": "  \r\n "}}])
async def test_a_failed_derivation_leaves_the_settings_untouched(backend, answer):
    _messages(8)
    set_setting("reply_style", "- mein Text")
    set_setting("reply_style_meta", '{"count": 3}')
    backend.error, backend.data = answer.get("error"), answer.get("data", {})
    with pytest.raises(LLMError):
        await style.derive()
    assert get_setting("reply_style") == "- mein Text" and get_setting("reply_style_meta") == '{"count": 3}'


# ------------------------------------------------------------------ the background job
async def test_start_queues_one_job_and_not_a_second_while_it_runs(backend):
    _messages(8)
    backend.gate = asyncio.Event()
    job = style.start()
    assert job is not None and job.kind == "style-derive" and job.lane == "claude" and job.current == "style"
    assert job.label == "Claude derives my writing style" and jobs.busy("style-derive") and style.panel()["running"]
    assert style.start() is None                      # queued or running: no second one
    backend.gate.set()
    await finish_jobs()
    assert job.status == "done" and job.result == "Style profile derived from 8 messages"
    assert not jobs.busy("style-derive") and not style.panel()["running"] and style.profile() == "- Du-Form\n- kurz, ohne Gruß"
    assert len(backend.calls) == 1
    assert style.start() is not None                  # free again
    await finish_jobs()


async def test_start_job_failure_goes_to_the_header(backend):
    _messages(8)
    backend.error = LLMError("Claude timed out")
    job = style.start()
    await finish_jobs()
    assert job.status == "failed" and job.result == "Claude timed out" and style.profile() == ""


# ------------------------------------------------------------------ the page
def test_rules_page_shows_the_card_with_the_button_disabled_until_there_are_enough_messages(client):
    _messages(style.MIN_MESSAGES - 1)
    page = client.get("/settings/rules", headers=EN).text
    card = _card(page)
    assert 'id="style-card"' in page and "Writing style" in card and "data-pending-key" not in card
    assert page.index('id="rules-card"') < page.index('id="style-card"')
    assert " disabled" in _derive_button(page) and "Derive from my messages" in card
    assert f"Only {style.MIN_MESSAGES - 1} of your own messages so far – at least {style.MIN_MESSAGES} are needed" in card
    assert 'name="reply_style"' in card and 'form="general"' in card and 'formaction="/settings"' in card
    _messages(style.MIN_MESSAGES + 1, prefix="mehr")
    page = client.get("/settings/rules", headers=EN).text
    assert " disabled" not in _derive_button(page) and "Not derived yet" in _card(page)


def test_rules_page_shows_the_profile_and_when_it_was_derived(client):
    _messages(6)
    set_setting("reply_style", "- kurz <und> per Du")
    set_setting("reply_style_meta", '{"at": "2026-10-18T10:30:00+00:00", "count": 42, "examples": [], "note": "wenige Daten"}')
    card = _card(client.get("/settings/rules", headers=EN).text)
    assert "- kurz &lt;und&gt; per Du</textarea>" in card
    assert "Derived 18.10. 12:30 from 42 messages" in card and "wenige Daten" in card


def test_derive_post_over_htmx_returns_the_running_card(client, backend):
    _messages(8)
    r = client.post("/settings/style/derive", headers={"hx-request": "true", **EN})
    assert r.status_code == 200 and r.text.lstrip().startswith("<section id=\"style-card\"")
    assert 'data-pending-key="style"' in r.text and "Claude is reading your messages…" in r.text
    assert " disabled" in _derive_button(r.text) and "Deriving…" in r.text
    wait_jobs()
    assert style.profile() == "- Du-Form\n- kurz, ohne Gruß" and style.meta()["count"] == 8
    page = client.get("/settings/rules", headers=EN).text
    assert "data-pending-key" not in _card(page) and "Derived " in _card(page) and "from 8 messages" in _card(page)
    assert len(backend.calls) == 1


def test_derive_post_while_running_just_shows_the_running_card(client, backend, monkeypatch):
    _messages(8)
    monkeypatch.setattr(style.jobs, "busy", lambda prefix="": prefix == "style-derive")
    r = client.post("/settings/style/derive", headers={"hx-request": "true", **EN})
    assert r.status_code == 200 and 'data-pending-key="style"' in r.text
    assert backend.calls == [] and not jobs._active


def test_derive_post_with_too_few_messages_starts_nothing(client, backend):
    _messages(style.MIN_MESSAGES - 1)
    r = client.post("/settings/style/derive", headers={"hx-request": "true", **EN})
    assert r.status_code == 200 and "data-pending-key" not in r.text and " disabled" in _derive_button(r.text)
    assert f"Only {style.MIN_MESSAGES - 1} of your own messages so far" in r.text
    plain = client.post("/settings/style/derive", headers=EN, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"].startswith("/settings/rules?msg=Only%204%20of%20your%20own")
    assert backend.calls == [] and not jobs._active


def test_derive_post_without_htmx_redirects_to_the_rules_page(client, backend):
    _messages(8)
    r = client.post("/settings/style/derive", headers=EN, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/rules?msg=Claude%20derives%20your%20writing%20style%20in%20the%20background"
    wait_jobs()
    assert style.profile() and len(backend.calls) == 1
    assert client.get(r.headers["location"], headers=EN).status_code == 200


# ------------------------------------------------------------------ saving the profile with the rules form
def test_rules_save_stores_the_profile_and_a_post_without_it_keeps_it(client):
    r = client.post("/settings", data={"section": "rules", "reply_rules": "Meine Regel",
                                       "reply_style": "  - kurz\r\n- per Du  "}, follow_redirects=False)
    assert r.status_code == 303 and get_setting("reply_rules") == "Meine Regel"
    assert get_setting("reply_style") == "- kurz\n- per Du"
    client.post("/settings", data={"section": "rules", "reply_rules": "Andere Regel"})
    assert get_setting("reply_rules") == "Andere Regel" and get_setting("reply_style") == "- kurz\n- per Du"
    client.post("/settings", data={"section": "rules", "reply_rules": "x", "reply_style": ""})
    assert get_setting("reply_style") == ""            # an emptied textarea clears it
    client.post("/settings", data={"section": "rules", "reply_rules": "x", "reply_style": "y" * (style.MAX_PROFILE + 50)})
    assert len(get_setting("reply_style")) == style.MAX_PROFILE
