"""Sold / reserved workflow: willhaben calls (fake session), notice drafts, auto replies off, web flow."""
import json

import pytest
from sqlmodel import select

from app.db import Conversation, Draft, Message, MyAd, session, set_setting, utcnow
from app.services import chats, sold
from app.willhaben import sales
from app.willhaben.account import parse_my_adverts
from app.willhaben.session import ApiResponse, NotLoggedIn
from tests.conftest import finish_jobs, wait_jobs

AD = "1000000001"


def _links(*ids):
    paths = {"setAdSold": f"/bap/sold/{AD}", "markAdReserved": f"/bap/updatesalesinfo/{AD}/5",
             "removeMarkAdReserved": f"/bap/updatesalesinfo/{AD}/0", "2": f"/bap/deactivate/{AD}"}
    return {"contextLink": [{"id": i, "serviceName": "iad", "relativePath": paths[i]} for i in ids]}


def _page(links, reasons_query=None):
    ad = {"id": AD, "description": "Fahrrad", "advertStatus": {"id": "active", "description": "Aktiv"},
          "advertEditStatusActionsList": {"statusId": 50, "contextLinkList": links},
          "deleteReasonQueryParameter": reasons_query, "attributes": {"attribute": []}}
    return {"page": "/iad/myprofile/myadverts", "props": {"pageProps": {"initialUserAdsList": {
        "verticalList": [{"verticalId": 5, "advertDetails": [ad]}]}}}}


REASONS = {"header": "Wo hast du verkauft?", "soldReasons": [
    {"soldReasonId": 1, "description": "Über willhaben", "playAnimation": True},
    {"soldReasonId": 2, "description": "Woanders", "playAnimation": False}]}


class FakeSession:
    """Logged-in browser stand-in: serves the my-adverts page, records every other call (all go through api())."""

    def __init__(self, page, reasons=None, fail=False, write_text="", reasons_status=200):
        self.page, self.reasons, self.fail, self.calls = page, reasons, fail, []
        self.write_text, self.reasons_status, self.logged_in = write_text, reasons_status, True

    async def api(self, method, path, body=None, accept="application/json"):
        if (method, path) == ("GET", "/iad/myprofile/myadverts"):
            html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(self.page)}</script>'
            return ApiResponse(status=200, text=html, url="https://www.willhaben.at" + path)
        self.calls.append((method, path, body))
        url = "https://www.willhaben.at" + path
        if method == "GET" and path.startswith("/webapi/iad/soldreasons"):
            return ApiResponse(status=self.reasons_status, text=json.dumps(self.reasons) if self.reasons else "", url=url)
        if self.fail:
            return ApiResponse(status=409, text="conflict", url=url)
        return ApiResponse(status=200, text=self.write_text, url=url)

    async def refresh_sso(self):
        return False

    async def api_json(self, method, path, body=None):
        raise AssertionError("sales.py decides on the HTTP status itself, not via api_json")

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


# ------------------------------------------------------------------ willhaben layer
def test_parse_keeps_action_links():
    ad = parse_my_adverts(_page(_links("setAdSold", "markAdReserved"), "adId=1"))[0]
    assert set(ad.actions) == {"setAdSold", "markAdReserved"} and ad.reasons_query
    assert sales.action_path(ad, sales.SOLD) == f"/webapi/iad/bap/sold/{AD}"
    assert sales.offers(ad) == {"sold": True, "reserve": True, "unreserve": False}
    with pytest.raises(sales.NotOffered):
        sales.action_path(ad, sales.UNRESERVE)


async def test_mark_sold_asks_for_reason_before_writing():
    s = FakeSession(_page(_links("setAdSold"), "adId=1"), REASONS)
    with pytest.raises(sales.ReasonRequired) as e:
        await sales.mark_sold(s, AD)
    assert [(r.id, r.label) for r in e.value.reasons] == [("1", "Über willhaben"), ("2", "Woanders")]
    assert not s.writes
    with pytest.raises(sales.ReasonRequired):
        await sales.mark_sold(s, AD, reason_id="99")        # not one of willhaben's reasons
    assert not s.writes

    await sales.mark_sold(s, AD, reason_id="1")
    # same order and bodies as the web app: reason (as string) first, then the sold link with PUT and no body
    assert s.writes == [("POST", f"/webapi/iad/soldreasons?adId={AD}", {"soldReasonId": "1"}),
                        ("PUT", f"/webapi/iad/bap/sold/{AD}", None)]


