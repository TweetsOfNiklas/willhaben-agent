"""Minimum price per own ad: prompt floor, code-side guard, euro regex, my-ads form."""
import pytest

from app.db import Conversation, Message, MyAd, session, set_setting, utcnow
from app.llm.base import LLMResult
from app.llm.prompts import auto_rules
from app.services import chats, min_price


# ------------------------------------------------------------------ regex
@pytest.mark.parametrize("text, amounts", [
    ("Für 25 € gerne", [25]),
    ("€ 25 passt", [25]),
    ("€25", [25]),
    ("Ok, 25,- und er gehört dir", [25]),
    ("25.- und fertig", [25]),
    ("25 Euro wären ok", [25]),
    ("25euro", [25]),
    ("EUR 18.50", [18.5]),
    ("18,50 €", [18.5]),
    ("1.000 € ist mein Preis", [1000]),
    ("Um 18 Uhr in Wien 1150, 20€", [20]),
    ("um 18.30 Uhr passt", []),
    ("Ist noch da.", []),
    ("20 € + 5 € Versand", [20]),
    ("Versand kostet 4,50 €", []),
    ("10 € inkl. Versand", [10]),
    ("Versand ist dabei, 10 €", [10]),
    ("Porto: 5 €, Preis 12 €", [12]),
    # qualified shipping word / bundle: total or item price, must count
    ("Inkl. Versand 15 €", [15]),
    ("Mit Versand 15 €", [15]),
    ("Mit Versand: 15 €", [15]),
    ("mit Versand wären 15 €", [15]),
    ("Ohne Versand 15 €", [15]),
    ("inkl. Versand nach Österreich sind 15 €", [15]),
    ("Das ganze Paket kostet 15 €", [15]),
    ("Paketpreis 15 €", [15]),
])
def test_euro_amounts(text, amounts):
    assert min_price.euro_amounts(text) == amounts


@pytest.mark.parametrize("raw, value", [("", None), ("  ", None), ("25", 25), ("25,50", 25.5), ("25.5", 25.5),
                                        ("€ 1.000", 1000), ("30,-", 30)])
def test_parse_form(raw, value):
    assert min_price.parse_form(raw) == value


@pytest.mark.parametrize("raw", ["abc", "-5", "2x"])
def test_parse_form_rejects(raw):
    with pytest.raises(ValueError):
        min_price.parse_form(raw)


def test_blockers():
    assert min_price.blockers(None, 5, "Für 5 € gerne") == []                 # no floor: model decides as before
    assert min_price.blockers(20, 20, "Für 20 € gerne") == []                  # at the floor
    assert min_price.blockers(20, 25, "Gegenangebot: 25 €") == []              # above
    assert min_price.blockers(20, None, "Unter 20 € geht leider nicht.") == []
    assert "Suggested price 15 €" in min_price.blockers(20, 15, "Passt so")[0]
    assert "Answer names 18 €" in min_price.blockers(20, None, "Ok, 18,- und er gehört dir")[0]
    assert "19,50 €" in min_price.blockers(20, None, "Ok, € 19.50")[0]


@pytest.mark.parametrize("text", ["Gerne, inkl. Versand 15 €", "Mit Versand 15 €", "Ohne Versand 15 €",
                                  "Paketpreis 15 €", "Das ganze Paket kostet 15 €"])
def test_blockers_qualified_shipping(text):
    assert min_price.blockers(30, None, text)                 # no suggested_price: the text guard alone must block


@pytest.mark.parametrize("value", [25, 25.5, 12345.67, 12345.64, 0.5, 1000000, 1234567.89])
def test_form_value_round_trip(value):
    from app.main import templates
    tpl = templates.env.from_string(
        "{{ ('%.2f'|format(v)).rstrip('0').rstrip('.') if v is not none else '' }}")
    assert min_price.parse_form(tpl.render(v=value)) == value


# ------------------------------------------------------------------ prompt
def test_prompt_contains_floor_and_asking_price():
    p = auto_rules("nur Verfügbarkeit", "Netzteil dabei", 20, "€ 30")
    assert "Untergrenze 20 €" in p and "Angebotspreis € 30" in p and "Nie unter 20 €" in p
    assert "Untergrenze" not in auto_rules("x", None)


# ------------------------------------------------------------------ end to end with a fake backend
class _Backend:
    model = "fake"

    def __init__(self):
        self.data, self.calls = {}, []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append(prompt)
        base = {"reply": "Für 25 € gehört er dir.", "intent": "price_negotiation", "needs_info": [],
                "suggested_price": 25, "note": "", "auto_send": True, "auto_reason": "Angebot über Untergrenze",
                "meetup": {"start": "", "minutes": 30, "location": ""}}
        return LLMResult(data={**base, **self.data}, model=self.model)


