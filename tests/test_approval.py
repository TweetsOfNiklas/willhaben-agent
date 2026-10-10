"""Payment, approval and the label pipeline: after_paid, run_pipeline, auto rules, push buttons, chat page."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Message, MyAd, Shipment, TelegramAction, get_setting, session, set_setting, utcnow
from app.llm.base import LLMResult
from app.services import ha_actions, label_buy, printing, shipping, telegram
from tests.test_shipping import ADDR


@pytest.fixture(autouse=True)
def clean_background():
    yield
    for task in list(shipping._tasks):
        task.cancel()
    shipping._graces.clear()
    shipping._running.clear()


@pytest.fixture
def desk(monkeypatch):
    """Chat c1 about ad 77, with a Claude stand-in whose answer each test sets in `reply`."""
    class Backend:
        model = "fake"
        reply = {"found": True, "address": ADDR, "note": "", "agreed_price": 0, "payment_claimed": False,
                 "parcel": {"length_cm": 30, "width_cm": 22, "height_cm": 6, "weight_kg": 1.1, "contents": "Buch"}}

        async def complete(self, *, system, prompt, schema, images=None):
            return LLMResult(data=dict(self.reply), model="fake")

    backend = Backend()
    monkeypatch.setattr("app.llm.base.get_backend", lambda *a: backend)

    async def no_ctx(cid):
        with session() as db:
            return db.get(Conversation, cid)

    monkeypatch.setattr("app.services.chats.ensure_ad_context", no_ctx)
    with session() as db:
        db.add(MyAd(ad_id="77", heading="Kinderbuch"))
        db.add(Conversation(id="c1", ad_id="77", ad_title="Kinderbuch", partner_name="Max", role="seller",
                            last_message_text="Max Muster, Hauptstraße 12/3/4, 1150 Wien"))
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in",
                       text="Max Muster, Hauptstraße 12/3/4, 1150 Wien", created_at=utcnow()))
        db.commit()
    return backend


@pytest.fixture
def pushes(monkeypatch):
    calls = []

    async def fake_notify(kind, title, message, **kw):
        calls.append({"kind": kind, "title": title, "message": message, **kw})
        return True

    async def fake_clear(tag):
        return None

    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    monkeypatch.setattr("app.services.notify.clear_push", fake_clear)
    return calls


@pytest.fixture
def buy(monkeypatch):
    """Stand-ins for the label purchase and the printer; `state` picks the outcome."""
    calls = {"buy": [], "print": []}
    state = {"buy": "ok", "print": "ok", "auto": True, "hook": False}

    async def fake_buy(sid):
        calls["buy"].append(sid)
        if state["buy"] == "needs":
            raise label_buy.NeedsHuman("confirm the payment in your bank app")
        if state["buy"] == "error":
            raise label_buy.BuyError("carrier down")
        shipping.update(sid, status="labelled", label_path=f"/data/labels/{sid}.pdf", paid_price=3.9)
        if state["hook"]:                    # the download listener saw the label while the purchase runs
            shipping.label_arrived(sid)

    async def fake_print(sid):
        calls["print"].append(sid)
        if state["print"] == "error":
            raise printing.PrintError("no printer")
        shipping.update(sid, status="printed", print_job="job-1", printed_at=utcnow())
        return "job-1"

    monkeypatch.setattr(label_buy, "buy", fake_buy)
    monkeypatch.setattr(printing, "auto_choice", lambda s: "label" if state["auto"] else None)
    monkeypatch.setattr(printing, "print_label", fake_print)
    return calls, state


async def draft(sold: bool = True, user_parcel: bool = True) -> int:
    """A shipment for chat c1: address, option GLS S -> Shop (3.90 €), sold to this buyer, parcel from the user."""
    shipping.save_sender({**ADDR, "name": "Max Seller"})
    s = await shipping.detect("c1")
    if user_parcel:
        s = shipping.set_parcel(s.id, {"length_cm": 30, "width_cm": 22, "height_cm": 8, "weight_kg": 1.4})
    if sold:
        with session() as db:
            ad = db.get(MyAd, "77")
            ad.sale_status = "sold"
            ad.sale_conversation_id = "c1"
            db.add(ad)
            db.commit()
    return s.id


def auto_on(grace: int = 0) -> None:
    set_setting("ship_auto", "1")
    set_setting("ship_auto_grace_min", str(grace))


def states(sid: int) -> dict:
    s = shipping.get(sid)
    return {"status": s.status, "approval": s.approval, "error": s.error, "label": s.label_path}


# ------------------------------------------------------------------ payment marks
async def test_mark_paid_is_idempotent(desk, pushes):
    sid = await draft()
    first = shipping.mark_paid(sid, "manual")
    second = shipping.mark_paid(sid, "email", 3.9)
    assert second.paid_at == first.paid_at and second.paid_via == "manual" and second.paid_amount is None
    with pytest.raises(shipping.ShippingError):
        shipping.mark_paid(sid, "cash")


async def test_claim_does_not_pay(desk, pushes):
    sid = await draft()
    s = shipping.claim_payment(sid)
    assert s.payment_claimed_at and s.paid_at is None and s.status == "draft"


async def test_claim_from_chat_asks_the_user_and_does_not_pay(desk, pushes):
    sid = await draft()
    desk.reply = {**desk.reply, "found": False, "address": None, "payment_claimed": True}
    with session() as db:
        conv = db.get(Conversation, "c1")
        conv.last_message_text = "Habe schon überwiesen"
        db.add(conv)
        db.commit()
    await shipping.on_incoming(conv)
    for task in list(shipping._tasks):
        await task
    push = pushes[-1]
    assert push["title"] == "Buyer reports payment – check your account"
    assert [a["title"] for a in push["actions"]] == ["Paid, buy label"]
    s = shipping.get(sid)
    assert s.payment_claimed_at and s.paid_at is None and s.status == "draft"


async def test_agreed_price_is_kept_from_the_chat(desk, pushes):
    sid = await draft()
    desk.reply = {**desk.reply, "agreed_price": 4.5}
    await shipping.detect("c1")
    assert shipping.get(sid).agreed_price == 4.5
    desk.reply = {**desk.reply, "agreed_price": 0}
    await shipping.detect("c1")
    assert shipping.get(sid).agreed_price == 4.5                     # 0 = unknown: keep what we had


async def test_payment_remark_without_shipment_does_nothing(desk, pushes):
    desk.reply = {**desk.reply, "found": False, "address": None, "payment_claimed": True}
    with session() as db:
        conv = db.get(Conversation, "c1")
    assert await shipping.detect("c1") is None
    await shipping.on_incoming(conv)
    await asyncio.gather(*list(shipping._tasks))
    assert not pushes


# ------------------------------------------------------------------ automatic rules
async def test_blockers_name_every_missing_rule(desk, pushes):
    sid = await draft(sold=False, user_parcel=False)
    reasons = " | ".join(shipping.auto_blockers(shipping.get(sid)))
    for part in ("automatic buying is off", "not paid yet", "only an estimate", "not marked as sold"):
        assert part in reasons
    auto_on()
    shipping.mark_paid(sid, "manual")
    shipping.set_parcel(sid, {"length_cm": 30, "width_cm": 22, "height_cm": 8, "weight_kg": 1.4}, remember=False)
    with session() as db:
        ad = db.get(MyAd, "77")
        ad.sale_status, ad.sale_conversation_id = "sold", "c1"
        db.add(ad)
        db.commit()
    assert shipping.auto_blockers(shipping.get(sid)) == []


async def test_blockers_price_carrier_and_daily_cap(desk, pushes):
    sid = await draft()
    auto_on()
    shipping.mark_paid(sid, "manual")
    s = shipping.get(sid)
    assert shipping.auto_blockers(s) == []
    set_setting("ship_auto_max_price", "3")
    assert any("above the limit" in r for r in shipping.auto_blockers(s))
    set_setting("ship_auto_max_price", "10")
    set_setting("ship_auto_carriers", "-")
    assert any("not allowed" in r for r in shipping.auto_blockers(s))
    set_setting("ship_auto_carriers", "")
    set_setting("ship_auto_daily", "1")
    with session() as db:
        db.add(Shipment(conversation_id="c1", ad_id="77", recipient=ADDR, approval="auto", approved_at=utcnow()))
        db.commit()
    assert any("daily limit" in r for r in shipping.auto_blockers(s))


async def test_auto_rules_buy_without_asking(desk, pushes, buy):
    calls, _ = buy
    sid = await draft()
    auto_on(grace=0)
    shipping.mark_paid(sid, "manual")
    await shipping.after_paid(sid)
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid] and calls["print"] == [sid]
    assert states(sid)["approval"] == "auto" and shipping.get(sid).status == "printed"


async def test_no_rules_met_asks_first(desk, pushes, buy):
    calls, _ = buy
    sid = await draft()
    shipping.mark_paid(sid, "manual")
    await shipping.after_paid(sid)
    assert states(sid) == {"status": "awaiting", "approval": "pending", "error": None, "label": None}
    push = pushes[-1]
    assert push["title"] == "Paid – buy the label?"
    assert [a["title"] for a in push["actions"]] == ["Buy & print", "Decline"]
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == []


async def test_paylivery_is_never_bought(desk, pushes, buy):
    calls, _ = buy
    sid = await draft()
    auto_on()
    shipping.mark_paid(sid, "paylivery")
    await shipping.after_paid(sid)
    await shipping.run_pipeline(sid)
    assert calls["buy"] == [] and pushes[-1]["title"] == "Paid via willhaben PayLivery"


async def test_unpaid_is_never_bought(desk, pushes, buy):
    calls, _ = buy
    sid = await draft()
    auto_on()
    await shipping.run_pipeline(sid)
    with pytest.raises(shipping.ShippingError):
        shipping.approve(sid)
    assert calls["buy"] == []


async def test_after_paid_is_idempotent(desk, pushes, buy):
    sid = await draft()
    shipping.mark_paid(sid, "manual")
    await shipping.after_paid(sid)
    await shipping.after_paid(sid)
    assert len([p for p in pushes if p["title"] == "Paid – buy the label?"]) == 1


# ------------------------------------------------------------------ pipeline
async def paid_and_approved() -> int:
    sid = await draft()
    shipping.mark_paid(sid, "manual")
    shipping.update(sid, approval="approved", approved_at=utcnow(), status="awaiting")
    return sid


async def test_pipeline_buys_then_prints(desk, pushes, buy):
    calls, _ = buy
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    assert calls["buy"] == [sid] and calls["print"] == [sid]
    assert shipping.get(sid).status == "printed" and pushes[-1]["title"] == "Label printed"


async def test_pipeline_without_automatic_printing_only_buys(desk, pushes, buy):
    calls, state = buy
    state["auto"] = False                    # switch off, "No printing" for the carrier or no printer address
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    assert calls["buy"] == [sid] and calls["print"] == []
    assert shipping.get(sid).status == "labelled" and pushes[-1]["title"] == "Label bought"


async def test_label_seen_during_the_purchase_is_printed_once(desk, pushes, buy):
    calls, state = buy
    state["hook"] = True
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid] and calls["print"] == [sid] and shipping.get(sid).status == "printed"


async def test_needs_human_keeps_buying_and_links_the_browser(desk, pushes, buy):
    calls, state = buy
    state["buy"] = "needs"
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    s = shipping.get(sid)
    assert s.status == "buying" and "bank app" in s.error
    push = pushes[-1]
    assert push["title"] == "Finish the payment in the browser window" and push["url"].endswith("/session")
    assert calls["print"] == []


async def test_buy_error_marks_failed(desk, pushes, buy):
    calls, state = buy
    state["buy"] = "error"
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    assert states(sid)["status"] == "failed" and states(sid)["error"] == "carrier down"
    assert pushes[-1]["title"] == "Label purchase failed"


async def test_retry_after_failed_print_only_prints(desk, pushes, buy):
    calls, state = buy
    state["print"] = "error"
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    assert shipping.get(sid).status == "failed" and shipping.get(sid).label_path
    state["print"] = "ok"
    shipping.retry(sid)
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid]                       # no second label
    assert calls["print"] == [sid, sid] and shipping.get(sid).status == "printed"


async def test_retry_with_printing_switched_off_leaves_failed(desk, pushes, buy):
    calls, state = buy
    state["print"] = "error"
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    assert shipping.get(sid).status == "failed"
    state["auto"] = False
    shipping.retry(sid)
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid] and calls["print"] == [sid]
    assert states(sid)["status"] == "labelled" and states(sid)["error"] is None


async def test_retry_after_failed_buy_buys_again(desk, pushes, buy):
    calls, state = buy
    state["buy"] = "error"
    sid = await paid_and_approved()
    await shipping.run_pipeline(sid)
    state["buy"] = "ok"
    shipping.retry(sid)
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid, sid] and shipping.get(sid).status == "printed"


async def test_pipeline_runs_once_per_shipment(desk, pushes, buy):
    calls, _ = buy
    sid = await paid_and_approved()
    await asyncio.gather(shipping.run_pipeline(sid), shipping.run_pipeline(sid))
    assert calls["buy"] == [sid]


# ------------------------------------------------------------------ grace countdown
async def test_grace_countdown_can_be_cancelled(desk, pushes, buy, monkeypatch):
    calls, _ = buy
    never = asyncio.Event()

    async def hold(seconds):
        await never.wait()

    monkeypatch.setattr(shipping, "_wait", hold)
    sid = await draft()
    auto_on(grace=1)
    shipping.mark_paid(sid, "manual")
    await shipping.after_paid(sid)
    push = pushes[-1]
    assert push["title"] == "Label will be bought in 1 min"
    assert [a["title"] for a in push["actions"]] == ["Cancel"]
    assert shipping.page_state(shipping.get(sid))["ship_grace_left"] == 1
    assert await ha_actions.handle({"action": ha_actions.action_id("CANCEL", sid)}) == "cancel"
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == []
    assert states(sid)["approval"] == "pending" and shipping.get(sid).status == "awaiting"
    assert pushes[-1]["title"] == "Paid – buy the label?"


async def test_grace_countdown_buys_when_it_runs_out(desk, pushes, buy, monkeypatch):
    calls, _ = buy

    async def expired(seconds):
        return None

    monkeypatch.setattr(shipping, "_wait", expired)
    sid = await draft()
    auto_on(grace=1)
    shipping.mark_paid(sid, "manual")
    await shipping.after_paid(sid)
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid] and shipping.get(sid).status == "printed"


# ------------------------------------------------------------------ push buttons
async def test_push_buttons_follow_the_rules(desk, pushes, buy):
    calls, state = buy
    state["auto"] = False

    def act(verb, sid):
        return {"action": ha_actions.action_id(verb, sid)}

    sid = await draft()
    assert ha_actions.parse_action(act("WAIT", sid)["action"]) is None         # no such button any more
    assert await ha_actions.handle(act("PAID", sid)) == "paid"
    assert shipping.get(sid).paid_at and states(sid)["approval"] == "pending"
    assert await ha_actions.handle(act("PAID", sid)) is None                   # second tap
    assert await ha_actions.handle(act("DECL", sid)) == "decl"
    assert states(sid)["approval"] == "declined" and shipping.get(sid).status == "draft"
    assert await ha_actions.handle(act("DECL", sid)) is None
    assert ha_actions.parse_action(act("BUYNP", sid)["action"]) is None        # "buy only" is gone: one print switch
    assert ha_actions.approval_actions(sid)[0]["title"] == "Buy label"         # automatic printing off
    assert await ha_actions.handle(act("BUY", sid)) == "buy"                   # changed my mind
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid] and calls["print"] == [] and shipping.get(sid).status == "labelled"
    assert await ha_actions.handle(act("BUY", sid)) is None
    forged = {"action": ha_actions.action_id("BUY", sid)[:-12] + "0" * 12}
    assert await ha_actions.handle(forged) is None


async def test_push_buy_and_print(desk, pushes, buy):
    calls, _ = buy
    sid = await draft()
    shipping.mark_paid(sid, "manual")
    await shipping.after_paid(sid)
    assert await ha_actions.handle({"action": ha_actions.action_id("BUY", sid)}) == "buy"
    await asyncio.gather(*list(shipping._tasks), return_exceptions=True)
    assert calls["buy"] == [sid] and calls["print"] == [sid] and shipping.get(sid).status == "printed"


def test_telegram_buttons_map_to_the_shipment_verbs(desk):
    kb = telegram.keyboard("shipping", {"shipment_id": 5}, ha_actions.approval_actions(5), None)
    rows = [b for row in kb["inline_keyboard"] for b in row]
    with session() as db:
        found = [db.get(TelegramAction, b["callback_data"][2:]) for b in rows]
    assert [f.verb for f in found] == ["D_BUY", "D_DECL"] and {f.ref for f in found} == {"5"}


# ------------------------------------------------------------------ chat page and routes
async def test_chat_page_shows_each_state(desk, pushes, buy):
    from app.main import app
    sid = await draft()
    with TestClient(app) as c:
        shipping.mark_paid(sid, "manual")
        await shipping.after_paid(sid)                                         # pending: the ask box
        page = c.get("/chats/c1").text
        assert "Paid – buy the label?" in page and "Buy &amp; print" in page and "Decline" in page
        shipping.update(sid, status="buying", error="confirm in your bank app", approval="approved")
        page = c.get("/chats/c1").text
        assert "Buying the label on the carrier" in page and "Finish in browser" in page and "Retry" in page
        shipping.update(sid, status="failed", error="carrier down")
        page = c.get("/chats/c1").text
        assert "carrier down" in page and "Retry" in page
        shipping.update(sid, status="printed", label_path="/data/labels/x.pdf", approval="approved")
        page = c.get("/chats/c1").text
        assert f"/shipments/{sid}/label.pdf" in page and "Finish in browser" not in page


async def test_chat_page_restart_recovery_asks_again(desk, pushes, buy):
    from app.main import app
    sid = await draft()
    shipping.mark_paid(sid, "manual")
    shipping.update(sid, status="awaiting", approval="auto", approved_at=utcnow())   # no countdown: a restart
    with TestClient(app) as c:
        page = c.get("/chats/c1").text
    assert "Buy &amp; print" in page and "Cancel" not in page


async def test_payment_routes(desk, pushes, buy, monkeypatch):
    from app.main import app
    started = []
    monkeypatch.setattr(shipping, "start_pipeline", lambda sid: started.append(sid))
    sid = await draft()
    with TestClient(app) as c:
        r = c.post(f"/shipments/{sid}/paid", follow_redirects=False)
        assert r.status_code == 303 and "#shipping" in r.headers["location"]
        assert shipping.get(sid).paid_at and states(sid)["approval"] == "pending"
        r = c.post(f"/shipments/{sid}/approve", follow_redirects=False)
        assert r.status_code == 303 and started == [sid]
        assert states(sid)["approval"] == "approved"
        r = c.post(f"/shipments/{sid}/decline", follow_redirects=False)        # already decided: nothing to decline
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        r = c.post(f"/shipments/{sid}/unpaid", follow_redirects=False)
        assert r.status_code == 303 and shipping.get(sid).paid_at is None and shipping.get(sid).approval is None
        r = c.post(f"/shipments/{sid}/cancel-auto", follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]


def test_settings_route_stores_the_rules():
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/settings/shipping/auto", data={"on": "1", "max_price": "7,5", "grace_min": "15",
                                                    "daily": "2", "need_sold": "1",
                                                    "auto_carrier": ["gls", "post"]}, follow_redirects=False)
        assert r.status_code == 303
        assert get_setting("ship_auto") == "1" and get_setting("ship_auto_max_price") == "7.5"
        assert get_setting("ship_auto_grace_min") == "15" and get_setting("ship_auto_daily") == "2"
        assert get_setting("ship_auto_need_sold") == "1" and get_setting("ship_auto_need_parcel") == "0"
        assert get_setting("ship_auto_carriers") == "gls,post"
        assert "Automatic label purchase" in c.get("/settings/shipping").text
        c.post("/settings/shipping/auto", data={"max_price": "x"}, follow_redirects=False)
        assert get_setting("ship_auto") == "0" and get_setting("ship_auto_carriers") == "-"
