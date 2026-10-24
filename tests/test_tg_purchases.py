"""The Telegram Mini App's purchase orders (app/tg_purchases.py): the list, one order with its buttons, the buy dialog
and Start – the dashboard's handlers, the Mini App's pages and messages. Fakes as in test_purchase_web."""
from app import db as dbmod
from app.db import Purchase, get_setting
from app.services import purchase as svc
from tests.test_dashboard import _conversation, client  # noqa: F401  (client fixture: dashboard with mocked willhaben)
from tests.test_purchase_web import _ad, _market_res, _order, _switch_on, _use


def test_the_list_by_status_and_one_order_with_its_chat(client):  # noqa: F811
    _conversation()
    pid = _order(conversation_id="c1")
    done = _order(status="done", ad_title="Alter Kauf")
    listing = client.get("/tg/purchases").text
    assert f'href="/tg/purchases/{pid}"' in listing and "Alter Kauf" not in listing
    assert "Alter Kauf" in client.get("/tg/purchases?show=done").text
    assert f'href="/tg/purchases/{done}"' in client.get("/tg/purchases?show=all").text
    page = client.get(f"/tg/purchases/{pid}").text
    assert 'href="/tg/chats/c1"' in page and "Gegenangebot" in page
    assert f'action="/tg/purchases/{pid}/cancel"' in page and 'action="/purchases/' not in page   # stays in the Mini App
    assert "data-purchase-card" in page and "data-agreed" in page                 # the whole card, as the dashboard's
    assert client.get("/tg/purchases/999999").status_code == 404


def test_an_order_button_runs_the_dashboards_handler_and_comes_back_with_its_notice(client, monkeypatch):  # noqa: F811
    _conversation()
    pid = _order(conversation_id="c1", status="negotiating")
    seen = []

    async def fake_pause(p, note=None, *, push=True):
        seen.append(p)

    monkeypatch.setattr(svc, "pause", fake_pause)
    r = client.post(f"/tg/purchases/{pid}/pause", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/tg/purchases/{pid}?msg=") and seen == [pid]
    r = client.post(f"/tg/purchases/{pid}/paid", follow_redirects=False)             # not offered now: a refusal
    assert "msg=" in r.headers["location"]
    assert "This action is not possible now." in client.get(r.headers["location"]).text
    r = client.post(f"/tg/purchases/{pid}/max_price", data={"value": "199"}, follow_redirects=False)
    with dbmod.session() as db:
        assert db.get(Purchase, pid).max_price == 199.0


def test_the_buy_dialog_and_start(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(), _market_res(180.0, 200.0, 220.0))
    page = client.get("/tg/purchases/new/2000000001?search=3&q=geschirr").text
    assert 'id="tg-buy"' in page and 'name="mode" value="paylivery"' in page and 'name="wish_price"' in page
    assert 'name="max_default" value="1"' in page and "data-fair" in page and "data-gauge" in page
    assert 'data-main-form="tg-buy"' in page
    # live as on the dashboard: the pill, the ring to drag, the summary, the maths (tg/_buy_script.html)
    assert 'class="bd-drag"' in page and 'class="bd-pin"' in page and "bd-summary" in page and "bd-live" in page
    assert "window.wbBuy" in page and "wbBuy.submit(this, event)" in page and 'data-ship=' in page
    assert 'id="tg-buy"' in client.get("/tg/purchases/new/willhaben:2000000001").text     # a ref works too
    assert "willhaben ads only" in client.get("/tg/purchases/new/vinted:55").text
    calls = {}

    def fake_start(ad_id, mode, max_price=None, **kw):
        calls.update(ad_id=ad_id, mode=mode, max_price=max_price, **kw)
        return 42

    monkeypatch.setattr(svc, "start", fake_start)
    r = client.post("/tg/purchases/new/2000000001", data={"mode": "pickup", "max_price": "230", "max_default": "",
                                                          "wish_price": "", "questions": "OVP dabei?"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/tg/purchases/42?msg=")
    assert calls["mode"] == "pickup" and calls["max_price"] == 230.0 and calls["questions"] == "OVP dabei?"
    r = client.post("/tg/purchases/new/2000000001", data={"mode": "pickup", "max_price": "-3"}, follow_redirects=False)
    assert r.headers["location"].startswith("/tg/purchases/new/2000000001?msg=")            # a refusal: back to the dialog


def test_the_dialog_while_purchases_are_off(client, monkeypatch):  # noqa: F811
    assert get_setting(svc.SETTING_ENABLED) != "1"
    _use(monkeypatch, _ad())
    page = client.get("/tg/purchases/new/2000000001").text
    assert "switched off" in page and 'id="tg-buy"' not in page
