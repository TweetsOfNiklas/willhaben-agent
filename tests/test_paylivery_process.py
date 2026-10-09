"""PayLivery process: stage detection for both roles, state and history, card, settings, actions and their guards.

The browser helpers of paylivery (_collect, _click, _fill, _bank_reason ...) are replaced by fakes, so the step loop
runs without Chromium. One test uses a real Chromium page when one is installed (skipped otherwise).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.db import Conversation, Message, Shipment, get_setting, session, set_setting, utcnow
from app.llm.base import LLMResult
from app.services import paylivery, shipping
from app.willhaben import session as willhaben_session
from app.willhaben.account import parse_messages
from app.willhaben.parse import parse_detail
from tests.conftest import load

CHAT = "https://www.willhaben.at/iad/myprofile/chat"
AD_URL = "https://www.willhaben.at/iad/object?adId=77"
AD_CTX = {"title": "Kinderbuch", "url": AD_URL, "price": "€ 12", "paylivery": True, "buy_now": True}


# ------------------------------------------------------------------ stage detection
@pytest.mark.parametrize("text, role, stage", [
    ("Preisvorschlag erhalten: € 120,00", "seller", "offer_received"),
    ("Preisvorschlag gesendet: € 100", "buyer", "offer_sent"),
    ("Preisvorschlag", "seller", "offer_received"),          # no direction word: the role decides
    ("Preisvorschlag", "buyer", "offer_sent"),
    ("Dein Preisvorschlag wurde angenommen", "buyer", "offer_accepted"),
    ("Preisvorschlag abgelehnt", "buyer", "offer_declined"),
    ("Der Preisvorschlag wurde nicht angenommen", "buyer", "offer_declined"),
    ("Zahlung eingegangen: € 12,34", "seller", "paid"),
    ("Du hast bezahlt", "buyer", "paid"),
    ("Dein Paketschein steht bereit", "seller", "label_ready"),
    ("Bezahlt. Versandlabel steht bereit", "seller", "label_ready"),   # most advanced stage wins
    ("Paket verschickt, Sendungsnummer: ABC12345678", "seller", "shipped"),
    ("Paket zugestellt", "buyer", "delivered"),
    ("Erhalt bestätigt", "seller", "received"),
    ("Problem gemeldet", "seller", "problem"),
    ("Auszahlung erfolgt", "seller", "paid_out"),
    ("Vorgang storniert", "buyer", "cancelled"),
    ("Sofort gekauft", "seller", "offer_accepted"),
    ("Du hast gekauft", "buyer", "paid"),
])
def test_stage_patterns(text, role, stage):
    assert paylivery.detect_stage(text, role) == stage


@pytest.mark.parametrize("text", [
    "Ist das noch da?",
    "PayLivery: sicher bezahlen",
    "Der Käufer hat nicht bezahlt",
    "Du hast unbezahlt gelassen",
    "Dank QR-Code kannst du ab jetzt ganz ohne Drucker versenden.",
    "Das Versandetikett lässt sich bequem ausdrucken.",
    "Angebote, die auch PayLivery anbieten, werden öfter geklickt. Die Zahlung passiert in Echtzeit.",
    "Erhalte jetzt Fixpreis-Angebote ohne zu verhandeln. Du entscheidest weiterhin, ob du annimmst oder ablehnst.",
])
@pytest.mark.parametrize("role", ["seller", "buyer"])
def test_generic_text_sets_no_stage(text, role):
    assert paylivery.detect_stage(text, role) is None


def test_structured_parameters_are_matched_too():
    params = {"status": {"label": "Preisvorschlag erhalten"}, "amount": 99.5}
    assert paylivery.detect_stage(paylivery._full_text("", params), "seller") == "offer_received"
    assert paylivery.notice_facts("", params, None)["amount"] == 99.5


def test_facts_amount_deadline_tracking_and_label():
    from datetime import datetime, timezone

    created = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    f = paylivery.notice_facts("Bitte bis 12.10. verschicken. Sendungsnummer: DX123456789 € 12,50", None, created)
    assert f["deadline"].startswith("2026-10-12") and f["tracking"] == "DX123456789" and f["amount"] == 12.5
    f = paylivery.notice_facts("Innerhalb von 7 Tagen", None, created)
    assert f["deadline"].startswith("2026-10-08")
    f = paylivery.notice_facts("Paket verschickt", None, created)       # a word is not a tracking number
    assert f["tracking"] is None
    f = paylivery.notice_facts("Label", None, created)
    assert f["label_url"] is None
    f = paylivery.notice_facts("Dein Etikett: https://www.willhaben.at/iad/paketschein/x.pdf", None, created)
    assert f["label_url"] == "https://www.willhaben.at/iad/paketschein/x.pdf"


def test_ad_detail_flags_paylivery_and_buy_now():
    """Fixture ad: buy-now (Sofort kaufen) but PayLivery switched off on the ad."""
    d = parse_detail(load("detail_1662146981_nextdata.json"))
    assert d.buy_now is True and d.paylivery_enabled is False
    ctx = d.as_context()
    assert ctx["buy_now"] is True and ctx["paylivery"] is False
    data = load("detail_1662146981_nextdata.json")
    data["advertDetails"]["advertisingParameters"]["payliveryEnabled"] = ["true"]
    assert parse_detail(data).paylivery_enabled is True


def test_parse_messages_keeps_parameters():
    data = {"messages": [{"message_id": "n1", "type": "integration_message", "text": "Hinweis",
                          "parameters": {"status": "Preisvorschlag erhalten", "amount": 5}}]}
    [m] = parse_messages(data, set())
    assert m.params == {"status": "Preisvorschlag erhalten", "amount": 5}


# ------------------------------------------------------------------ state and history
def _chat(cid: str = "c1", role: str = "seller", ad_ctx: dict | None = None, state: dict | None = None) -> None:
    with session() as db:
        db.add(Conversation(id=cid, ad_id="77", role=role, partner_name="Max", ad_title="Kinderbuch",
                            ad_context=AD_CTX if ad_ctx is None else ad_ctx, paylivery=state))
        db.commit()


def _notice(ext_id: str, text: str, cid: str = "c1", minutes_ago: int = 5, params=None) -> Message:
    with session() as db:
        m = Message(conversation_id=cid, ext_id=ext_id, direction="unknown", text=text, kind="integration",
                    created_at=utcnow() - timedelta(minutes=minutes_ago), params=params)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m


def _state(cid: str = "c1") -> dict:
    with session() as db:
        return dict(db.get(Conversation, cid).paylivery or {})


from datetime import timedelta  # noqa: E402  (used by the helpers above)


async def test_state_is_written_and_survives_a_reload():
    _chat()
    m = _notice("o1", "Preisvorschlag erhalten: € 120")
    with session() as db:
        conv = db.get(Conversation, "c1")
    changed = paylivery.apply_notice(conv, m)
    assert changed["stage"] == "offer_received" and changed["offer"] == 120.0
    fresh = _state()                                          # a new session reads what was written
    assert fresh["stage"] == "offer_received" and fresh["history"][0]["text"] == "Preisvorschlag erhalten: € 120"
    assert fresh["role"] == "seller" and fresh["applied"] == ["o1"]


async def test_a_notice_is_applied_once():
    _chat()
    m = _notice("o1", "Preisvorschlag erhalten: € 120")
    with session() as db:
        conv = db.get(Conversation, "c1")
    assert paylivery.apply_notice(conv, m) is not None
    assert paylivery.apply_notice(conv, m) is None
    assert len(_state()["history"]) == 1


async def test_history_is_capped_at_30():
    _chat(state={"role": "seller", "stage": None, "history": [{"at": "x", "stage": None, "text": str(i)}
                                                              for i in range(30)]})
    with session() as db:
        conv = db.get(Conversation, "c1")
    paylivery.apply_notice(conv, _notice("o1", "Preisvorschlag erhalten: € 5"))
    hist = _state()["history"]
    assert len(hist) == 30 and hist[-1]["text"] == "Preisvorschlag erhalten: € 5" and hist[0]["text"] == "1"


async def test_stage_never_goes_back_after_payment():
    _chat(state={"role": "seller", "stage": "paid", "history": []})
    with session() as db:
        conv = db.get(Conversation, "c1")
    assert paylivery.apply_notice(conv, _notice("o1", "Preisvorschlag erhalten: € 5")) is None
    assert _state()["stage"] == "paid"
    assert paylivery.apply_notice(conv, _notice("c2", "Problem gemeldet")) is not None   # problems always apply
    assert _state()["stage"] == "problem"


async def test_offer_may_restart_before_payment():
    _chat(state={"role": "buyer", "stage": "offer_declined", "history": []})
    with session() as db:
        conv = db.get(Conversation, "c1")
    paylivery.apply_notice(conv, _notice("o2", "Preisvorschlag gesendet: € 90", cid="c1"))
    assert _state()["stage"] == "offer_sent" and _state()["offer"] == 90.0


async def test_buyer_notice_never_creates_or_marks_a_shipment(monkeypatch):
    _chat(cid="b1", role="buyer")
    _notice("p1", "Zahlung eingegangen: € 12,34", cid="b1")
    called = []

    async def fake_detect(*a, **k):
        called.append("detect")

    monkeypatch.setattr(shipping, "detect", fake_detect)
    assert await paylivery.scan("b1") is None
    with session() as db:
        assert db.exec(select(Shipment)).all() == []
    assert called == []
    assert _state("b1")["stage"] == "paid"                    # the state still follows the notice


async def test_scan_applies_notices_of_both_roles_and_marks_them_seen(monkeypatch):
    _chat()
    _notice("o1", "Preisvorschlag erhalten: € 120", minutes_ago=20)
    _notice("o2", "Preisvorschlag angenommen", minutes_ago=10)
    await paylivery.scan("c1")
    assert _state()["stage"] == "offer_accepted"
    assert json.loads(get_setting("paylivery_seen")) == ["c1:o1", "c1:o2"]


# ------------------------------------------------------------------ pushes
async def test_push_only_for_recent_stage_changes(monkeypatch):
    pushes = []

    async def fake_notify(kind, title, message, **kw):
        pushes.append((kind, title, kw.get("url")))
        return True

    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    _chat()
    _notice("o0", "Preisvorschlag erhalten: € 90", minutes_ago=60 * 24 * 30)      # old: history only, no push
    _notice("p1", "Zahlung eingegangen: € 120", minutes_ago=5)                    # recent stage change: push
    _notice("o1", "Preisvorschlag erhalten: € 130", minutes_ago=4)                # same stage again: no push
    await paylivery.scan("c1")
    assert len(pushes) == 1 and pushes[0][0] == "paylivery"
    assert pushes[0][2].endswith("/chats/c1#paylivery")


async def test_push_can_be_switched_off(monkeypatch):
    pushes = []

    async def fake_notify(kind, title, message, **kw):
        pushes.append(kind)
        return True

    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    set_setting("paylivery_notify", "0")
    _chat()
    _notice("o1", "Preisvorschlag erhalten: € 120", minutes_ago=5)
    await paylivery.scan("c1")
    assert pushes == [] and _state()["stage"] == "offer_received"


# ------------------------------------------------------------------ card
def _card(role, stage, ad_ctx=None, buy_on=True):
    set_setting("paylivery_buy_allowed", "1" if buy_on else "0")
    conv = SimpleNamespace(id="c1", role=role, ad_context=AD_CTX if ad_ctx is None else ad_ctx,
                           paylivery={"role": role, "stage": stage, "history": [], "offer": 120.0} if stage else None,
                           partner_name="Max", ad_title="Kinderbuch")
    return paylivery.card(conv)


def test_card_hidden_without_state_and_without_paylivery():
    assert _card("seller", None, ad_ctx={"paylivery": False}) is None
    assert _card("buyer", None, ad_ctx={}) is None


def test_card_seller_offer_received_has_accept_and_decline():
    c = _card("seller", "offer_received")
    assert [a["name"] for a in c["actions"]] == ["accept", "decline"]
    assert c["stage_label"] == "Offer received" and c["offer"] == 120.0          # the test locale is English


def test_card_seller_paid_offers_label_and_mark_shipped():
    assert [a["name"] for a in _card("seller", "paid")["actions"]] == ["label", "mark_shipped"]
    assert _card("seller", "delivered")["actions"] == []


def test_card_buyer_offers_offer_and_buy_now_disabled_when_switched_off():
    c = _card("buyer", None, buy_on=False)
    names = {a["name"]: a for a in c["actions"]}
    assert set(names) == {"offer", "buy"}
    assert names["buy"]["disabled"] and names["offer"]["disabled"] and names["buy"]["note"]


def test_card_buy_now_only_with_both_flags():
    no_buy_now = _card("buyer", None, ad_ctx={**AD_CTX, "buy_now": False})
    assert [a["name"] for a in no_buy_now["actions"]] == ["offer"]
    accepted = _card("buyer", "offer_accepted")
    assert [a["name"] for a in accepted["actions"]] == ["buy"] and not accepted["actions"][0]["disabled"]


def test_card_buyer_delivered_confirms_or_reports():
    assert [a["name"] for a in _card("buyer", "delivered")["actions"]] == ["receipt", "problem"]


def test_card_history_is_newest_first():
    conv = SimpleNamespace(id="c1", role="seller", ad_context=AD_CTX, partner_name="x", ad_title="y",
                           paylivery={"role": "seller", "stage": "paid", "offer": None,
                                      "history": [{"at": "1", "stage": "offer_received", "text": "first"},
                                                  {"at": "2", "stage": "paid", "text": "second"}]})
    assert [h["text"] for h in paylivery.card(conv)["history"]] == ["second", "first"]


def test_settings_card_defaults():
    assert paylivery.card(None) == {"buy_allowed": False, "notify": True}
    paylivery.save_settings(buy_allowed=True, notify=False)
    assert paylivery.card(None) == {"buy_allowed": True, "notify": False}


# ------------------------------------------------------------------ routes, pages and settings
def test_chat_page_shows_the_card():
    from fastapi.testclient import TestClient

    from app.main import app

    _chat(state={"role": "seller", "stage": "offer_received", "offer": 120.0, "history": [],
                 "applied": []})
    with TestClient(app) as c:
        r = c.get("/chats/c1")
    assert r.status_code == 200
    assert 'id="paylivery"' in r.text and "Offer received" in r.text
    assert "/chats/c1/paylivery/accept" in r.text and "/chats/c1/paylivery/decline" in r.text


def test_settings_route_saves_switches():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/settings/paylivery", data={"buy_allowed": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("#paylivery")
        assert get_setting("paylivery_buy_allowed") == "1" and get_setting("paylivery_notify") == "0"
        page = c.get("/settings")
    assert page.status_code == 200 and 'id="paylivery"' in page.text


def test_action_route_refuses_buying_while_switched_off():
    from fastapi.testclient import TestClient

    from app.main import app

    _chat(cid="b1", role="buyer")
    with TestClient(app) as c:
        r = c.post("/chats/b1/paylivery/buy", data={"amount": ""}, follow_redirects=False)
    assert r.status_code == 303 and "#paylivery" in r.headers["location"]
    assert "switched off" in r.headers["location"] or "ausgeschaltet" in r.headers["location"] or "msg=" in r.headers["location"]


# ------------------------------------------------------------------ browser action guards (pure)
@pytest.mark.parametrize("action, label, verdict", [
    ("accept", "Angebot annehmen", "final"),
    ("accept", "Kaufen", "refused"),                 # another action's commit button
    ("accept", "Verkaufen", "neutral"),              # no word-boundary match
    ("accept", "Alle akzeptieren", "refused"),       # cookie banner words
    ("accept", "Bedingungen akzeptieren", "refused"),
    ("accept", "Konto löschen", "refused"),
    ("accept", "", "refused"),
    ("accept", "Nachricht lesen", "neutral"),
    ("decline", "Ablehnen", "final"),
    ("buy", "Sofort kaufen", "final"),
    ("buy", "Zur Kasse", "neutral"),
    ("buy", "Bezahlen", "final"),
    ("buy", "Angebot annehmen", "refused"),
    ("offer", "Preisvorschlag senden", "final"),
    ("offer", "Nachricht senden", "neutral"),
    ("receipt", "Erhalt bestätigen", "final"),
    ("problem", "Problem melden", "final"),
])
def test_click_policy(action, label, verdict):
    assert paylivery.click_verdict(action, label) == verdict


@pytest.mark.parametrize("field, allowed", [
    ({"type": "password", "label": "Passwort"}, False),
    ({"type": "text", "name": "cc-number", "autocomplete": "cc-number"}, False),
    ({"type": "text", "label": "IBAN"}, False),
    ({"type": "checkbox", "label": "Ich akzeptiere die AGB"}, False),
    ({"type": "checkbox", "label": "Paket versichern"}, True),
    ({"type": "text", "label": "Betrag"}, True),
])
def test_fill_policy(field, allowed):
    assert paylivery.fill_allowed(field) is allowed


def test_settings_default_is_buying_off():
    assert paylivery.settings_state()["buy_allowed"] is False


# ------------------------------------------------------------------ browser actions with fakes
class FakeSess:
    def __init__(self, page, capturing: bool = False):
        self.started = True
        self.capturing = capturing
        self.ui_page = page
        self.opened: list[str] = []
        self.starts: list[str] = []
        self.stops = 0

    async def open_url(self, url):
        self.opened.append(url)

    def start_capture(self, label):
        self.starts.append(label)
        self.capturing = True

    async def stop_capture(self):
        self.stops += 1
        self.capturing = False
        return Path("/tmp/capture-1.json")


class FakeBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, *, system, prompt, schema, images=None):
        self.prompts.append(prompt)
        return LLMResult(data=self.replies.pop(0), model="fake")


def _step(**kw):
    base = {"fills": [], "action_idx": None, "status": "continue", "note": ""}
    return {**base, **kw}


@pytest.fixture
def browser(monkeypatch):
    """Fake page + session + Claude; helpers replaced so no browser is needed. Returns a namespace to script."""
    async def noop(*a, **k):
        return None

    ns = SimpleNamespace(page=SimpleNamespace(url=CHAT), elements=[], clicked=[], filled=[], bank=None,
                         replies=[], sess=None, backend=None)

    async def collect(page):
        return list(ns.elements)

    async def click(page, el):
        ns.clicked.append(el["label"])

    async def fill(page, el, value):
        ns.filled.append((el["idx"], value))

    async def bank(page):
        return ns.bank

    async def card_visible(page):
        return False

    monkeypatch.setattr(paylivery, "_settle", noop)
    monkeypatch.setattr(paylivery, "_dismiss", noop)
    monkeypatch.setattr(paylivery, "_collect", collect)
    monkeypatch.setattr(paylivery, "_click", click)
    monkeypatch.setattr(paylivery, "_fill", fill)
    monkeypatch.setattr(paylivery, "_bank_reason", bank)
    monkeypatch.setattr(paylivery, "_card_fields_visible", card_visible)
    ns.sess = FakeSess(ns.page)
    monkeypatch.setattr(willhaben_session, "get_session", lambda: ns.sess)
    ns.backend = FakeBackend([])
    monkeypatch.setattr("app.llm.base.get_backend", lambda *a, **k: ns.backend)
    return ns


def _btn(idx: int, label: str) -> dict:
    return {"idx": idx, "kind": "button", "tag": "button", "type": "button", "name": "", "label": label,
            "text": label, "placeholder": "", "autocomplete": "", "aria": "", "value": ""}


def _field(idx: int, label: str, type_: str = "text", name: str = "") -> dict:
    return {"idx": idx, "kind": "field", "tag": "input", "type": type_, "name": name, "label": label,
            "placeholder": "", "autocomplete": "", "aria": "", "value": ""}


async def test_accept_offer_clicks_once_and_moves_the_stage(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.elements = [_btn(1, "Angebot annehmen"), _btn(2, "Ablehnen")]
    browser.backend.replies = [_step(action_idx=1, status="done", note="angenommen")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == ["Angebot annehmen"]
    assert browser.sess.opened == [CHAT]
    assert browser.sess.starts == ["paylivery-accept"] and browser.sess.stops == 1
    assert "/tmp/capture-1.json" not in msg and "capture-1.json" in msg
    st = _state()
    assert st["stage"] == "offer_accepted" and st["history"][-1]["text"].startswith("accept:")


async def test_never_clicks_another_actions_commit_button(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.elements = [_btn(1, "Kaufen"), _btn(2, "Angebot annehmen")]
    browser.backend.replies = [_step(action_idx=1, status="continue")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == [] and "refused" in msg
    assert _state()["stage"] == "offer_received"              # not moved by a refused click
    assert browser.sess.stops == 1                             # the recording is still stopped


async def test_done_without_a_confirm_button_is_not_success(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.elements = [_btn(1, "Nachricht lesen")]
    browser.backend.replies = [_step(action_idx=1, status="done", note="fertig?")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == ["Nachricht lesen"]
    assert "no confirm" in msg or "confirmed" in msg
    assert _state()["stage"] == "offer_received"


async def test_bank_page_stops_for_the_user(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.bank = "Zahlungsbestätigung (3-D Secure / TAN)"
    browser.elements = [_btn(1, "Angebot annehmen")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == [] and browser.backend.prompts == []
    assert "/session" in msg or "Session" in msg
    assert browser.sess.stops == 1


async def test_recording_started_by_the_user_is_left_alone(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.sess.capturing = True
    browser.elements = [_btn(1, "Angebot annehmen")]
    browser.backend.replies = [_step(action_idx=1, status="done")]
    await paylivery.run_action("c1", "accept")
    assert browser.sess.starts == [] and browser.sess.stops == 0


async def test_offer_amount_is_the_confirmed_one_not_claudes(browser, monkeypatch):
    monkeypatch.setattr(paylivery.willhaben_session, "get_session", lambda: browser.sess)
    set_setting("paylivery_buy_allowed", "1")
    _chat(cid="b1", role="buyer")
    browser.elements = [_btn(1, "Preisvorschlag"), _field(2, "Betrag in €", "text", "amount")]
    browser.backend.replies = [
        _step(fills=[{"idx": 2, "value": "999"}], action_idx=1, status="continue"),
        _step(action_idx=3, status="done", note="gesendet"),
    ]
    browser.elements.append(_btn(3, "Preisvorschlag senden"))
    msg = await paylivery.run_action("b1", "offer", 120.0)
    assert set(browser.filled) == {(2, "120,00")}             # the code's amount (German style for a text field), never 999
    assert browser.clicked == ["Preisvorschlag", "Preisvorschlag senden"]
    assert "gesendet" in msg
    assert _state("b1")["stage"] == "offer_sent" and _state("b1")["history"][-1]["text"].startswith("offer:")


async def test_buy_refused_while_the_setting_is_off(browser):
    _chat(cid="b1", role="buyer")
    with pytest.raises(paylivery.PayLiveryError):
        await paylivery.run_action("b1", "buy", 12.0)
    assert browser.sess.opened == [] and browser.sess.starts == []


async def test_buy_needs_paylivery_and_a_price(browser):
    set_setting("paylivery_buy_allowed", "1")
    _chat(cid="b1", role="buyer", ad_ctx={**AD_CTX, "paylivery": False})
    with pytest.raises(paylivery.PayLiveryError):
        await paylivery.run_action("b1", "buy", 12.0)
    with pytest.raises(paylivery.PayLiveryError):
        paylivery.check_action(SimpleNamespace(role="buyer", paylivery={}, ad_context={"paylivery": True}),
                               "buy", None)


async def test_buy_never_fills_card_fields_itself(browser, monkeypatch):
    set_setting("paylivery_buy_allowed", "1")
    _chat(cid="b1", role="buyer")
    browser.elements = [_field(1, "Kartennummer", "text", "cardnumber"), _btn(2, "Sofort kaufen")]
    monkeypatch.setattr(paylivery, "_card_fields_visible", _true)
    browser.backend.replies = []
    msg = await paylivery.run_action("b1", "buy", 12.0)
    assert browser.filled == [] and browser.clicked == []
    assert "card" in msg or "Karte" in msg or "Kartendaten" in msg or "card details" in msg


async def _true(page):
    return True


async def test_label_and_mark_shipped_are_local(browser):
    _chat(state={"role": "seller", "stage": "paid", "history": [], "applied": []})
    msg = await paylivery.start_action("c1", "mark_shipped")
    assert "Marked" in msg or "markiert" in msg
    assert _state()["stage"] == "shipped"
    assert browser.sess.opened == []                          # no browser for a local action


async def test_download_label_without_link_says_so(browser):
    with session() as db:
        db.add(Conversation(id="c1", role="seller", paylivery={"role": "seller", "stage": "paid", "history": []}))
        db.add(Shipment(conversation_id="c1", status="awaiting"))
        db.commit()
    with pytest.raises(paylivery.PayLiveryError):
        await paylivery.download_label("c1")


async def test_action_refused_for_the_other_role(browser):
    _chat(cid="b1", role="buyer")
    with pytest.raises(paylivery.PayLiveryError):
        paylivery.check_action(_conv("b1"), "accept", None)


async def test_start_action_is_background_for_browser_actions(browser, monkeypatch):
    set_setting("paylivery_buy_allowed", "1")
    _chat(cid="b1", role="buyer")
    started = []

    async def fake_run(cid, action, amount=None):
        started.append((cid, action, amount))
        return "ok"

    monkeypatch.setattr(paylivery, "run_action", fake_run)
    msg = await paylivery.start_action("b1", "offer", "120,50")
    assert "Started" in msg or "Im Browser" in msg
    await asyncio.gather(*paylivery._tasks)
    assert started == [("b1", "offer", 120.5)]


def _conv(cid: str):
    with session() as db:
        return db.get(Conversation, cid)


# ------------------------------------------------------------------ real Chromium (skipped when not installed)
@pytest.fixture
async def chromium_page():
    pw_mod = pytest.importorskip("playwright.async_api")
    async with pw_mod.async_playwright() as pw:
        browser = None
        for kwargs in ({}, {"channel": "chrome"}):
            try:
                browser = await pw.chromium.launch(**kwargs)
                break
            except Exception:  # noqa: BLE001 - not installed under this name
                continue
        if browser is None:
            pytest.skip("no Chromium or Google Chrome for Playwright")
        page = await browser.new_page()
        yield page
        await browser.close()


async def test_collector_and_click_on_a_real_page(chromium_page):
    page = chromium_page
    await page.set_content("""<html><body>
      <input name="betrag" placeholder="Betrag">
      <input type="password" name="pw">
      <button id="ok" onclick="window.clicked = (window.clicked||0)+1">Angebot annehmen</button>
      <button onclick="window.bad = 1">Kaufen</button>
    </body></html>""")
    elements = await paylivery._collect(page)
    labels = [e.get("label") for e in elements]
    assert "Angebot annehmen" in labels and "Kaufen" in labels
    assert not any(e.get("name") == "pw" for e in elements)               # password fields never reach Claude
    accept = next(e for e in elements if e.get("label") == "Angebot annehmen")
    assert paylivery.click_verdict("accept", accept["label"]) == "final"
    await paylivery._click(page, accept)
    assert await page.evaluate("window.clicked") == 1
    assert await paylivery._bank_reason(page) is None
