"""Sold / reserved workflow: willhaben calls (fake session), notice drafts, auto replies off, web flow."""
import json

import pytest
from sqlmodel import select

from app.db import Conversation, Draft, Message, MyAd, session, set_setting, utcnow
from app.services import chats, sold
from app.willhaben import sales
from app.willhaben.account import parse_my_adverts
from app.willhaben.session import ApiResponse, NotLoggedIn

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
    """Logged-in browser stand-in: serves the my-adverts page, records every other call."""

    def __init__(self, page, reasons=None, fail=False):
        self.page, self.reasons, self.fail, self.calls = page, reasons, fail, []

    async def api(self, method, path, body=None, accept="application/json"):
        assert (method, path) == ("GET", "/iad/myprofile/myadverts")
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(self.page)}</script>'
        return ApiResponse(status=200, text=html, url="https://www.willhaben.at" + path)

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/webapi/iad/soldreasons"):
            return self.reasons
        if self.fail:
            raise RuntimeError(f"{method} {path}: HTTP 409 conflict")
        return None

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


# ------------------------------------------------------------------ willhaben layer
def test_parse_keeps_action_links():
    ad = parse_my_adverts(_page(_links("setAdSold", "markAdReserved"), "adId=1"))[0]
    assert set(ad.links) == {"setAdSold", "markAdReserved"} and ad.reasons_query
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
        async def api_json(self, method, path, body=None):
            raise NotLoggedIn(path)
    with pytest.raises(NotLoggedIn):
        await sales.mark_reserved(Expired(_page(_links("markAdReserved"))), AD)


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

    sent, errors = await sold.send_notices(AD, [(open_[0].id, "Leider schon verkauft, sorry!")])
    assert (sent, errors) == (1, []) and account.sent == [("c-2", "Leider schon verkauft, sorry!")]
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
    assert await sold.send_notices(AD, [(i, "x") for i in ids]) == (0, [])
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
        assert "1 messages sent" in r.text and ad_with_chats.sent == [("c-2", "Schon weg, sorry.")]

        r = c.post(f"/sold/{AD}/finish", data={"kind": "sold", "buyer": "c-buyer", "auto_off": "1"})
        assert "Saved" in r.text
        ad = sold.ad_row(AD)
        assert ad.sale_status == "sold" and ad.sale_conversation_id == "c-buyer" and ad.auto_reply is False
        assert "to this buyer" in c.get("/chats/c-buyer").text
