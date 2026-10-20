"""Claude's advice on a buyer's PayLivery offer: shown on the card, the counter offer field starts with its amount, and
the code holds it to the ad's minimum and asking price."""
from fastapi.testclient import TestClient

from app.db import Conversation, MyAd, session
from app.llm.base import LLMResult
from app.services import paylivery, paylivery_advice as pa


def test_guard_holds_the_advice_to_the_numbers():
    assert pa.guard({"action": "counter", "amount": 20, "reason": "x"}, 30, 80, 50)["amount"] == 50     # not below min
    assert pa.guard({"action": "counter", "amount": 95}, 30, 80, None)["amount"] == 80                  # not above ask
    assert pa.guard({"action": "counter", "amount": None}, 30, 80, None)["amount"] == 55                # midpoint
    assert pa.guard({"action": "accept", "amount": None}, 30, 80, 50) == {"action": "counter", "amount": 55, "reason": ""}
    assert pa.guard({"action": "counter", "amount": 79.8}, 80, 80, None)["action"] == "accept"          # offer = price
    assert pa.guard({"action": "counter", "amount": 61.3}, 30, 80, None)["amount"] == 61.5


async def test_an_incoming_offer_gets_advice_on_the_card(monkeypatch):
    with session() as db:
        db.add(MyAd(ad_id="77", heading="Router", min_price=60))
        db.add(Conversation(id="s-adv", ad_id="77", role="seller", partner_name="x", ad_context={"price": "€ 120", "paylivery": True},
                            paylivery={"role": "seller", "stage": "offer_received", "offer": 30.0, "history": []}))
        db.commit()
    monkeypatch.setattr(pa, "AUTO", True)
    calls = []

    class Backend:
        async def complete(self, **kw):
            calls.append(kw["prompt"])
            return LLMResult(data={"action": "counter", "amount": 95, "reason": "Weit unter dem Preis."}, model="fake")
    monkeypatch.setattr("app.llm.base.get_backend", lambda purpose=None: Backend())
    with session() as db:
        conv = db.get(Conversation, "s-adv")
    await pa.on_stage(conv, "offer_received", conv.paylivery)
    await pa.on_stage(conv, "offer_received", conv.paylivery)                     # once per offer
    assert len(calls) == 1 and "Mindestpreis des Verkäufers: 60,00 €" in calls[0] and "30,00 €" in calls[0]
    with session() as db:
        card = paylivery.card(db.get(Conversation, "s-adv"))
    assert card["advice"]["action"] == "counter" and card["counter_amount"] == 95
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats/s-adv").text
        assert "Claude suggests:" in page and "Weit unter dem Preis." in page and 'value="95.0"' in page
        c.post("/chats/s-adv/paylivery-advice")
    assert len(calls) == 2                                                         # asked again on request