async def test_mark_sold_without_reason_query_and_not_offered():
    s = FakeSession(_page(_links("setAdSold")))
    await sales.mark_sold(s, AD)
    assert s.calls == [("PUT", f"/webapi/iad/bap/sold/{AD}", None)]

    s = FakeSession(_page(_links("2")))                     # e.g. expired ad: willhaben offers no "sold"
    with pytest.raises(sales.NotOffered):
        await sales.mark_sold(s, AD)
    assert not s.calls

    s = FakeSession({"page": "/iad/myprofile/myadverts", "props": {"pageProps": {"initialUserAdsList": {"verticalList": []}}}})
    with pytest.raises(sales.NotOffered, match="not in your willhaben list"):
        await sales.mark_sold(s, AD)


async def test_mark_reserved_and_undo():
    s = FakeSession(_page(_links("markAdReserved", "removeMarkAdReserved")))
    await sales.mark_reserved(s, AD)
    await sales.mark_reserved(s, AD, on=False)
    assert s.calls == [("PUT", f"/webapi/iad/bap/updatesalesinfo/{AD}/5", None),
                       ("PUT", f"/webapi/iad/bap/updatesalesinfo/{AD}/0", None)]


async def test_refused_change_and_login_errors():
    s = FakeSession(_page(_links("markAdReserved")), fail=True)
    with pytest.raises(sales.SaleFailed, match="409"):
        await sales.mark_reserved(s, AD)

    class Expired(FakeSession):
        async def api(self, method, path, body=None, accept="application/json"):
            r = await super().api(method, path, body, accept)
            return r if method == "GET" else ApiResponse(status=401, text="", url=r.url)
    s = Expired(_page(_links("markAdReserved")))
    with pytest.raises(NotLoggedIn):
        await sales.mark_reserved(s, AD)
    assert s.logged_in is False


async def test_write_with_non_json_2xx_body_counts_as_done():
    # willhaben's answer bodies are unknown: a 2xx with plain text / HTML must not be reported as "not changed"
    s = FakeSession(_page(_links("setAdSold")), write_text="OK")
    await sales.mark_sold(s, AD)
    assert s.writes == [("PUT", f"/webapi/iad/bap/sold/{AD}", None)]
    s = FakeSession(_page(_links("markAdReserved")), write_text="<!DOCTYPE html><html></html>")
    await sales.mark_reserved(s, AD)


async def test_sold_reasons_http_error_is_sale_failed(ad_with_chats, monkeypatch):
    s = FakeSession(_page(_links("setAdSold"), "adId=1"), REASONS, reasons_status=500)
    with pytest.raises(sales.SaleFailed, match="500"):
        await sales.mark_sold(s, AD)
    assert not s.writes

    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(sold, "get_session", lambda: s)
    with TestClient(app) as c:
        r = c.post(f"/sold/{AD}/willhaben", data={"kind": "sold", "buyer": "c-buyer"})
    assert r.status_code == 200 and "Not changed on willhaben" in r.text and not s.writes


def test_action_path_rejects_odd_links():
    for link in ({"id": "setAdSold", "serviceName": "iad", "relativePath": "/bap/../x"},
                 {"id": "setAdSold", "serviceName": "iad/x", "relativePath": "/bap/sold/1"}):
        ad = parse_my_adverts(_page({"contextLink": [link]}))[0]
        with pytest.raises(sales.NotOffered):
            sales.action_path(ad, sales.SOLD)


# ------------------------------------------------------------------ service
class _Account:
    def __init__(self):
        self.sent = []

    async def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return "m1"

    async def mark_read(self, conversation_id, msgs=None):
        return 0


@pytest.fixture
def ad_with_chats(monkeypatch):
    account = _Account()
    monkeypatch.setattr(chats, "_account", lambda: account)
    with session() as db:
        db.add(MyAd(ad_id=AD, heading="Fahrrad", status="active", auto_reply=True, auto_reply_instructions="Preis fix"))
        for cid, name in (("c-buyer", "Max Muster"), ("c-2", "Erika Muster"), ("c-3", None)):
            db.add(Conversation(id=cid, ad_id=AD, ad_title="Fahrrad", partner_name=name, role="seller"))
            db.add(Message(conversation_id=cid, ext_id=f"m-{cid}", direction="in", text="Ist das Rad noch da?",
                           created_at=utcnow()))
        db.add(Conversation(id="c-other-ad", ad_id="1000000002", partner_name="Max Muster", role="seller"))
        db.commit()
    set_setting("send_verified", "1")
    return account


