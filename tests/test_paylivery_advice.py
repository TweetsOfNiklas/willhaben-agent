"""The PayLivery step in the answer draft: with an open offer the draft call asks for it too, the code holds it to the
ad's minimum and asking price, the composer shows it and "Send + PayLivery" starts it with the answer."""
from fastapi.testclient import TestClient

from app.db import Conversation, Draft, MyAd, session
from app.llm.base import LLMResult
from app.services import chats, paylivery, paylivery_advice as pa

SELLER = {"role": "seller", "offer": 30.0}


def test_clean_holds_the_step_to_the_numbers():
    assert pa.clean({"action": "counter", "amount": 20}, SELLER, 80, 50)["amount"] == 50     # not below the minimum
    assert pa.clean({"action": "counter", "amount": 95}, SELLER, 80, None)["amount"] == 80   # not above the price
    assert pa.clean({"action": "counter", "amount": None}, SELLER, 80, None)["amount"] == 55  # midpoint
    assert pa.clean({"action": "accept", "amount": None}, SELLER, 80, 50)["action"] == "counter"
    assert pa.clean({"action": "counter", "amount": 61.3}, SELLER, 80, None)["amount"] == 61.5
    assert pa.clean({"action": "none", "amount": None}, SELLER, 80, None) is None
    assert pa.clean({"action": "counter", "amount": 50}, {"role": "buyer", "offer": 45.0}, None, None) is None


def _seller_chat():
    with session() as db:
        db.add(MyAd(ad_id="77", heading="Router", min_price=60))
        db.add(Conversation(id="s-adv", ad_id="77", role="seller", partner_name="x",
                            ad_context={"price": "€ 120", "paylivery": True, "title": "Router"},
                            paylivery={"role": "seller", "stage": "offer_received", "offer": 30.0, "history": []}))
        db.commit()


async def test_the_draft_asks_for_the_step_and_keeps_it(monkeypatch):
    _seller_chat()
    seen = {}

    class Backend:
        async def complete(self, **kw):
            seen.update(kw)
            return LLMResult(data={"reply": "30 € ist zu wenig, ich schicke dir ein Gegenangebot über 95 €.",
                                   "intent": "price_negotiation", "needs_info": [], "suggested_price": 95, "note": "",
                                   "meetup": None, "alternatives": [],
                                   "paylivery_action": {"action": "counter", "amount": 95}}, model="fake")
    monkeypatch.setattr(chats, "get_backend", lambda purpose=None: Backend())

    async def ctx(cid):
        with session() as db:
            return db.get(Conversation, cid)
    monkeypatch.setattr(chats, "ensure_ad_context", ctx)
    d = await chats.draft_reply("s-adv")
    assert "paylivery_action" in seen["schema"]["required"] and "Offene PayLivery-Anfrage" in seen["prompt"]
    assert "Mindestpreis 60,00 €" in seen["prompt"]
    assert d.paylivery == {"action": "counter", "amount": 95.0, "offer": 30.0}
    assert any("PayLivery step" in w for w in chats.answer_blockers(None, d, [], ad=None, buy=None, buyer=False))


def test_the_composer_shows_the_step_and_send_starts_it(monkeypatch):
    _seller_chat()
    with session() as db:
        d = Draft(conversation_id="s-adv", text="Gegenangebot kommt.",
                  paylivery={"action": "counter", "amount": 95.0, "offer": 30.0})
        db.add(d)
        db.commit()
        db.refresh(d)
    started, queued = [], []

    async def start(cid, action, amount=""):
        started.append((cid, action, amount))
        return "ok"
    monkeypatch.setattr(paylivery, "start_action", start)
    monkeypatch.setattr(chats, "enqueue_send", lambda did, text, now=False: queued.append(did) or True)
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats/s-adv").text
        assert "data-paylivery-step" in page and 'name="pl_amount"' in page and 'value="95.0"' in page
        assert "Send + PayLivery" in page
        c.post(f"/drafts/{d.id}/send", data={"text": "Gegenangebot kommt.", "paylivery_step": "counter", "pl_amount": "90"})
        assert started == [("s-adv", "counter", "90")] and queued == [d.id]
        c.post(f"/drafts/{d.id}/send", data={"text": "x"})                     # unticked: only the answer
        assert len(started) == 1 and len(queued) == 2
        assert "Only PayLivery: Counter offer" in page
        c.post("/chats/s-adv/paylivery/counter", data={"text": "x", "paylivery_step": "counter", "pl_amount": "88"})
        assert started[-1] == ("s-adv", "counter", "88") and len(queued) == 2  # the step alone, no message
        paylivery._update_state("s-adv", lambda s: {**s, "stage": "counter_sent"})
        assert "data-paylivery-step" not in c.get("/chats/s-adv").text          # answered: the step is gone


async def test_an_offer_notice_writes_a_draft(monkeypatch):
    _seller_chat()
    monkeypatch.setattr(pa, "AUTO", True)
    asked = []
    monkeypatch.setattr(chats, "enqueue_draft", lambda cid, hint=None: asked.append(cid) or True)
    with session() as db:
        conv = db.get(Conversation, "s-adv")
    await pa.on_stage(conv, "offer_received", conv.paylivery)
    assert asked == ["s-adv"]


def test_a_counter_offer_without_amount_keeps_the_previous_one():
    assert pa.clean({"action": "counter", "amount": None}, SELLER, 80, None, before=50)["amount"] == 50
    assert "behalte diesen Betrag" in pa.prompt_block(SELLER, 80, None, 50)
    _seller_chat()
    with session() as db:
        db.add(Draft(conversation_id="s-adv", text="x", paylivery={"action": "counter", "amount": 50.0, "offer": 30.0}))
        db.commit()
    assert pa.previous_amount("s-adv", 30.0) == 50.0 and pa.previous_amount("s-adv", 40.0) is None


def test_chat_steps_start_in_the_conversation_itself():
    conv = Conversation(id="f3dee5b8-9973-4ccc-8e01-013ae0bcefd9", ad_context={"url": "https://www.willhaben.at/iad/x"})
    assert paylivery._start_url(conv, "counter") == paylivery.CHAT_URL + "/f3dee5b8-9973-4ccc-8e01-013ae0bcefd9"
    assert paylivery._start_url(Conversation(id="c1"), "accept") == paylivery.CHAT_URL


def test_a_running_step_shows_a_spinner_that_refreshes_itself():
    _seller_chat()
    with session() as db:
        db.add(Draft(conversation_id="s-adv", text="x", paylivery={"action": "counter", "amount": 50.0, "offer": 30.0}))
        db.commit()
    from app.main import app
    paylivery._busy["s-adv"] = "counter"
    try:
        with TestClient(app) as c:
            page = c.get("/chats/s-adv").text
        assert "data-paylivery-busy" in page and "Counter offer is running on willhaben…" in page
        assert 'hx-trigger="every 2s" hx-select="#pl-step"' in page and 'hx-select="#paylivery"' in page
        assert "data-paylivery-step" not in page                    # no second start while it runs
    finally:
        paylivery._busy.pop("s-adv", None)
    with TestClient(app) as c:
        page = c.get("/chats/s-adv").text
    assert "data-paylivery-busy" not in page and "data-paylivery-step" in page and 'aria-busy="true"' not in page
