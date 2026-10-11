"""Kaufauftrag web pages (app/purchase_routes.py): the buy dialog (opened from a product search result and from the ad
viewer), the order start, the order list and page, the order buttons, the chat card and the Settings section.

Fakes only: the dashboard client from test_dashboard mocks willhaben; the ad detail and the price check of the dialog
are faked per test (FakePublic, deals.analyze); purchase.start and the actions are monkeypatched where they would start
background work.
"""
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import db as dbmod
from app.db import Purchase, ProductSearch, get_setting, set_setting
from app.services import deals
from app.services import purchase as svc
from app.willhaben import public as public_mod
from app.willhaben.parse import AdDetail
from app.willhaben.public import WillhabenError
from tests.test_dashboard import _conversation, client  # noqa: F401  (client fixture: dashboard with mocked willhaben)

RESULT = {"ad_id": "2000000001", "title": "Geschirrspüler Bosch", "price": 250.0, "price_display": "€ 250",
          "location": "Wien", "url": "https://www.willhaben.at/iad/object?adId=2000000001",
          "image_url": "https://cache.willhaben.at/x.jpg", "is_private": True, "published_at": None, "text": "",
          "query": "geschirrspüler", "position": {"pos": None, "reason": "no_price"}, "median": None,
          "comparables": 0, "scam_flags": [], "red_flags": [], "fit": 80, "reason": "passt zur Beschreibung"}
# market data of the comparable ads (product search rows carry min, p25 and median)
MARKET = {**RESULT, "min": 180.0, "p25": 200.0, "median": 220.0, "comparables": 9}
MARKET_ABOVE = {**RESULT, "min": 300.0, "p25": 280.0, "median": 290.0, "comparables": 9}


def _search(result: dict = RESULT) -> int:
    with dbmod.session() as db:
        row = ProductSearch(description="Geschirrspüler", status="done", plan={"summary": "Geschirrspüler, 60 cm"},
                            settings={"depth": "normal", "unusual": False, "categories": False, "sort": "relevance"},
                            results=[result], requests=1, candidates=1, progress={"done": 1, "total": 1})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _order(**kw) -> int:
    base = dict(ad_id="2000000001", ad_title="Bosch Geschirrspüler", ad_url=RESULT["url"], asking_price=250.0,
                max_price=220.0, mode="paylivery", status="negotiating",
                log=[{"at": "2026-10-09T08:00:00", "status": "negotiating", "text": "Gegenangebot", "from": "contacting"}])
    base.update(kw)
    with dbmod.session() as db:
        row = Purchase(**base)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _switch_on() -> None:
    set_setting(svc.SETTING_ENABLED, "1")


# ------------------------------------------------------------------ buy buttons
DIALOG = "/purchases/new/2000000001?search={sid}&q=geschirr"
FRAGMENT = {"X-Requested-With": "fetch"}      # what the dialog's script sends


def test_search_result_has_the_buy_link(client):  # noqa: F811
    sid = _search()
    page = client.get(f"/deals?search={sid}").text
    assert "data-buy=" not in page and 'action="/purchases"' not in page
    assert "Turn on purchases in Settings" in page                 # switched off: disabled look and the settings link

    _switch_on()
    page = client.get(f"/deals?search={sid}").text
    assert f'data-buy="2000000001" href="/purchases/new/2000000001?search={sid}&amp;q=geschirrsp%C3%BCler"' in page
    assert 'action="/purchases"' not in page                       # no inline menu any more
    assert 'id="buy-dialog"' in page                               # the dialog is part of every page


def test_viewer_has_the_buy_button(client):  # noqa: F811
    page = client.get("/hits/ad/1662146981").text
    assert "data-buy=" not in page and "Turn on purchases in Settings" in page

    _switch_on()
    page = client.get("/hits/ad/1662146981").text
    assert 'data-buy="1662146981" href="/purchases/new/1662146981"' in page
    assert 'action="/purchases"' not in page