class _Account:
    def __init__(self):
        self.sent = []

    async def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return "m1"


@pytest.fixture
def env(monkeypatch):
    backend, account, notes = _Backend(), _Account(), []
    monkeypatch.setattr(chats, "get_backend", lambda *a: backend)
    monkeypatch.setattr(chats, "_account", lambda: account)

    async def no_ctx(cid):
        with session() as db:
            return db.get(Conversation, cid)

    async def fake_notify(kind, title, message, **kw):
        notes.append((kind, title, message, kw))
        return True

    monkeypatch.setattr(chats, "ensure_ad_context", no_ctx)
    monkeypatch.setattr(chats, "notify", fake_notify)
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Lampe", auto_reply=True, min_price=20))
        db.add(Conversation(id="c-min", ad_id="1000000001", ad_title="Lampe", partner_name="Max Muster", role="seller",
                            unread_count=1, ad_context={"title": "Lampe", "price": "€ 30"}))
        db.add(Message(conversation_id="c-min", ext_id="m0", direction="in", text="Nimmst du 25?", created_at=utcnow()))
        db.commit()
    set_setting("send_verified", "1")
    return backend, account, notes


async def test_auto_sends_at_or_above_floor(env):
    backend, account, notes = env
    d = await chats.auto_reply("c-min")
    assert d.status == "sent" and account.sent == [("c-min", "Für 25 € gehört er dir.")]
    assert "Untergrenze 20 €" in backend.calls[0] and "Angebotspreis € 30" in backend.calls[0]


@pytest.mark.parametrize("change, reason", [
    ({"suggested_price": 15, "reply": "Passt, abgemacht."}, "Suggested price 15 €"),
    ({"suggested_price": None, "reply": "Ok, 18,- und er gehört dir."}, "Answer names 18 €"),
    ({"suggested_price": 25, "reply": "Für € 19 geht's auch."}, "Answer names 19 €"),
])
async def test_auto_blocked_below_floor(env, change, reason):
    backend, account, notes = env
    backend.data = change
    d = await chats.auto_reply("c-min")
    assert d.status == "draft" and not account.sent and reason in d.auto_reason
    assert notes[-1][0] == "auto_reply_pending"                       # goes through offer_draft (approval push)


async def test_manual_draft_gets_floor_too(env):
    backend, account, notes = env
    await chats.draft_reply("c-min")
    assert "Untergrenze 20 €" in backend.calls[0]


async def test_no_floor_keeps_old_behavior(env):
    backend, account, notes = env
    with session() as db:
        ad = db.get(MyAd, "1000000001")
        ad.min_price = None
        db.add(ad)
        db.commit()
    backend.data = {"suggested_price": 5, "reply": "Für 5 € gehört er dir."}
    d = await chats.auto_reply("c-min")
    assert d.status == "sent"                    # model said auto_send; no floor -> no code-side price check
    assert "Untergrenze" not in backend.calls[0]


# ------------------------------------------------------------------ my-ads form
def test_route_saves_min_price():
    from fastapi.testclient import TestClient

    from app.main import app
    with session() as db:
        db.add(MyAd(ad_id="1000000002", heading="Rad"))
        db.commit()
    with TestClient(app) as c:
        assert 'name="min_price"' in c.get("/myads?show_inactive=1").text
        c.post("/myads/1000000002/auto-reply", data={"instructions": "Abholung Wien", "min_price": "25,50"})
        with session() as db:
            ad = db.get(MyAd, "1000000002")
        assert ad.min_price == 25.5 and ad.auto_reply_instructions == "Abholung Wien" and not ad.auto_reply
        assert 'value="25.5"' in c.get("/myads?show_inactive=1").text

        r = c.post("/myads/1000000002/auto-reply", data={"instructions": "neu", "min_price": "abc"})
        assert "number" in r.text or r.status_code in (200, 303)
        with session() as db:
            ad = db.get(MyAd, "1000000002")
        assert ad.min_price == 25.5 and ad.auto_reply_instructions == "neu"   # junk floor: floor kept, rest saved

        c.post("/myads/1000000002/auto-reply", data={"instructions": "neu", "min_price": "12345,67"})
        assert 'value="12345.67"' in c.get("/myads?show_inactive=1").text     # no precision loss in the rendered value

        c.post("/myads/1000000002/auto-reply", data={"instructions": "x", "min_price": ""})  # emptied: floor cleared
        with session() as db:
            assert db.get(MyAd, "1000000002").min_price is None
