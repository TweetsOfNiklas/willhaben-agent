import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.config import get_settings
from app.db import Conversation, Message, Shipment, get_setting, session, utcnow
from app.services import chats, paylivery, shipping
from app.willhaben import session as willhaben_session
from app.willhaben.account import ChatMessage, parse_messages

LABEL = "https://www.willhaben.at/iad/paketschein/abc.pdf"


def test_parse_messages_tags_notices_and_links():
    data = {"messages": [
        {"message_id": "n1", "type": "integration_message", "text": "Zahlung eingegangen: € 12,34",
         "is_mine": False, "created_at": "2026-10-08T16:00:00Z",
         "parameters": {"link": {"url": LABEL}}},
        {"message_id": "n2", "type": "system_message", "created_at": "2026-10-08T16:01:00Z",
         "attachment": {"href": "https://www.willhaben.at/x/label"}},          # no text: kept, it has a link
        {"message_id": "m3", "type": "text", "text": "Ja", "is_mine": False, "created_at": "2026-10-08T16:02:00Z"},
        {"message_id": "m4", "type": "text", "created_at": "2026-10-08T16:03:00Z"},   # no text, normal: dropped
    ]}
    msgs = parse_messages(data, set())
    assert [(m.ext_id, m.kind, m.direction) for m in msgs] == [
        ("n1", "integration", "unknown"), ("n2", "system", "unknown"), ("m3", None, "in")]
    assert msgs[0].raw_links == [LABEL]
    assert msgs[1].text == "" and msgs[1].raw_links == ["https://www.willhaben.at/x/label"]
    assert msgs[2].raw_links == []


@pytest.mark.parametrize("text, amount", [
    ("Zahlung eingegangen: € 12,34", 12.34),
    ("Du hast 12,34 € erhalten", 12.34),
    ("EUR 1.234,50 bezahlt", 1234.5),
    ("Betrag EUR 7", 7.0),
    ("kein Betrag", None),
])
def test_parse_amount(text, amount):
    assert paylivery.parse_amount(text) == amount


def test_paid_wording():
    assert paylivery.is_paid("Zahlung eingegangen") and paylivery.is_paid("Der Käufer hat bezahlt.")
    assert paylivery.is_paid("PayLivery: Sendung wurde bezahlt")
    assert not paylivery.is_paid("Der Käufer hat nicht bezahlt")
    assert not paylivery.is_paid("PayLivery: sicher bezahlen")        # generic advice, not a payment
    assert not paylivery.is_paid("Ist das noch da?")


def test_label_urls_only_willhaben_https():
    assert paylivery.label_urls("Paketschein: https://www.willhaben.at/a/etikett.pdf", []) == [
        "https://www.willhaben.at/a/etikett.pdf"]
    assert paylivery.label_urls("", ["https://evil.example/label.pdf", "http://www.willhaben.at/label.pdf"]) == []
    assert paylivery.label_urls("", ["https://www.willhaben.at/iad/object?adId=1"]) == []


@pytest.fixture
def seller_chat():
    with session() as db:
        db.add(Conversation(id="c1", ad_id="77", role="seller", ad_title="Kinderbuch", partner_name="Max"))
        db.add(Shipment(conversation_id="c1", ad_id="77", status="awaiting",
                        recipient={"name": "Max Muster"}, parcel={"length_cm": 30, "width_cm": 22, "height_cm": 6,
                                                                  "weight_kg": 1.1}))
        db.commit()


def _notice(ext_id: str, text: str, kind: str = "integration", minutes_ago: int = 5, cid: str = "c1") -> None:
    with session() as db:
        db.add(Message(conversation_id=cid, ext_id=ext_id, direction="unknown", text=text, kind=kind,
                       created_at=utcnow() - timedelta(minutes=minutes_ago)))
        db.commit()


async def test_paid_notice_marks_shipment_and_runs_after_paid(seller_chat, monkeypatch):
    calls = []

    async def fake_after(shipment_id):
        calls.append(shipment_id)

    monkeypatch.setattr(shipping, "after_paid", fake_after)
    _notice("n1", "Zahlung eingegangen: € 12,34 über PayLivery")
    s = await paylivery.scan("c1")
    assert s.paid_via == "paylivery" and s.paid_amount == 12.34 and s.paid_at is not None
    assert calls == [s.id]
    assert await paylivery.scan("c1") is None                     # seen: nothing happens a second time
    assert calls == [s.id]
    assert shipping.get(s.id).paid_amount == 12.34
    assert json.loads(get_setting("paylivery_seen")) == ["c1:n1"]