async def test_notices_for_other_buyers_only_after_send(ad_with_chats):
    account = ad_with_chats
    assert [o.conv.id for o in sold.others(AD, "c-buyer")] == ["c-2", "c-3"]
    made, errors = await sold.prepare_notices(AD, "sold", ["c-2", "c-3", "c-buyer", "c-other-ad"], buyer="c-buyer")
    assert (made, errors) == (2, []) and not account.sent
    drafts = {o.conv.id: o.draft for o in sold.others(AD, "c-buyer")}
    assert drafts["c-2"].text.startswith("Hallo Erika Muster,") and "schon verkauft" in drafts["c-2"].text
    assert drafts["c-3"].text.startswith("Hallo,") and drafts["c-2"].intent == "sold_notice"

    # preparing again replaces the unsent draft instead of piling up
    await sold.prepare_notices(AD, "sold", ["c-2"], buyer="c-buyer")
    with session() as db:
        open_ = list(db.exec(select(Draft).where(Draft.conversation_id == "c-2", Draft.status == "draft")))
    assert len(open_) == 1

    assert sold.send_notices(AD, [(open_[0].id, "Leider schon verkauft, sorry!")]) == 1
    assert sold.others(AD, "c-buyer")[0].sending is not None                    # queued: shown as "sending"
    await finish_jobs()
    assert account.sent == [("c-2", "Leider schon verkauft, sorry!")]
    others = {o.conv.id: o for o in sold.others(AD, "c-buyer")}
    assert others["c-2"].informed and others["c-3"].draft is not None and not others["c-3"].informed

    assert sold.discard_notices(AD, [others["c-3"].draft.id]) == 1
    assert sold.others(AD, "c-buyer")[1].draft is None


async def test_send_notices_ignores_foreign_drafts(ad_with_chats):
    account = ad_with_chats
    with session() as db:
        normal = Draft(conversation_id="c-2", text="normaler Entwurf")
        foreign = Draft(conversation_id="c-other-ad", text="x", intent="sold_notice")
        db.add(normal)
        db.add(foreign)
        db.commit()
        ids = [normal.id, foreign.id]
    assert sold.send_notices(AD, [(i, "x") for i in ids]) == 0
    assert not account.sent


async def test_finish_switches_auto_replies_off_and_blocks_sold(ad_with_chats):
    conv = Conversation(id="c-2", ad_id=AD, role="seller")
    assert chats._auto_ad(conv) is not None
    sold.finish(AD, "reserved", "c-buyer")
    ad = sold.ad_row(AD)
    assert ad.auto_reply is False and ad.auto_reply_instructions == "Preis fix" and ad.sale_status == "reserved"

    with session() as db:   # even if someone switches auto replies on again: a sold ad gets none
        row = db.get(MyAd, AD)
        row.auto_reply, row.sale_status = True, "sold"
        db.add(row)
        db.commit()
    assert chats._auto_ad(conv) is None
    assert await chats.process_pending_auto(AD) == 0 and not ad_with_chats.sent
    assert sold.state_for(Conversation(id="c-buyer", ad_id=AD))["kind"] == "sold"

    sold.reset(AD)
    assert sold.ad_row(AD).sale_status is None and chats._auto_ad(conv) is not None


