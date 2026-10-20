"""A label whose pay click went through but whose PDF never arrived is not bought again by "Retry": the carrier may
have charged it. Only the owner's "Not charged – buy again" buys anew."""
from fastapi.testclient import TestClient

from app.db import Conversation, Shipment, session, utcnow
from app.services import label_buy, shipping


def _shipment(**kw):
    with session() as db:
        db.add(Conversation(id="c-lbl", partner_name="x", role="seller", last_message_text=""))
        s = Shipment(conversation_id="c-lbl", ad_id="1", status="failed", approval="approved", paid_at=utcnow(),
                     option="gls:S:home", price=4.5, error="download failed", **kw)
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id


def _count_buys(monkeypatch):
    calls = []

    async def buy(sid):
        calls.append(sid)
        shipping.update(sid, label_path="x.pdf")
    monkeypatch.setattr(label_buy, "buy", buy)
    return calls


async def test_retry_after_the_pay_click_does_not_buy_again(monkeypatch):
    calls = _count_buys(monkeypatch)
    sid = _shipment(paid_price=4.5)
    await shipping.run_pipeline(sid)
    s = shipping.get(sid)
    assert calls == [] and s.status == "failed" and "paid already" in s.error and s.paid_price == 4.5


async def test_without_a_pay_click_retry_buys(monkeypatch):
    calls = _count_buys(monkeypatch)
    sid = _shipment()
    await shipping.run_pipeline(sid)
    assert calls == [sid]


def test_the_card_offers_buy_again_only_after_a_pay_click(monkeypatch):
    calls = _count_buys(monkeypatch)
    sid = _shipment(paid_price=4.5)
    started = []
    monkeypatch.setattr(shipping, "start_pipeline", lambda i: started.append(i))
    from app.main import app

    with TestClient(app) as c:
        assert "Not charged – buy again" in c.get("/chats/c-lbl").text
        c.post(f"/shipments/{sid}/retry", data={})
        assert shipping.get(sid).paid_price == 4.5             # plain retry: the record stays
        c.post(f"/shipments/{sid}/retry", data={"rebuy": "1"})
        assert shipping.get(sid).paid_price is None and started == [sid, sid]
    assert calls == []