async def test_paid_notice_creates_shipment_when_none_is_open(monkeypatch):
    with session() as db:
        db.add(Conversation(id="c2", role="seller"))
        db.commit()
    calls = []

    async def fake_detect(conversation_id, force=False):
        assert force
        with session() as db:
            s = Shipment(conversation_id=conversation_id, status="draft")
            db.add(s)
            db.commit()
            db.refresh(s)
            return s

    async def fake_after(shipment_id):
        calls.append(shipment_id)

    monkeypatch.setattr(shipping, "detect", fake_detect)
    monkeypatch.setattr(shipping, "after_paid", fake_after)
    _notice("n9", "Zahlung erhalten: 9,50 €", cid="c2")
    s = await paylivery.scan("c2")
    assert s.paid_via == "paylivery" and s.paid_amount == 9.5 and calls == [s.id]


async def test_old_notice_is_only_marked_seen(seller_chat, monkeypatch):
    calls = []

    async def fake_after(shipment_id):
        calls.append(shipment_id)

    monkeypatch.setattr(shipping, "after_paid", fake_after)
    _notice("old", "Zahlung eingegangen: € 5", minutes_ago=60 * 24 * 30)
    assert await paylivery.scan("c1") is None
    assert shipping.open_for("c1").paid_at is None and calls == []
    assert json.loads(get_setting("paylivery_seen")) == ["c1:old"]


class _Resp:
    def __init__(self, body: bytes, ok: bool = True, status: int = 200):
        self._body, self.ok, self.status = body, ok, status

    async def body(self):
        return self._body


class _Request:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.urls = resp, exc, []

    async def get(self, url, timeout=None):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return self.resp


def _fake_session(monkeypatch, request, started: bool = True) -> None:
    sess = SimpleNamespace(started=started, context=SimpleNamespace(request=request))
    monkeypatch.setattr(willhaben_session, "get_session", lambda: sess)


def _label_msg(url: str = LABEL) -> list[ChatMessage]:
    return [ChatMessage(ext_id="n2", text="Dein Paketschein steht bereit", created_at=None, direction="unknown",
                        kind="system", raw_links=[url])]


async def test_label_link_is_downloaded_and_shipment_labelled(seller_chat, monkeypatch):
    req = _Request(_Resp(b"%PDF-1.4 label"))
    _fake_session(monkeypatch, req)
    _notice("n2", "Dein Paketschein steht bereit", kind="system")
    s = await paylivery.scan("c1", fresh=_label_msg())
    assert req.urls == [LABEL]
    assert s.status == "labelled" and s.label_path == f"shipment-{s.id}-paylivery.pdf"
    assert (get_settings().data_dir / "labels" / s.label_path).read_bytes() == b"%PDF-1.4 label"


@pytest.mark.parametrize("auto", [True, False])
async def test_downloaded_label_is_printed_with_automatic_printing(seller_chat, monkeypatch, auto):
    import asyncio

    from app.services import printing
    printed = []

    async def fake_print(sid, which="auto"):
        printed.append(sid)
        return "1"

    monkeypatch.setattr(printing, "auto_choice", lambda s: "normal" if auto else None)
    monkeypatch.setattr(printing, "print_label", fake_print)
    _fake_session(monkeypatch, _Request(_Resp(b"%PDF-1.4 label")))
    _notice("n2", "Dein Paketschein steht bereit", kind="system")
    s = await paylivery.scan("c1", fresh=_label_msg())
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert printed == ([s.id] if auto else [])


@pytest.mark.parametrize("request_", [
    _Request(_Resp(b"<html>login</html>")),                 # answered, but no PDF
    _Request(_Resp(b"", ok=False, status=403)),              # refused
    _Request(exc=RuntimeError("network down")),              # request failed
])
async def test_label_not_saved_without_pdf(seller_chat, monkeypatch, request_):
    _fake_session(monkeypatch, request_)
    _notice("n2", "Dein Paketschein steht bereit", kind="system")
    s = await paylivery.scan("c1", fresh=_label_msg())
    assert s.status == "awaiting" and s.label_path is None