async def test_on_willhaben_updates_status(ad_with_chats, monkeypatch):
    s = FakeSession(_page(_links("setAdSold", "markAdReserved", "removeMarkAdReserved")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    await sold.on_willhaben(AD, "reserved")
    assert sold.ad_row(AD).status == "reserved"
    await sold.on_willhaben(AD, "reserved", undo=True)
    assert sold.ad_row(AD).status == "active"
    with pytest.raises(sales.NotOffered):
        await sold.on_willhaben(AD, "sold", undo=True)
    await sold.on_willhaben(AD, "sold")
    assert sold.ad_row(AD).status == "sold" and s.writes[-1] == ("PUT", f"/webapi/iad/bap/sold/{AD}", None)


# ------------------------------------------------------------------ web flow
def test_web_flow(ad_with_chats, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    s = FakeSession(_page(_links("setAdSold"), "adId=1"), REASONS)
    monkeypatch.setattr(sold, "get_session", lambda: s)
    with TestClient(app) as c:
        assert "Sold to Max Muster" in c.get("/chats/c-buyer").text
        assert f"/sold/{AD}?kind=sold" in c.get("/myads").text
        page = c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text
        assert "Erika Muster" in page and "Mark as sold on willhaben" in page
        assert page.count('name="conv"') == 2 and page.count("checked>") >= 2 and "Pick the buyer above" not in page
        nobuyer = c.get(f"/sold/{AD}?kind=sold").text   # buyer unknown: nobody pre-checked (the buyer is among them)
        assert nobuyer.count('name="conv"') == 3 and "Pick the buyer above" in nobuyer
        assert not any('name="conv"' in line and "checked" in line for line in nobuyer.splitlines())

        r = c.post(f"/sold/{AD}/willhaben", data={"kind": "sold", "buyer": "c-buyer"})
        assert r.status_code == 200 and "Über willhaben" in r.text and not s.writes   # reason first, nothing written
        r = c.post(f"/sold/{AD}/willhaben", data={"kind": "sold", "buyer": "c-buyer", "reason_id": "2"})
        assert "Marked as sold on willhaben" in r.text and len(s.writes) == 2

        r = c.post(f"/sold/{AD}/notices", data={"kind": "sold", "buyer": "c-buyer", "conv": ["c-2", "c-3"],
                                                 "mode": "template"})
        assert "2 drafts prepared" in r.text and "Send selected" in r.text and not ad_with_chats.sent
        drafts = {o.conv.id: o.draft.id for o in sold.others(AD, "c-buyer")}
        r = c.post(f"/sold/{AD}/notices/send", data={"kind": "sold", "buyer": "c-buyer", "action": "send",
                                                      "send": [str(drafts["c-2"])], f"text_{drafts['c-2']}": "Schon weg, sorry."})
        assert "1 messages queued" in r.text
        wait_jobs()
        assert ad_with_chats.sent == [("c-2", "Schon weg, sorry.")]
        assert "informed" in c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text

        r = c.post(f"/sold/{AD}/finish", data={"kind": "sold", "buyer": "c-buyer", "auto_off": "1"})
        assert "Saved" in r.text
        ad = sold.ad_row(AD)
        assert ad.sale_status == "sold" and ad.sale_conversation_id == "c-buyer" and ad.auto_reply is False
        assert "to this buyer" in c.get("/chats/c-buyer").text


async def test_sold_ad_is_never_auto_renewed(ad_with_chats, monkeypatch):
    from app.services import my_ads
    calls = []

    async def no_import():
        return {AD}

    async def fake_reactivate(row):
        calls.append(row.ad_id)

    async def fake_notify(*a, **kw):
        calls.append("notify")
    monkeypatch.setattr(my_ads, "import_from_account", no_import)
    monkeypatch.setattr(my_ads, "_reactivate", fake_reactivate)
    monkeypatch.setattr(my_ads, "notify", fake_notify)
    with session() as db:
        row = db.get(MyAd, AD)
        row.auto_renew = True
        db.add(row)
        db.commit()
    assert sold.finish(AD, "sold", "c-buyer").auto_renew is False
    with session() as db:   # switched on again by hand and expired on willhaben: still never reactivated
        row = db.get(MyAd, AD)
        row.status, row.auto_renew = "expired", True
        db.add(row)
        db.commit()
    await my_ads.check_all()
    assert calls == []

    s = FakeSession(_page(_links("setAdSold")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    sold.reset(AD)
    with session() as db:
        row = db.get(MyAd, AD)
        row.auto_renew = True
        db.add(row)
        db.commit()
    await sold.on_willhaben(AD, "sold")
    assert sold.ad_row(AD).auto_renew is False


def _fake_draft(on_draft=None):
    async def fake_draft(cid, hint=None, auto=False):
        if on_draft:
            on_draft()
        with session() as db:
            d = Draft(conversation_id=cid, text="Ja, noch da.", auto=True, status="draft", auto_reason="✓ ok")
            db.add(d)
            db.commit()
            db.refresh(d)
            return d
    return fake_draft


@pytest.mark.parametrize("humanized", [True, False])
async def test_running_auto_answer_stops_when_sold(ad_with_chats, monkeypatch, humanized):
    account = ad_with_chats
    finish = lambda: sold.finish(AD, "sold", "c-buyer", auto_off=False)  # noqa: E731 - sold, auto switch untouched
    monkeypatch.setattr(chats, "draft_reply", _fake_draft(None if humanized else finish))
    monkeypatch.setattr(chats, "_auto_blockers", lambda *a: [])

    async def typing(d):   # the owner marks the ad sold while the answer is "typing"
        finish()
        return True
    d = await chats.auto_reply("c-2", before_send=typing if humanized else None)
    assert d is None and not account.sent
    with session() as db:
        assert {x.status for x in db.exec(select(Draft).where(Draft.conversation_id == "c-2"))} == {"discarded"}


async def test_running_auto_answer_stops_when_auto_off(ad_with_chats, monkeypatch):
    account = ad_with_chats
    monkeypatch.setattr(chats, "draft_reply", _fake_draft())
    monkeypatch.setattr(chats, "_auto_blockers", lambda *a: [])

    async def typing(d):
        sold.finish(AD, "reserved", "c-buyer")   # step 3 with "auto answers off"
        return True
    assert await chats.auto_reply("c-2", before_send=typing) is None and not account.sent