# ------------------------------------------------------------------ start
def test_start_while_switched_off_redirects_with_a_notice(client):  # noqa: F811
    r = client.post("/purchases", data={"ad_id": "2000000001", "mode": "paylivery", "back": "/deals?search=3#ps-result",
                                        "title": "Bosch", "url": RESULT["url"], "price": "250"}, follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert loc.path == "/deals" and loc.fragment == "ps-result"
    assert parse_qs(loc.query)["search"] == ["3"] and "pn" in parse_qs(loc.query)
    assert "Purchase orders are switched off" in client.get(r.headers["location"]).text


def test_unsafe_back_link_falls_back_to_the_order_list(client):  # noqa: F811
    r = client.post("/purchases", data={"ad_id": "2000000001", "mode": "paylivery", "back": "//evil.example/x"},
                    follow_redirects=False)
    assert urlsplit(r.headers["location"]).path == "/purchases"


def test_notices_show_only_our_own_tokens(client):  # noqa: F811
    assert "unknown-text-from-a-link" not in client.get("/deals?pn=unknown-text-from-a-link").text


def test_start_with_the_switch_on_goes_to_the_order_page(client, monkeypatch):  # noqa: F811
    _switch_on()
    calls = {}

    def fake_start(ad_id, mode, max_price=None, **kw):
        calls.update(ad_id=ad_id, mode=mode, max_price=max_price, **kw)
        return 7

    monkeypatch.setattr(svc, "start", fake_start)
    r = client.post("/purchases", data={"ad_id": "2000000001", "mode": "pickup", "max_price": "220,50",
                                        "pickup_window": "Sa vormittag", "search_id": "3", "query": "geschirr",
                                        "title": "Bosch", "url": RESULT["url"],
                                        "image_url": "https://cache.willhaben.at/x.jpg", "price": "250",
                                        "back": "/deals"}, follow_redirects=False)
    assert r.headers["location"] == "/purchases/7"
    assert calls["ad_id"] == "2000000001" and calls["mode"] == "pickup" and calls["max_price"] == 220.5
    assert calls["pickup_window"] == "Sa vormittag" and calls["search_id"] == 3 and calls["price"] == 250.0
    assert calls["url"] == RESULT["url"] and calls["image_url"] == "https://cache.willhaben.at/x.jpg"


def test_start_refuses_a_bad_maximum_price_with_a_notice(client, monkeypatch):  # noqa: F811
    _switch_on()
    monkeypatch.setattr(svc, "start", lambda *a, **k: 1 / 0)      # must not be reached
    r = client.post("/purchases", data={"ad_id": "2000000001", "mode": "direct", "max_price": "viel",
                                        "back": "/deals"}, follow_redirects=False)
    assert "Enter the maximum price in euros." in client.get(r.headers["location"]).text


# ------------------------------------------------------------------ order list and page
def test_order_list_and_order_page(client):  # noqa: F811
    _conversation()
    pid = _order(conversation_id="c1")
    listing = client.get("/purchases").text
    assert "Bosch Geschirrspüler" in listing and f"/purchases/{pid}" in listing
    page = client.get(f"/purchases/{pid}").text
    assert "Purchase order" in page and "Gegenangebot" in page and "Open chat" in page
    assert client.get("/purchases/999999").status_code == 404


# ------------------------------------------------------------------ chat card
def test_chat_page_shows_the_pay_now_card_for_a_bank_transfer(client):  # noqa: F811
    _conversation()
    pid = _order(conversation_id="c1", mode="direct", status="pay_now", agreed_price=200.0, max_price=None,
                 payment={"iban": "AT611904300234573201", "holder": "Anna Muster", "amount": 200.0,
                          "reference": "Bosch willhaben"})
    page = client.get("/chats/c1").text
    assert "Pay now!" in page and "AT61 1904 3002 3457 3201" in page and "Anna Muster" in page
    assert "Bank transfer" in page and f'action="/purchases/{pid}/paid"' in page


def test_chat_page_without_an_order_shows_no_card(client):  # noqa: F811
    _conversation()
    assert "Purchase order" not in client.get("/chats/c1").text


def test_paylivery_pay_now_card_points_at_the_buy_button(client):  # noqa: F811
    with dbmod.session() as db:
        db.add(dbmod.Conversation(id="c2", ad_id="2000000001", ad_title="Bosch", role="buyer", partner_name="Max",
                                  ad_context={"paylivery": True, "buy_now": True, "price": "250 €"}))
        db.commit()
    _order(conversation_id="c2", mode="paylivery", status="pay_now", agreed_price=250.0)
    page = client.get("/chats/c2").text
    assert "Pay now!" in page and "3-D Secure" in page
    assert "https://www.willhaben.at/iad/myprofile/chat/c2" in page
    assert 'id="paylivery"' in page and 'action="/chats/c2/paylivery/buy"' in page   # the PayLivery card's buy button


def test_pickup_order_page_shows_the_appointment(client):  # noqa: F811
    pid = _order(mode="pickup", status="meetup", conversation_id=None, agreed_price=180.0,
                 pickup={"start": "2026-10-17T10:00", "minutes": 30, "location": "Stiege 2"})
    page = client.get(f"/purchases/{pid}").text
    assert "Stiege 2" in page and "2026-10-17 10:00" in page and "180 cash" in page
    assert f'action="/purchases/{pid}/collected"' in page


# ------------------------------------------------------------------ order buttons
def test_order_buttons_call_the_mapped_functions(client, monkeypatch):  # noqa: F811
    _conversation()
    pid = _order(conversation_id="c1", status="negotiating")
    seen = []

    async def fake_pause(p, note=None, *, push=True):
        seen.append((p, note, push))

    monkeypatch.setattr(svc, "pause", fake_pause)
    r = client.post(f"/purchases/{pid}/pause", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/chats/c1?pn=")
    assert r.headers["location"].endswith("#purchase")
    assert seen == [(pid, "Paused by you", False)]


def test_max_price_button_validates_and_sets(client):  # noqa: F811
    _conversation()
    pid = _order(conversation_id="c1", status="negotiating")
    bad = client.post(f"/purchases/{pid}/max_price", data={"value": "abc"}, follow_redirects=False)
    assert "Enter the maximum price in euros." in client.get(bad.headers["location"]).text
    assert svc.get(pid).max_price == 220.0
    client.post(f"/purchases/{pid}/max_price", data={"value": "180"}, follow_redirects=False)
    assert svc.get(pid).max_price == 180.0


def test_paid_button_runs_only_when_the_status_offers_it(client, monkeypatch):  # noqa: F811
    _conversation()
    pid = _order(conversation_id="c1", status="negotiating", mode="direct")
    called = []
    monkeypatch.setattr(svc, "mark_paid", lambda p: called.append(p))
    r = client.post(f"/purchases/{pid}/paid", follow_redirects=False)
    assert called == [] and "This action is not possible now." in client.get(r.headers["location"]).text

    with dbmod.session() as db:
        row = db.get(Purchase, pid)
        row.status, row.mode = "pay_now", "direct"
        db.add(row)
        db.commit()
    client.post(f"/purchases/{pid}/paid", follow_redirects=False)
    assert called == [pid]


def test_pickup_collected_button_and_unknown_actions(client, monkeypatch):  # noqa: F811
    pid = _order(mode="pickup", status="meetup", conversation_id=None,
                 pickup={"start": "2026-10-17T10:00", "minutes": 30, "location": "Stiege 2"})
    called = []
    monkeypatch.setattr(svc, "mark_collected", lambda p: called.append(p))
    r = client.post(f"/purchases/{pid}/collected", follow_redirects=False)
    assert called == [pid] and r.headers["location"].startswith(f"/purchases/{pid}?pn=")
    assert client.post(f"/purchases/{pid}/delete", follow_redirects=False).status_code == 404


# ------------------------------------------------------------------ settings
def test_settings_section_saves_the_three_keys(client):  # noqa: F811
    page = client.get("/settings/purchase").text
    assert 'id="purchase"' in page and 'action="/settings/purchase"' in page

    r = client.post("/settings/purchase", data={"enabled": "1", "rules": "  nur Privatverkäufer\nRechnung fragen ",
                                                "discount_pct": "15"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/purchase?")
    assert get_setting("purchase_enabled") == "1"
    assert get_setting("purchase_rules") == "nur Privatverkäufer\nRechnung fragen"
    assert get_setting("purchase_discount_pct") == "15"

    client.post("/settings/purchase", data={"rules": "x", "discount_pct": "80"})   # switch unchecked, clamped
    assert get_setting("purchase_enabled") == "0" and get_setting("purchase_discount_pct") == "50"


def test_prefer_paylivery_needs_orders_on(client):  # noqa: F811
    client.post("/settings/purchase", data={"prefer_paylivery": "1", "discount_pct": "10"})   # orders off: ignored
    assert get_setting("purchase_prefer_paylivery") == "0"
    page = client.get("/settings/purchase").text
    assert 'name="prefer_paylivery"' in page and "disabled" in page.split('name="prefer_paylivery"')[1].split(">")[0]

    client.post("/settings/purchase", data={"enabled": "1", "prefer_paylivery": "1", "discount_pct": "10"})
    assert get_setting("purchase_prefer_paylivery") == "1"
    client.post("/settings/purchase", data={"enabled": "1", "discount_pct": "10"})             # preference off
    assert get_setting("purchase_prefer_paylivery") == "0"


def test_settings_rejects_a_discount_that_is_not_a_number(client):  # noqa: F811
    set_setting("purchase_discount_pct", "12")
    r = client.post("/settings/purchase", data={"enabled": "1", "discount_pct": "viel"}, follow_redirects=False)
    assert "Enter a discount between 0 and 50 %." in client.get(r.headers["location"]).text
    assert get_setting("purchase_discount_pct") == "12"


# ------------------------------------------------------------------ buy dialog
class FakePublic:
    """Stands in for willhaben: one ad detail (None: the ad is gone; an exception: willhaben fails); counts requests."""

    def __init__(self, detail=None, error=None):
        self.detail, self.error, self.asked = detail, error, []

    async def get_detail(self, ad_id):
        self.asked.append(ad_id)
        if self.error:
            raise self.error
        return self.detail

    async def aclose(self):                    # the app shuts the willhaben client down
        pass


def _ad(price=250.0, paylivery=True, shipping=7.9, **kw) -> AdDetail:
    fields = dict(ad_id="2000000001", heading="Bosch Geschirrspüler", description="Kaum benutzt, mit Garantie.",
                  price=price, price_display=None, status="active", published_at=None, first_published_at=None,
                  end_date=None, url=RESULT["url"], images=["https://cache.willhaben.at/x.jpg"], location="1010 Wien",
                  seller_name="Anna Muster", is_private=True, category_path=[], paylivery_enabled=paylivery,
                  buy_now=False, shipping_price=shipping)
    fields.update(kw)
    return AdDetail(**fields)


def _market_res(low: float, p25: float, median: float, count: int = 9, price: float = 250.0) -> dict:
    """What deals.analyze returns (only the parts the dialog reads): the position comes from deals.position itself."""
    stats = {"count": count, "min": low, "p10": (low + p25) / 2, "p25": p25, "median": median, "p75": median + 30,
             "p90": median + 60, "max": median + 100}
    return {"ad": {"price": price}, "comparables": {"count": count, "stats": stats},
            "position": deals.position(price, stats, None)}


def _use(monkeypatch, detail=None, market=None, error=None) -> FakePublic:
    """Fake ad detail and fake price check; returns the fake client (with the list of ad ids and queries asked)."""
    fake = FakePublic(detail, error)
    fake.queries = []

    async def analyze(ad, *, query=None, **kw):
        fake.queries.append(query)
        return market

    public_mod.set_public(fake)
    monkeypatch.setattr(deals, "analyze", analyze)
    return fake


def _preselected(page: str) -> list[str]:
    return re.findall(r'value="(\w+)"[^>]*\schecked', page)


def test_dialog_is_a_fragment_for_the_script_and_a_page_without_it(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad())
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert 'class="bd-form"' in frag and "<html" not in frag.lower()
    assert "<html" not in client.get(DIALOG.format(sid=3), headers={"HX-Request": "true"}).text.lower()
    page = client.get(DIALOG.format(sid=3)).text
    assert "<!doctype html>" in page.lower()
    assert 'class="bd-form"' in page and "willhaben Agent" in page


def test_dialog_prefill_is_the_asking_price_plus_the_shipping_of_the_mode(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad())                                      # asking € 250, PayLivery shipping € 7,90 by the ad
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert 'name="max_price" min="1" step="0.01" value="250.00"' in frag   # nothing preselected: the asking price
    assert _preselected(frag) == []
    assert "data-ship='{" in frag and '"paylivery": 7.9' in frag and '"direct": 6.0' in frag and '"pickup": 0.0' in frag
    assert 'data-ship-text="Shipping € 7,90 · Buyer protection from 4,5 %"' in frag and 'value="256.00"' not in frag and 'value="269.15"' not in frag
    assert 'name="ad_id" value="2000000001"' in frag and 'name="search_id" value="3"' in frag
    assert 'name="query" value="geschirr"' in frag and 'name="max_default" value="1"' in frag


def test_preference_preselects_paylivery_and_the_maximum_with_its_shipping(client, monkeypatch):  # noqa: F811
    _switch_on()
    set_setting(svc.SETTING_PREFER_PAYLIVERY, "1")
    _use(monkeypatch, _ad(shipping=7.9))                          # asking € 250 + the ad's PayLivery shipping
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert _preselected(frag) == ["paylivery"]
    assert 'name="max_price" min="1" step="0.01" value="269.15"' in frag      # 250 + 7.90 + buyer protection 11.25

    _use(monkeypatch, _ad(shipping=None))                         # no shipping named: the estimate (€ 6) counts
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert 'name="max_price" min="1" step="0.01" value="267.25"' in frag      # 250 + 6 + 11.25

    _use(monkeypatch, _ad(paylivery=False, shipping=None, price=100.0))   # no PayLivery at the ad: not preselected
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert _preselected(frag) == [] and 'name="max_price" min="1" step="0.01" value="100.00"' in frag


def test_paylivery_card_is_disabled_without_paylivery_at_the_ad(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(paylivery=False, shipping=None, price=100.0))
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert 'value="paylivery" required disabled' in frag
    assert "Not available" in frag and "opacity-55" in frag
    assert 'value="direct" required' in frag and 'value="pickup" required' in frag


def _step2(frag: str) -> str:
    return re.search(r'<section class="bd-step" data-step="2"[^>]*>(.*?)</section>', frag, re.S).group(1)


def test_step2_has_one_fairness_hint_from_the_product_search_bar(client, monkeypatch):  # noqa: F811
    _switch_on()
    fake = _use(monkeypatch, _ad(), _market_res(180.0, 200.0, 220.0))      # asking € 250 is above the median € 220
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    step2 = _step2(frag)
    pos = deals.position(250.0, _market_res(180.0, 200.0, 220.0)["comparables"]["stats"], None)
    # the bar of the product search: gradient track, needle at the position of the asking price, the level badge
    assert 'bd-drag ' in step2 and 'class="deal-track"' in step2
    assert 'class="deal-needle ' in step2 and f'style="left:{round(pos["pos"] * 100, 1)}%"' in step2
    assert f'bg-lvl-{pos["level"]}">' in step2 and deals.level_label(pos["level"]) in step2
    # one short line, worded as on the product search card; the numbers of the old pills and the market line are gone
    assert "14 % above the median · 9 comparable ads" in step2
    for gone in ["Lower quartile € 200", "−10 %", "median € 220", "bd-ceil", "bd-pct", "bd-market"]:
        assert gone not in frag, gone
    assert fake.queries == [None]                                # the same price check as the viewer (title query)
    # the summary says it once more in the same words, with the badge
    summary = re.search(r'<ul class="bd-summary[^"]*".*?</ul>', frag, re.S).group(0)
    assert "14 % above the median · 9 comparable ads" in summary and deals.level_label(pos["level"]) in summary
    assert '>Rating</span>' in summary and "Market" not in summary
    assert summary.count('data-tpl="{n} % · {level}"') == 1 and step2.count('data-tpl="{n} % · {level}"') == 1


def test_step2_scale_for_the_browser_is_the_one_position_reads(client, monkeypatch):  # noqa: F811
    _switch_on()
    res = _market_res(180.0, 200.0, 220.0)
    _use(monkeypatch, _ad(), res)
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    import html as htmllib
    import json
    js = json.loads(htmllib.unescape(re.search(r"data-fair='([^']*)'", frag).group(1)))
    stats = res["comparables"]["stats"]
    assert js["scale"] == [[v, q] for v, q in deals.anchors(stats)] and js["scale"][3] == [220.0, 0.5]
    assert js["pos"] == res["position"]["pos"] and js["levels"][0] == [0.12, "bargain"] and js["levels"][-1][1] == "very_expensive"
    assert js["labels"]["cheap"] == "cheap" and set(js["labels"]) == {n for _, n in deals.LEVELS}
    assert re.search(r'class="bd-pin [^"]*" hidden', frag) and 'data-tpl="{n} % · {level}"' in frag and 'bd-drag ' in frag


def test_step2_has_three_chips_with_the_item_price_and_the_label_of_the_mode(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(), _market_res(180.0, 200.0, 220.0))
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    chips = re.findall(r'<button type="button" class="k-(\w+) [^"]*" data-v="([\d.]+)"[^>]*data-tpl="([^"]*)"[^>]*>([^<]*)</button>', step2)
    # the cheapest comparable first, then lower quartile and median (item prices; the script adds the shipping of the mode); no −10 %
    assert chips == [("min", "180.00", "Cheapest · {x}", "Cheapest · € 180"), ("cheap", "200.00", "Cheap · {x}", "Cheap · € 200"),
                     ("market", "220.00", "Market price · {x}", "Market price · € 220")]
    assert 'onclick="wbBuy.pick(this.form, +this.dataset.v)"' in step2


def test_chip_labels_start_with_the_shipping_of_the_preselected_mode(client, monkeypatch):  # noqa: F811
    _switch_on()
    set_setting(svc.SETTING_PREFER_PAYLIVERY, "1")
    _use(monkeypatch, _ad(shipping=7.9), _market_res(180.0, 200.0, 220.0))
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    # item price of the comparable + the ad's shipping 7.90 + its buyer protection fee (4.5 %: 8.10, 9, 9.90)
    assert "Cheapest · € 196" in step2 and "Cheap · € 216,90" in step2 and "Market price · € 237,80" in step2 and 'data-v="200.00"' in step2


def test_chips_only_below_the_asking_price_and_not_twice(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(), _market_res(249.0, 249.5, 260.0))            # min and p25 under 1 % below the ask, the median above it
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    assert "bd-sugg" not in step2 and "bd-gauge" in step2                  # the bar stays, no chips
    _use(monkeypatch, _ad(), _market_res(180.0, 249.0, 260.0))            # only the cheapest is worth a chip
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    assert step2.count('data-v="') == 1 and 'class="k-min ' in step2
    _use(monkeypatch, _ad(), _market_res(180.0, 200.0, 200.4))            # median rounds to the same price as the quartile
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    assert step2.count('data-v="') == 2 and 'class="k-cheap ' in step2 and 'class="k-market ' not in step2
    _use(monkeypatch, _ad(), _market_res(200.0, 200.2, 220.0))            # quartile rounds to the cheapest: one chip less
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    assert re.findall(r'class="k-(\w+) ', step2) == ["min", "market"]
    _use(monkeypatch, _ad(), _market_res(300.0, 280.0, 290.0, price=250.0))   # everything above the asking price
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert 'name="max_price" min="1" step="0.01" value="250.00"' in frag    # still prefilled
    assert "bd-sugg" not in frag and "Cheap ·" not in frag and "Cheapest ·" not in frag and "bd-gauge" in frag


def test_step2_bar_ends_name_the_cheapest_and_the_dearest_comparable(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(), _market_res(180.0, 200.0, 220.0))             # min 180, max 320
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    assert '<span>€ 180</span><span>€ 320</span>' in step2 and 'class="bd-ends' in step2
    assert step2.index('class="bd-gauge ') < step2.index('class="bd-ends') < step2.index('class="bd-sugg')
    # too few comparable ads: no scale, no ends, no drag
    res = _market_res(180.0, 200.0, 220.0, count=3)
    res["position"] = {"pos": 0.4, "level": "fair", "basis": "geizhals"}
    _use(monkeypatch, _ad(), res)
    step2 = _step2(client.get(DIALOG.format(sid=3), headers=FRAGMENT).text)
    assert "bd-ends" not in step2 and "bd-drag" not in step2 and 'class="bd-gauge ' in step2
    from app import purchase_routes
    assert purchase_routes._fair(_ad(), _market_res(180.0, 200.0, 220.0))["min"] == 180.0
    assert purchase_routes._fair(_ad(), res)["min"] is None and purchase_routes._fair(_ad(), res)["max"] is None


def test_no_hint_without_comparables_just_the_field(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(), None)
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    step2, summary = _step2(frag), re.search(r'<ul class="bd-summary[^"]*".*?</ul>', frag, re.S).group(0)
    for gone in ["bd-gauge", "deal-track", "deal-needle", "bd-sugg", "bd-live", "bd-pin", "deal-lvl"]:
        assert gone not in step2 and gone not in summary, gone
    assert "data-fair" not in frag and 'name="max_price"' in step2 and "Rating" not in summary
    # too few comparable ads: the position comes from the new price, no scale for the browser, no chips, no pill
    res = _market_res(180.0, 200.0, 220.0, count=3)
    res["position"] = {"pos": 0.4, "level": "fair", "basis": "geizhals"}
    _use(monkeypatch, _ad(), res)
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert "bd-gauge" in frag and "data-fair" not in frag and "bd-sugg" not in frag and "bd-live" not in frag and "bd-pin" not in frag
    assert "14 % above the median · 3 comparable ads" in frag


def test_maximum_field_holds_the_default_number_and_has_no_placeholder(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(), _market_res(180.0, 200.0, 220.0))
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    step2 = _step2(frag)
    field = re.search(r'<input type="number" name="max_price"[^>]*>', step2).group(0)
    assert 'value="250.00"' in field and 'inputmode="decimal"' in field and 'oninput="wbBuy.input(this.form)"' in field
    assert 'onblur="wbBuy.blur(this.form)"' in field                      # a cleared field is refilled
    assert "placeholder" not in field and "data-ph" not in frag and "Default:" not in frag and "Standard" not in frag
    assert 'data-noprice="no price in the ad"' in frag and 'name="max_default" value="1"' in frag
    assert "optional" not in step2
    # the rating pill sits in the same row as the field
    assert re.search(r'<span class="bd-field[^"]*"[^>]*>.*?<span class="bd-eur"[^>]*>€</span>\s*<input[^>]*>.*?<span class="bd-pill bd-live', step2, re.S)


def test_ad_without_a_price_makes_the_maximum_optional(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(price=None, shipping=None), _market_res(180.0, 200.0, 220.0))
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    step2 = _step2(frag)
    assert 'name="max_price" min="1" step="0.01" value=""' in step2 and "(optional)" in step2 and "placeholder" not in step2
    assert "bd-sugg" not in step2 and "bd-gauge" not in step2 and "bd-live" not in frag and "data-fair" not in frag
    assert 'data-ask="0"' in frag and 'name="max_default" value="1"' in frag


BUY_PANEL = (Path(__file__).resolve().parent.parent / "app" / "templates" / "_buy_panel.html").read_text(encoding="utf-8")


def test_mode_cards_span_the_column_and_the_number_field_has_no_spinner(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad())
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert frag.count('<label class="bd-mode') == 3            # the three ways to buy, one card each
    assert 'inputmode="decimal"' in frag
    # the bar: a draggable target (touch-action none), the ring is shown at the default too
    assert "touch-none" in (Path(__file__).resolve().parent.parent / "app" / "templates" / "_buy_dialog.html").read_text(encoding="utf-8")


def test_dialog_has_three_steps_in_order_and_the_chips(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(buy_now=True))
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    # no hidden attribute in the markup: all three panels show without JavaScript (the script hides two of them)
    assert re.findall(r'<section class="bd-step" data-step="(\d)"[^>]*>', frag) == ["1", "2", "3"]
    assert re.findall(r'class="bd-chip[^"]*" data-n="(\d)"', frag) == ["1", "2", "3"]
    assert re.findall(r'<span class="bd-n[^"]*">\d</span>([^<]+)</button>', frag) == ["How to buy", "Maximum", "Start"]
    assert frag.count('class="btn btn-primary bd-next ') == 2 and frag.count('class="btn bd-back ') == 2
    assert 'onkeydown="wbBuy.enter(event)"' in frag and 'data-empty="–"' in frag
    assert frag.count(">Change</button>") == 3 and 'data-s="timerow"' in frag
    for key in ["mode", "ship", "time", "max"]:
        assert f'data-s="{key}"' in frag
    assert 'data-s="max"' in frag and ">Ad<" not in frag             # the ad is on the left, not a summary row again
    assert 'class="btn btn-primary ml-auto bd-start"' in frag and 'type="submit"' in frag and "Start purchase" in frag
    assert "Nothing is paid automatically." in frag
    assert 'href="/settings/purchase"' in frag and "Settings → Buying rules" in frag
    assert 'class="bd-time ' in frag and "Preferred time" in frag and "e.g. Saturday morning" in frag
    assert "Description" in frag and ">Viewer</a>" in frag and "willhaben ↗" in frag
    assert "Buy now" in frag and 'data-ad="2000000001"' in frag
    assert "Shipping about" not in frag or "data-ship-text" in frag


def test_dialog_has_one_pill_in_step2_and_one_in_the_summary(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(), _market_res(180.0, 200.0, 220.0))
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert frag.count('data-tpl="{n} % · {level}" hidden') == 2


def test_dialog_with_purchases_off_shows_one_notice_and_fetches_nothing(client, monkeypatch):  # noqa: F811
    fake = _use(monkeypatch, _ad())
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert "Purchase orders are switched off" in frag and "Turn on purchases in Settings" in frag
    assert 'class="bd-form"' not in frag and fake.asked == []
    assert "Purchase orders are switched off" in client.get(DIALOG.format(sid=3)).text


def test_dialog_for_a_gone_ad_is_a_404_with_the_notice(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, None)
    r = client.get(DIALOG.format(sid=3), headers=FRAGMENT)
    assert r.status_code == 404 and "This ad is no longer online" in r.text


def test_dialog_shows_a_willhaben_error(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, error=WillhabenError("HTTP 503"))
    r = client.get(DIALOG.format(sid=3), headers=FRAGMENT)
    assert r.status_code == 200 and "willhaben error: HTTP 503" in r.text


def test_dialog_price_check_failure_leaves_the_dialog_working(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad())

    async def broken(ad, **kw):
        raise RuntimeError("search down")

    monkeypatch.setattr(deals, "analyze", broken)
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    assert 'class="bd-form"' in frag and 'name="max_price" min="1" step="0.01" value="250.00"' in frag


def test_dialog_carries_the_fee_rule_as_json(client, monkeypatch):  # noqa: F811
    import html as htmllib
    import json
    _switch_on()
    _use(monkeypatch, _ad())
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    form = re.search(r'<form method="post" action="/purchases"[^>]*>', frag).group(0)
    assert json.loads(htmllib.unescape(re.search(r"data-fee='([^']*)'", form).group(1))) == {"pct": 4.5, "min": 0.99, "max": 49.99}
    assert 'data-fee-tpl="Buyer protection {amount}"' in frag


def test_paylivery_card_names_shipping_and_the_fee_rule(client, monkeypatch):  # noqa: F811
    _switch_on()
    _use(monkeypatch, _ad(shipping=7.9))
    frag = client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    card = re.search(r'<label class="bd-mode[^>]*>.*?value="paylivery".*?</label>', frag, re.S)
    text = card.group(0)
    assert "Shipping € 7,90 · Buyer protection from 4,5 %" in text
    assert 'data-ship-text="Shipping € 7,90 · Buyer protection from 4,5 %"' in text
    # only the PayLivery card names the fee; bank transfer and pickup have none
    other = re.sub(r'<label class="bd-mode[^>]*>.*?value="paylivery".*?</label>', "", frag, flags=re.S)
    assert "Buyer protection from" not in other
    for mode in ("direct", "pickup"):
        assert "protection" not in re.search(rf'value="{mode}".*?</label>', frag, re.S).group(0)
    # estimate without a price in the ad
    _use(monkeypatch, _ad(shipping=None))
    assert "Shipping about € 6 · Buyer protection from 4,5 %" in client.get(DIALOG.format(sid=3), headers=FRAGMENT).text
    # no PayLivery at the ad: nothing about the fee on the card
    _use(monkeypatch, _ad(paylivery=False, shipping=None, price=100.0))
    card = re.search(r'<label class="bd-mode[^>]*>.*?value="paylivery".*?</label>', client.get(DIALOG.format(sid=3), headers=FRAGMENT).text, re.S).group(0)
    assert "Not available" in card and "protection" not in card


def _market_order(**kw) -> int:
    return _order(conversation_id="c1", **kw)


@pytest.mark.skipif(not hasattr(Purchase, "shipping_cost"), reason="backend shipping columns not there yet")
def test_chat_card_shows_item_shipping_fee_total_and_the_market(client):  # noqa: F811
    _conversation()
    _market_order(mode="paylivery", status="negotiating", agreed_price=200.0, shipping_cost=4.9,
                  shipping_source="ad", agreed_total=213.9,
                  market={"median": 220.0, "min": 180.0, "p25": 200.0, "count": 9})
    page = client.get("/chats/c1").text
    assert "€ 200 + € 4,90 + € 9 = " in page and "€ 213,90" in page
    assert "−12 %" in page and "incl. shipping &amp; fee" in page
    assert "cheapest € 180" in page and "Comparable ads (9)" in page and "median € 220" in page


@pytest.mark.skipif(not hasattr(Purchase, "shipping_cost"), reason="backend shipping columns not there yet")
def test_bank_transfer_card_has_no_buyer_protection_line(client):  # noqa: F811
    _conversation()
    _market_order(mode="direct", status="negotiating", agreed_price=200.0, shipping_cost=4.9, shipping_source="chat",
                  agreed_total=204.9)
    page = client.get("/chats/c1").text
    assert "€ 200 + € 4,90 = " in page and "€ 204,90" in page and "+ buyer protection" not in page


@pytest.mark.skipif(not hasattr(Purchase, "shipping_cost"), reason="backend shipping columns not there yet")
def test_order_page_shows_the_fee_line_for_paylivery(client):  # noqa: F811
    _conversation()
    pid = _market_order(mode="paylivery", status="pay_now", asking_price=250.0, agreed_price=None, shipping_cost=7.9,
                        shipping_source="ad")
    page = client.get(f"/purchases/{pid}").text
    assert "€ 250 + € 7,90 + € 11,25 = " in page and "€ 269,15" in page   # no agreement yet: the asking price


@pytest.mark.skipif(not hasattr(Purchase, "shipping_cost"), reason="backend shipping columns not there yet")
def test_bank_transfer_pay_now_card_shows_the_total_incl_shipping(client):  # noqa: F811
    _conversation()
    _market_order(mode="direct", status="pay_now", agreed_price=200.0, max_price=None, shipping_cost=4.9,
                  shipping_source="ad", agreed_total=204.9,
                  payment={"iban": "AT611904300234573201", "holder": "Anna Muster", "amount": 204.9,
                           "reference": "Bosch willhaben"})
    page = client.get("/chats/c1").text
    assert "Pay now!" in page and "Total incl. shipping" in page and "€ 204,90" in page


@pytest.mark.skipif(not hasattr(Purchase, "shipping_cost"), reason="backend shipping columns not there yet")
def test_pickup_card_has_no_shipping_line(client):  # noqa: F811
    _conversation()
    _market_order(mode="pickup", status="negotiating", agreed_price=180.0, shipping_cost=0.0, shipping_source="none")
    page = client.get("/chats/c1").text
    assert "Purchase order" in page and "+ shipping €" not in page


# ------------------------------------------------------------------ step logic (window.wbBuy) in node
JS_HARNESS = r"""
function cls(names) { const set = new Set(names || []);
  return { add: c => set.add(c), remove: c => set.delete(c), contains: c => set.has(c),
           toggle: (c, on) => { const v = on === undefined ? !set.has(c) : !!on; v ? set.add(c) : set.delete(c); return v; } }; }
function node(props) { return Object.assign({ dataset: {}, hidden: false, disabled: false, textContent: '', classList: cls(props && props.cls),
  style: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
  focus() { this.focused = true; } }, props || {}); }
// the scale of deals.anchors for min 100, p10 120, p25 150, median 200, p75 250, p90 300, max 400 (asking price 250 = 0.75)
const FAIR = { pos: 0.75, scale: [[100, 0], [120, 0.1], [150, 0.25], [200, 0.5], [250, 0.75], [300, 0.9], [400, 1]],
  levels: [[0.12, 'bargain'], [0.35, 'cheap'], [0.65, 'fair'], [0.88, 'expensive'], [2.0, 'very_expensive']],
  labels: { bargain: 'bargain', cheap: 'cheap', fair: 'fair', expensive: 'expensive', very_expensive: 'very expensive' } };
function form(o) {
  const radios = {};
  for (const v of ['paylivery', 'direct', 'pickup'])
    radios[v] = node({ value: v, checked: o.checked === v, disabled: (o.disabled || []).includes(v),
                       dataset: { label: v, shipText: (o.shipText || {})[v] || '' } });
  const steps = [1, 2, 3].map(n => node({ dataset: { step: String(n) } }));
  const chips = [1, 2, 3].map(n => node({ dataset: { n: String(n) } }));
  const nexts = [node(), node()];
  const lives = [node({ dataset: { tpl: '{n} % · {level}' } }), node({ dataset: { tpl: '{n} % · {level}' } })];
  const pins = [node({ hidden: true })];
  // the bar: a gauge that records its pointer listeners and sits at x 100..500 on the page
  const gauge = node({ listeners: {}, captured: [], addEventListener(t, fn) { this.listeners[t] = fn; },
                       setPointerCapture(id) { this.captured.push(id); }, getBoundingClientRect: () => ({ left: 100, width: 400 }) });
  const gauges = o.fair === false ? [] : [gauge];
  const sugg = (o.chips || []).map(v => node({ dataset: { v: String(v), tpl: 'Cheap · {x}' } }));
  const sm = { mode: node(), ship: node(), time: node(), max: node(), timerow: node({ hidden: true }) };
  const summary = node({ dataset: { empty: '–', noprice: 'no price in the ad', feeTpl: 'Buyer protection {amount}' },
                         querySelector: s => { const m = s.match(/^\[data-s=(\w+)\]$/); return m ? sm[m[1]] : null; } });
  const fair = o.fair === false ? undefined : JSON.stringify(FAIR);
  // real forms always carry the PayLivery fee rule (data-fee); o.fee === false leaves it out (no rule: no fee at all)
  const fee = o.fee === false ? undefined : JSON.stringify({ pct: 4.5, min: 0.99, max: 49.99 });
  const f = node({ dataset: { ask: String(o.ask), ship: JSON.stringify(o.ship), step: o.step, fair: o.fair === false ? undefined : fair, fee },
    elements: { max_price: { value: o.max === undefined ? '' : o.max },
                max_default: { value: o.maxDefault === undefined ? '1' : o.maxDefault },
                pickup_window: { value: o.time || '' }, back: { value: '' } },
    scrolled: false, scrollIntoView() { this.scrolled = true; },
    closest: sel => (o.dialog && sel === 'dialog' ? { querySelector: () => ({ scrollTop: 99 }) } : null) });
  const map = { 'input[name=mode]:checked': () => Object.values(radios).find(r => r.checked) || null,
                '.bd-chip.bd-on': () => chips.find(c => c.classList.contains('bd-on')) || null, '.bd-summary': () => summary };
  const lists = { '.bd-step': steps, '.bd-chip': chips, '.bd-next': nexts, '.bd-live': lives, '.bd-pin': pins, '.bd-sugg button': sugg, '.bd-drag': gauges };
  f.querySelector = s => (map[s] ? map[s]() : null);
  f.querySelectorAll = s => lists[s] || [];
  Object.assign(f, { radios, steps, chips, nexts, lives, pins, sugg, sm, gauge });
  return f;
}
const base = { ask: 250, ship: { paylivery: 7.9, direct: 6, pickup: 0 }, shipText: { paylivery: 'Shipping € 7.90', direct: 'Shipping about € 6', pickup: 'cash, no shipping' } };
const shown = f => f.steps.map(s => !s.hidden);
const pill = f => [f.lives[0].textContent, f.lives[0].hidden, ['fair', 'cheap', 'bargain', 'expensive', 'very_expensive'].some(t => f.lives[0].classList.contains('bg-lvl-' + t))];
const results = {};
// 1. opening: step 1 only, no mode yet, Next disabled
let f = form({ ...base, chips: [200, 150] }); wbBuy.init(f);
results.init = { js: f.classList.contains('bd-js'), shown: shown(f), chips: f.chips.map(c => [c.disabled, c.classList.contains('bd-on')]), next1: f.nexts[0].disabled };
// 2. choose PayLivery (untouched maximum follows the mode), Next enabled, Next goes to step 2
f.elements.max_price.value = '250.00';
f.radios.paylivery.checked = true; wbBuy.mode(f);
results.mode = { max: f.elements.max_price.value, next1: f.nexts[0].disabled, sumMode: f.sm.mode.textContent, sumShip: f.sm.ship.textContent,
                 maxDefault: f.elements.max_default.value, sumMax: f.sm.max.textContent };
wbBuy.next(f);
results.step2 = { step: String(f.dataset.step), shown: shown(f), chipDone: f.chips[0].classList.contains('bd-done'), chipOn: f.chips[1].classList.contains('bd-on'), next2: f.nexts[1].disabled };
// the default itself: the ad's own position (0.75, expensive), 0 %, the ring is shown (around the needle)
results.dflt = { pill: pill(f), pinHidden: f.pins[0].hidden, chips: f.sugg.map(c => c.textContent), on: f.sugg.map(c => c.classList.contains('bd-on')) };
// 3. a cleared field: still valid (no pill while empty, the summary names the default); leaving the field refills the number
f.elements.max_price.value = ''; wbBuy.input(f);
results.empty = { next2: f.nexts[1].disabled, maxDefault: f.elements.max_default.value, pill: pill(f), pinHidden: f.pins[0].hidden, sumMax: f.sm.max.textContent };
wbBuy.blur(f);
results.blur = { value: f.elements.max_price.value, maxDefault: f.elements.max_default.value, pill: pill(f), pinHidden: f.pins[0].hidden };
f.elements.max_price.value = ''; wbBuy.input(f); wbBuy.mode(f);       // the untouched default follows the mode, also from empty
results.emptyMode = f.elements.max_price.value;
f.elements.max_price.value = ''; wbBuy.input(f);
f.elements.max_price.checkValidity = () => false;             // a half-typed "12,5": bad input, not empty
results.badInput = wbBuy.valid(f, 2);
delete f.elements.max_price.checkValidity;
wbBuy.next(f);                                                // Next refills too
results.emptyGoes = { step: String(f.dataset.step), value: f.elements.max_price.value };
wbBuy.go(f, 2);
f.elements.max_price.value = '200'; wbBuy.input(f);           // changed: the flag is cleared; typing the default again sets it
results.changed = f.elements.max_default.value;
wbBuy.mode(f);                                                // a changed value is kept when the mode changes
results.changedKept = f.elements.max_price.value;
f.elements.max_price.value = '269.15'; wbBuy.input(f);
results.backToDefault = f.elements.max_default.value;
f.elements.max_price.value = ''; wbBuy.input(f); wbBuy.go(f, 1); wbBuy.go(f, 2);   // the step chips refill as well
results.chipRefill = f.elements.max_price.value;
f.elements.max_price.value = '0.5'; wbBuy.input(f);
results.below1 = { next2: f.nexts[1].disabled, maxDefault: f.elements.max_default.value, pill: pill(f) };
f.elements.max_price.value = '216.90'; wbBuy.input(f);
// 4. one pill: 216.90 = item 200 + 7.90 shipping + 9 buyer protection = the median (0.5, fair), 19 % below 269.15; the ring at 50 %
results.fairPill = { pill: pill(f), cls: f.lives[0].classList.contains('bg-lvl-fair'), summaryPill: f.lives[1].textContent, pin: [f.pins[0].hidden, f.pins[0].style.left],
                     sumMax: f.sm.max.textContent, chips: f.sugg.map(c => c.textContent), on: f.sugg.map(c => c.classList.contains('bd-on')) };
// 5. enter in the number field goes to step 3, never submits; a radio Enter is swallowed
let prevented = 0;
const ev = (key, target) => ({ key, target, preventDefault() { prevented++; } });
wbBuy.enter(ev('Enter', { form: f, type: 'number', tagName: 'INPUT' }));
results.enter = { step: String(f.dataset.step), prevented };
let linkPrev = 0;
wbBuy.enter({ key: 'Enter', target: { form: f, tagName: 'A', type: '' }, preventDefault() { linkPrev++; } });
results.linkEnter = linkPrev;
f.elements.max_price.value = '400'; wbBuy.input(f);          // item 375.22 (400 - 7.90 - 16.88): 0.9752 (very expensive), 49 % above
results.above = { pill: pill(f), cls: f.lives[0].classList.contains('bg-lvl-very_expensive'), pin: [f.pins[0].hidden, f.pins[0].style.left] };
f.elements.max_price.value = '110'; wbBuy.input(f);          // item 97.70 (110 - 7.90 - 4.40): below the cheapest comparable (bargain)
results.bargain = { pill: pill(f), cls: f.lives[0].classList.contains('bg-lvl-bargain'), pin: f.pins[0].style.left };
f.elements.max_price.checkValidity = () => false;             // e.g. 220.555: the browser refuses the cents
results.invalidStep2 = wbBuy.valid(f, 2);
delete f.elements.max_price.checkValidity;
f.elements.max_price.value = '216.90'; wbBuy.input(f);
// 6. the summary: back to step 1 with Change, the pickup time row appears and follows the typed time
wbBuy.go(f, 1);
results.back1 = { step: String(f.dataset.step), shown: shown(f), timerowHidden: f.sm.timerow.hidden, scrolled: f.scrolled, chipFocus: !!f.chips[0].focused };
f.radios.paylivery.checked = false; f.radios.pickup.checked = true; wbBuy.mode(f);
results.pickup = { max: f.elements.max_price.value, maxDefault: f.elements.max_default.value, timerowHidden: f.sm.timerow.hidden, time: f.sm.time.textContent, ship: f.sm.ship.textContent,
                   chips: f.sugg.map(c => c.textContent), pill: pill(f) };                 // the typed maximum is kept; no shipping now
f.elements.pickup_window.value = ' Samstag '; wbBuy.sum(f);
results.time = f.sm.time.textContent;
// 7. jump forward stops at the first step that is not valid (no mode yet); a dialog body scrolls to the top
let g = form({ ...base, dialog: true, max: '' }); wbBuy.init(g);
wbBuy.go(g, 3);
results.jump = { step: String(g.dataset.step) };
g.radios.direct.checked = true; wbBuy.mode(g); wbBuy.go(g, 3);   // with a mode an empty maximum no longer stops the jump
results.jumpEmpty = { step: String(g.dataset.step), sumMax: g.sm.max.textContent, value: g.elements.max_price.value };
// 8. chip click: the item price plus shipping, untouched flag cleared, chip lights up
let h = form({ ...base, checked: 'direct', max: '256', chips: [200] }); wbBuy.init(h);
wbBuy.pick(h, 200);
wbBuy.pick(h, 250);                                           // the default again (250 + 6): the flag goes back to untouched
results.pickDefault = h.elements.max_default.value;
wbBuy.pick(h, 200);
results.pick = { max: h.elements.max_price.value, maxDefault: h.elements.max_default.value, chip: h.sugg[0].textContent, on: h.sugg[0].classList.contains('bd-on'),
                 pill: pill(h) };
// 9. no comparable ads (no data-fair): no pill, no ring, the field alone
let k = form({ ...base, checked: 'direct', max: '300', fair: false }); wbBuy.init(k);
results.noScale = { pill: pill(k), pinHidden: k.pins[0].hidden, sumMax: k.sm.max.textContent, next: k.nexts[0].disabled };
// 10. the same form with a fresh init keeps its maximum and step (no reset of the typed value)
wbBuy.init(k);
results.reinit = { max: k.elements.max_price.value, step: String(k.dataset.step) };
// 11. an ad without a price: optional, no percent, no pill, step 2 passable, the summary says so
let n = form({ ...base, ask: 0, checked: 'direct', max: '', fair: false, step: '2' }); wbBuy.init(n);
wbBuy.blur(n);                                                // no price, no default: nothing to refill
results.noPrice = { next2: n.nexts[1].disabled, value: n.elements.max_price.value, sumMax: n.sm.max.textContent, pill: pill(n), pct: wbBuy.percent(n) };
n.elements.max_price.value = '100'; wbBuy.input(n);
results.noPriceTyped = { sumMax: n.sm.max.textContent, pill: pill(n), next2: n.nexts[1].disabled };
// 14. fee, costs and item (PayLivery, shipping 7.90): the cents maths that purchase.py mirrors, also for direct and pickup
let c14 = form({ ...base, checked: 'paylivery', max: '300' }); wbBuy.init(c14);
const items = [0.5, 1, 5, 10, 22, 22.01, 22.5, 23, 25, 100, 250, 281.34, 300, 999.99, 1110, 1110.89, 1111, 1500, 5000];
const maxima = [5, 7, 8, 20, 23, 100, 300, 321.4, 1161, 2000, 7000];
results.fees = items.map(i => wbBuy.fee(c14, i));
results.totals = items.map(i => wbBuy.costs(c14, i).total);
results.roundTrip = items.map(i => wbBuy.item(c14, wbBuy.costs(c14, i).total));
results.ceilings = maxima.map(m => wbBuy.item(c14, m));
results.costs300 = wbBuy.costs(c14, 300);
c14.radios.paylivery.checked = false; c14.radios.direct.checked = true;
results.direct = { fee: wbBuy.fee(c14, 300), costs: wbBuy.costs(c14, 300), item: wbBuy.item(c14, 300) };
c14.radios.direct.checked = false; c14.radios.pickup.checked = true;
results.pickupCosts = { costs: wbBuy.costs(c14, 300), item: wbBuy.item(c14, 300), base: wbBuy.base(c14) };
let c15 = form({ ...base, checked: 'paylivery', max: '300', fee: false }); wbBuy.init(c15);   // no rule in the form: no fee
results.noRule = { fee: wbBuy.fee(c15, 300), total: wbBuy.costs(c15, 300).total, item: wbBuy.item(c15, 300), base: wbBuy.base(c15) };
// 15. the summary row "How to buy": the fee of the item price at the maximum (else of the asking price) only with PayLivery
let c16 = form({ ...base, checked: 'paylivery', max: '216.90', shipText: { paylivery: 'Shipping € 7.90', direct: 'Shipping about € 6' }, step: '3' }); wbBuy.init(c16);
results.sumFee = c16.sm.ship.textContent;                         // item 200 -> 9
c16.elements.max_price.value = ''; wbBuy.input(c16);
results.sumFeeEmpty = c16.sm.ship.textContent;                    // no maximum: the asking price 250 -> 11.25
c16.radios.paylivery.checked = false; c16.radios.direct.checked = true; wbBuy.mode(c16);
results.sumNoFee = c16.sm.ship.textContent;
// 12. the inverse of place(): round trip, clamped ends, equal prices merged
const sc = FAIR.scale, dup = [[100, 0], [100, 0.2], [200, 0.5], [300, 1]];       // 100 twice: merged to 0.1
results.inverse = { mid: wbBuy.unplace(sc, 0.5), q10: wbBuy.unplace(sc, 0.1), between: wbBuy.unplace(sc, 0.625), lo: wbBuy.unplace(sc, -3), hi: wbBuy.unplace(sc, 7),
  roundTrip: [0, 0.07, 0.3, 0.5, 0.8, 0.95, 1].map(q => Math.abs(wbBuy.place(sc, wbBuy.unplace(sc, q)) - q) < 1e-9),
  dup: [wbBuy.unplace(dup, 0), wbBuy.unplace(dup, 0.1), wbBuy.unplace(dup, 0.3), wbBuy.unplace(dup, 1)], dupPlace: wbBuy.place(dup, 100) };
// 13. dragging the ring: the item price from the position plus the shipping of the mode, live pill, clamped to the bar
let d = form({ ...base, checked: 'paylivery', max: '257.90' }); wbBuy.init(d);
results.bound = { listeners: Object.keys(d.gauge.listeners).sort(), once: d.gauge.dataset.bound };
wbBuy.init(d);                                                       // a second init does not bind again
const drags = {};
d.gauge.listeners.pointerdown({ pointerType: 'mouse', button: 0, pointerId: 7, clientX: 300, preventDefault() {} });   // 50 % = the median 200
drags.down = { value: d.elements.max_price.value, flag: d.elements.max_default.value, pin: d.pins[0].style.left, pill: pill(d), drag: d.gauge.classList.contains('bd-dragging'), captured: [...d.gauge.captured] };
d.gauge.listeners.pointermove({ clientX: 100 + 400 * 0.625 });      // item 225 (between median and p75): 232.90
drags.move = { value: d.elements.max_price.value, pill: pill(d), sum: d.sm.max.textContent };
d.gauge.listeners.pointermove({ clientX: 9999 });                    // far right: clamped to the dearest comparable (400)
drags.right = d.elements.max_price.value;
d.gauge.listeners.pointermove({ clientX: -50 });                     // far left: the cheapest (100)
drags.left = { value: d.elements.max_price.value, pin: d.pins[0].style.left };
d.gauge.listeners.pointerup({});
d.gauge.listeners.pointermove({ clientX: 300 });                     // released: no longer follows
drags.after = { value: d.elements.max_price.value, drag: d.gauge.classList.contains('bd-dragging') };
d.gauge.listeners.pointerdown({ pointerType: 'mouse', button: 2, pointerId: 8, clientX: 400, preventDefault() {} });   // right button: ignored
drags.right_button = d.elements.max_price.value;
d.gauge.listeners.pointerdown({ pointerType: 'touch', button: 0, pointerId: 9, clientX: 100 + 400 * 0.75, preventDefault() {} });   // a finger on the needle: 250 + 7.90 = the default
drags.touch = { value: d.elements.max_price.value, flag: d.elements.max_default.value, pill: pill(d) };
d.gauge.listeners.pointercancel({});
results.drags = drags;
const cheapItem = form({ ...base, ask: 6, ship: { paylivery: 1, direct: 1, pickup: 0 }, checked: 'pickup', max: '6' });
cheapItem.dataset.fair = JSON.stringify({ ...FAIR, scale: [[2, 0], [4, 0.25], [5, 0.5], [6, 0.75], [9, 1]] });
wbBuy.drag(cheapItem, 0.4);                                        // item 4.6 (cents below € 10)
results.cheapDrag = cheapItem.elements.max_price.value;
results.noScaleDrag = wbBuy.drag(k, 0.5);                           // no data-fair: nothing happens
console.log(JSON.stringify(results));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the dialog script")
def test_buy_steps_script_in_node():  # noqa: F811
    """The three steps, the lines and the summary of window.wbBuy (_buy_script.html), run without a browser on a fake form."""
    import json
    import subprocess

    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "_buy_script.html").read_text(encoding="utf-8")
    script = re.search(r"<script>\s*// Buy dialog.*?</script>", html, re.S).group(0)
    code = ("var window = {};\n" + script.replace("<script>", "").replace("</script>", "")
            + "\nconst wbBuy = window.wbBuy;\n" + JS_HARNESS)
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["init"] == {"js": True, "shown": [True, False, False], "chips": [[False, True], [True, False], [True, False]],
                         "next1": True}
    # PayLivery: the default maximum is the asking price 250 + shipping 7.90 + buyer protection 11.25 = 269.15
    assert r["mode"]["max"] == "269.15" and r["mode"]["next1"] is False and r["mode"]["maxDefault"] == "1"
    assert r["mode"]["sumMax"] == "€ 269,15"
    assert r["mode"]["sumMode"] == "paylivery" and r["mode"]["sumShip"] == "Shipping € 7.90 · Buyer protection € 11,25"
    assert r["step2"] == {"step": "2", "shown": [False, True, False], "chipDone": True, "chipOn": True, "next2": False}
    # the default is the ad's own price: 0 %, expensive (0.75); the ring is shown (around the needle), not hidden; the chips
    # add shipping 7.90 and the fee of their own item price (200: 9, 150: 6.75)
    assert r["dflt"] == {"pill": ["0 % · expensive", False, True], "pinHidden": False,
                         "chips": ["Cheap · € 216,90", "Cheap · € 164,65"], "on": [False, False]}
    # cleared: valid and passable, no pill and no ring while empty, the summary shows the default; leaving the field refills the number
    assert r["empty"] == {"next2": False, "maxDefault": "1", "pill": ["", True, False], "pinHidden": True, "sumMax": "€ 269,15"}
    assert r["blur"] == {"value": "269.15", "maxDefault": "1", "pill": ["0 % · expensive", False, True], "pinHidden": False}
    assert r["emptyMode"] == "269.15" and r["badInput"] is False and r["emptyGoes"] == {"step": "3", "value": "269.15"}
    assert r["changed"] == "" and r["changedKept"] == "200" and r["backToDefault"] == "1" and r["chipRefill"] == "269.15"
    assert r["below1"] == {"next2": True, "maxDefault": "", "pill": ["", True, False]}       # below € 1 (and no item price left): no pill
    assert r["fairPill"]["pill"] == ["−19 % · fair", False, True] and r["fairPill"]["cls"] and r["fairPill"]["summaryPill"] == "−19 % · fair"
    assert r["fairPill"]["pin"] == [False, "50.0%"] and r["fairPill"]["sumMax"] == "€ 216,90"
    assert r["fairPill"]["chips"] == ["Cheap · € 216,90", "Cheap · € 164,65"] and r["fairPill"]["on"] == [True, False]
    assert r["enter"] == {"step": "3", "prevented": 1} and r["linkEnter"] == 0 and r["invalidStep2"] is False
    assert r["above"] == {"pill": ["+49 % · very expensive", False, True], "cls": True, "pin": [False, "97.5%"]}
    assert r["bargain"] == {"pill": ["−59 % · bargain", False, True], "cls": True, "pin": "0.0%"}
    assert r["back1"]["step"] == "1" and r["back1"]["shown"] == [True, False, False] and r["back1"]["scrolled"]
    assert r["back1"]["timerowHidden"] is True and r["back1"]["chipFocus"] is True
    assert r["pickup"]["max"] == "216.90" and r["pickup"]["timerowHidden"] is False       # typed maximum is kept
    assert r["pickup"]["maxDefault"] == ""
    assert r["pickup"]["ship"] == "cash, no shipping" and r["pickup"]["chips"] == ["Cheap · € 200", "Cheap · € 150"]
    assert r["pickup"]["pill"] == ["−13 % · fair", False, True]                           # no shipping, no fee now: item 216.90, base 250
    assert r["time"] == "Samstag"
    assert r["jump"]["step"] == "1"                                  # no mode yet: stops at step 1
    assert r["jumpEmpty"] == {"step": "3", "sumMax": "€ 256", "value": "256.00"}          # the jump refilled the default
    assert r["pickDefault"] == "1"
    assert r["pick"]["max"] == "206.00" and r["pick"]["maxDefault"] == "" and r["pick"]["chip"] == "Cheap · € 206"
    assert r["pick"]["on"] is True and r["pick"]["pill"] == ["−20 % · fair", False, True]   # item 200 = the median, base 256
    assert r["noScale"] == {"pill": ["", True, False], "pinHidden": True, "sumMax": "€ 300", "next": False}
    assert r["reinit"]["max"] == "300" and r["reinit"]["step"] == "1"
    assert r["noPrice"] == {"next2": False, "value": "", "sumMax": "no price in the ad", "pill": ["", True, False], "pct": None}
    assert r["noPriceTyped"] == {"sumMax": "€ 100", "pill": ["", True, False], "next2": False}
    # inverse of the position scale: the exact mirror of place(), clamped to the bar, equal prices merged
    assert r["inverse"]["mid"] == 200 and r["inverse"]["q10"] == 120 and r["inverse"]["between"] == 225
    assert r["inverse"]["lo"] == 100 and r["inverse"]["hi"] == 400 and all(r["inverse"]["roundTrip"])
    assert r["inverse"]["dup"] == [100, 100, 150, 300] and r["inverse"]["dupPlace"] == 0.1
    # dragging: one binding, the ring follows the pointer, the price is the item price at that position plus the shipping
    assert r["bound"] == {"listeners": ["lostpointercapture", "pointercancel", "pointerdown", "pointermove", "pointerup"], "once": "1"}
    dr = r["drags"]
    # the ring maps item <-> total through costs(): item 200 -> 200 + 7.90 + 9 = 216.90 and the pin lands on exactly 50 % again
    assert dr["down"] == {"value": "216.90", "flag": "", "pin": "50.0%", "pill": ["−19 % · fair", False, True], "drag": True, "captured": [7]}
    assert dr["move"] == {"value": "243.03", "pill": ["−10 % · fair", False, True], "sum": "€ 243,03"}   # item 225 + 7.90 + 10.13
    assert dr["right"] == "425.90" and dr["left"] == {"value": "112.40", "pin": "0.0%"}              # 400 + 7.90 + 18 / 100 + 7.90 + 4.50
    assert dr["after"] == {"value": "112.40", "drag": False} and dr["right_button"] == "112.40"
    assert dr["touch"] == {"value": "269.15", "flag": "1", "pill": ["0 % · expensive", False, True]}   # on the needle: the default again
    assert r["cheapDrag"] == "4.60" and r["noScaleDrag"] is False
    # costs()/item() are the same cents maths as purchase.py: fee, total and the inverse agree for all three fee regimes
    from app.services.purchase import costs_of, item_ceiling, paylivery_fee
    items = [0.5, 1, 5, 10, 22, 22.01, 22.5, 23, 25, 100, 250, 281.34, 300, 999.99, 1110, 1110.89, 1111, 1500, 5000]
    maxima = [5, 7, 8, 20, 23, 100, 300, 321.4, 1161, 2000, 7000]
    ad = dict(mode="paylivery", shipping_cost=7.9, shipping_source="ad")
    assert r["fees"] == [paylivery_fee(i) for i in items]
    assert r["totals"] == [costs_of(Purchase(max_price=300.0, **ad), i)["total"] for i in items]
    assert r["roundTrip"] == items                                    # total -> item gives the item price back
    assert [max(1.0, c) for c in r["ceilings"]] == [item_ceiling(Purchase(max_price=m, **ad)) for m in maxima]
    assert r["ceilings"][:3] == [0, 0, 0]                             # nothing left after the shipping: 0, not negative
    assert r["costs300"] == {"ship": 7.9, "fee": 13.5, "total": 321.4}
    assert r["direct"] == {"fee": 0, "costs": {"ship": 6, "fee": 0, "total": 306}, "item": 294}        # no fee without PayLivery
    assert r["pickupCosts"] == {"costs": {"ship": 0, "fee": 0, "total": 300}, "item": 300, "base": 250}
    assert r["noRule"] == {"fee": 0, "total": 307.9, "item": 292.1, "base": 257.9}                      # no data-fee: no fee
    # the summary row names the fee of the item price at the maximum (the asking price while the field is empty)
    assert r["sumFee"] == "Shipping € 7.90 · Buyer protection € 9" and r["sumFeeEmpty"] == "Shipping € 7.90 · Buyer protection € 11,25"
    assert r["sumNoFee"] == "Shipping about € 6"