async def test_paid_then_label_in_one_scan_without_shipment(monkeypatch):
    with session() as db:
        db.add(Conversation(id="c3", role="seller"))
        db.commit()

    async def fake_detect(conversation_id, force=False):
        with session() as db:
            s = Shipment(conversation_id=conversation_id, status="draft")
            db.add(s)
            db.commit()
            db.refresh(s)
            return s

    calls = []

    async def fake_after(shipment_id):
        calls.append(shipment_id)

    monkeypatch.setattr(shipping, "detect", fake_detect)
    monkeypatch.setattr(shipping, "after_paid", fake_after)
    _fake_session(monkeypatch, _Request(_Resp(b"%PDF-1.4 label")))
    _notice("p1", "Zahlung eingegangen: € 4,20", minutes_ago=10, cid="c3")
    _notice("n2", "Dein Paketschein steht bereit", kind="system", minutes_ago=5, cid="c3")
    msgs = [ChatMessage(ext_id="n2", text="Dein Paketschein steht bereit", created_at=None, direction="unknown",
                        kind="system", raw_links=[LABEL])]
    s = await paylivery.scan("c3", fresh=msgs)
    assert s.paid_via == "paylivery" and s.paid_amount == 4.2
    assert s.status == "labelled" and s.label_path == f"shipment-{s.id}-paylivery.pdf"
    assert calls == [s.id]


async def test_label_is_retried_until_a_pdf_arrives(seller_chat, monkeypatch):
    req = _Request(_Resp(b"<html>not yet</html>"))
    _fake_session(monkeypatch, req)
    _notice("n2", "Dein Paketschein steht bereit", kind="system")
    s = await paylivery.scan("c1", fresh=_label_msg())
    assert s.status == "awaiting"
    assert "c1:n2" not in json.loads(get_setting("paylivery_seen") or "[]")   # not applied: stays open
    req.resp = _Resp(b"%PDF-1.4 label")
    s = await paylivery.scan("c1", fresh=_label_msg())
    assert s.status == "labelled" and s.label_path
    assert json.loads(get_setting("paylivery_seen")) == ["c1:n2"]
    assert len(req.urls) == 2


async def test_label_on_foreign_host_is_not_fetched(seller_chat, monkeypatch):
    req = _Request(_Resp(b"%PDF-1.4"))
    _fake_session(monkeypatch, req)
    _notice("n2", "Dein Paketschein steht bereit", kind="system")
    await paylivery.scan("c1", fresh=_label_msg("https://evil.example/label.pdf"))
    assert req.urls == []


async def test_sync_scans_seller_and_buyer_chats(monkeypatch):
    seen = []

    async def fake_scan(conversation_id, fresh=None):
        seen.append((conversation_id, [m.ext_id for m in fresh or []]))

    class Acct:
        async def messages(self, conversation_id):
            return [ChatMessage(ext_id="n1", text="Zahlung eingegangen", created_at=utcnow(), direction="unknown",
                                kind="integration")]

    monkeypatch.setattr(paylivery, "scan", fake_scan)
    monkeypatch.setattr(chats, "_account", lambda: Acct())
    with session() as db:
        db.add(Conversation(id="s1", role="seller"))
        db.add(Conversation(id="b1", role="buyer"))
        db.commit()
    assert await chats.sync_messages("s1") == "full"
    assert await chats.sync_messages("b1") == "full"
    assert seen == [("s1", ["n1"]), ("b1", ["n1"])]    # both roles: the state follows, shipments stay seller-only
    with session() as db:
        row = db.exec(select(Message).where(Message.conversation_id == "s1")).one()
    assert row.kind == "integration" and row.direction == "unknown"


async def test_sync_survives_failing_scan(monkeypatch):
    async def broken_scan(conversation_id, fresh=None):
        raise RuntimeError("boom")

    class Acct:
        async def messages(self, conversation_id):
            return []

    monkeypatch.setattr(paylivery, "scan", broken_scan)
    monkeypatch.setattr(chats, "_account", lambda: Acct())
    with session() as db:
        db.add(Conversation(id="s2", role="seller"))
        db.commit()
    assert await chats.sync_messages("s2") == "full"


def test_chat_page_renders_notice_line():
    from fastapi.testclient import TestClient

    from app.main import app
    with session() as db:
        db.add(Conversation(id="c1", role="seller", ad_title="Kinderbuch", partner_name="Max"))
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in", text="Ist das noch da?", created_at=utcnow()))
        db.add(Message(conversation_id="c1", ext_id="n1", direction="unknown", text="Zahlung eingegangen: € 12,34",
                       kind="integration", created_at=utcnow()))
        db.commit()
    with TestClient(app) as c:
        r = c.get("/chats/c1")
    assert r.status_code == 200
    assert 'data-notice' in r.text and "Zahlung eingegangen: € 12,34" in r.text   # willhaben notice, centered
