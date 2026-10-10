"""Auto-renew: ACTIVATE / REPUBLISH from the ad's action links, settings carried to the republished ad."""
import pytest

from app.db import MyAd, session
from app.services import my_ads
from app.willhaben import account
from app.willhaben.account import AccountClient, EndpointUnknown, OwnAdvert

LINKS = {"contextLink": [{"id": "selfLinkSave", "relativePath": "/bap/save", "serviceName": "iad"},
                         {"id": "imageList", "relativePath": "/atz/2/images", "serviceName": "iad"}]}


def _own(status, actions):
    return OwnAdvert(ad_id="1000000001", heading="Max Muster Rad", status=status, status_label=status, price_display="50",
                     start=None, end=None, url="u", image_url=None,
                     actions={a: {"id": a, "relativePath": f"/bap/{a}/1000000001", "serviceName": "iad"} for a in actions})


class FakeSession:
    def __init__(self):
        self.calls = []

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/webapi/iad/bap/6/1000000001":
            return {"adId": 1000000002, "adTypeId": 67}
        if path == "/webapi/iad/bap/1000000002":
            return {"adId": 1000000002, "adUuid": "u2", "heading": "Max Muster Rad", "price": 50, "contextLinkList": LINKS}
        if path.endswith("/save"):
            assert body["heading"] == "Max Muster Rad" and body["price"] == 50   # full editor JSON, not a stub
            return {"adId": 1000000002, "adUuid": "u2"}
        if path.endswith("/carts"):
            return {"shoppingCartId": "c1"}
        if path.endswith("/items"):
            return {"totalPrice": 0}
        if path.endswith("/checkout"):
            return {"status": "ACTIVATED"}
        return None


def _client(monkeypatch, own):
    s = FakeSession()
    c = AccountClient(s)

    async def my_adverts():
        return [own]

    monkeypatch.setattr(c, "my_adverts", my_adverts)
    return c, s


async def test_deactivated_ad_is_activated_in_place(monkeypatch):
    c, s = _client(monkeypatch, _own("deactivated", ["4"]))
    assert await c.reactivate("1000000001") == "1000000001"
    assert s.calls == [("PUT", "/webapi/iad/bap/4/1000000001")]


async def test_expired_ad_is_republished_and_finished(monkeypatch):
    c, s = _client(monkeypatch, _own("expired", ["6"]))
    assert await c.reactivate("1000000001") == "1000000002"
    paths = [p for _, p in s.calls]
    assert paths[0] == "/webapi/iad/bap/6/1000000001" and s.calls[0][0] == "POST"
    assert paths[-1].endswith("/checkout") and "/webapi/iad/bap/1000000002" in paths


async def test_passive_ad_is_republished(monkeypatch):
    """Live 2026-10-09: "Passiv" ads offer only Neu veröffentlichen (6), Rechnung anfordern and Löschen, no 4."""
    own = _own("passive", [])
    own.actions = {"6": {"id": "6", "description": "Neu veröffentlichen", "relativePath": "/bap/6/1000000001",
                         "serviceName": "iad"},
                   "requestBillLink": {"id": "requestBillLink", "relativePath": "/payment/all/1000000001",
                                       "serviceName": "iad"},
                   "3": {"id": "3", "description": "Löschen", "relativePath": "/bap/1000000001", "serviceName": "myatz"}}
    c, s = _client(monkeypatch, own)
    assert await c.reactivate("1000000001", [own]) == "1000000002"
    assert s.calls[0] == ("POST", "/webapi/iad/bap/6/1000000001") and s.calls[-1][1].endswith("/checkout")


async def test_passive_ad_deactivated_by_hand_is_activated_in_place(monkeypatch):
    c, s = _client(monkeypatch, _own("passive", ["4", "6"]))   # "Passiv" after Deaktivieren: Aktivieren wins
    assert await c.reactivate("1000000001") == "1000000001"
    assert s.calls == [("PUT", "/webapi/iad/bap/4/1000000001")]


async def test_active_ad_is_never_republished(monkeypatch):
    c, s = _client(monkeypatch, _own("active", ["6"]))
    with pytest.raises(EndpointUnknown):
        await c.reactivate("1000000001")
    assert s.calls == []


async def test_republish_copies_settings_and_keeps_old_row(monkeypatch):
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Max Muster Rad", status="expired", auto_renew=True,
                    auto_reply=True, auto_reply_instructions="Preis fix"))
        db.commit()

    async def reactivate(self, ad_id):
        return "1000000002"

    sent = []

    async def notify(kind, title, message, **kw):
        sent.append(kind)

    async def check(ad_id):
        raise AssertionError("a just republished ad is not public yet: the public page would call it expired")

    async def import_from_account():
        return {"1000000002"}

    monkeypatch.setattr(account.AccountClient, "reactivate", reactivate)
    monkeypatch.setattr(my_ads, "notify", notify)
    monkeypatch.setattr(my_ads, "check", check)
    monkeypatch.setattr(my_ads, "import_from_account", import_from_account)
    monkeypatch.setattr(my_ads, "get_session", lambda: None)
    with session() as db:
        row = db.get(MyAd, "1000000001")
    await my_ads._reactivate(row)

    with session() as db:
        old, new = db.get(MyAd, "1000000001"), db.get(MyAd, "1000000002")
    assert (new.auto_renew, new.auto_reply, new.auto_reply_instructions, new.status) == (True, True, "Preis fix", "active")
    assert (old.replaced_by, old.auto_renew, old.auto_reply) == ("1000000002", False, True)   # old chats keep working
    assert sent == ["ad_renewed"]
