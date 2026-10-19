"""willhaben's exact PayLivery fee from the chat's PayLivery window (app/willhaben/paylivery_fee.py)."""
import pytest

from app.willhaben import paylivery_fee as pf


@pytest.mark.parametrize("text, fee", [
    ("Artikel\n€ 420,00\nKäuferschutzgebühr\n€ 29,40\nGesamt\n€ 452,89", 29.4),
    ("Käuferschutzgebühr (7 %) € 1.029,40", 1029.4),
    ("29,40 €\nKäuferschutzgebühr", 29.4),
    ("Versand € 3,49", None),
])
def test_fee_from_the_windows_text(text, fee):
    assert pf.fee_from_text(text) == fee


def test_the_buy_now_window_first():
    offer = {"initName": "at.willhaben.messaging.p2p", "button": {"baseHref": "https://x/offer"}}
    buy = {"initName": "at.willhaben.messaging.p2p-buynow", "button": {"baseHref": "https://x/buy"}}
    other = {"initName": "at.willhaben.messaging.phone", "button": {"baseHref": "https://x/phone"}}
    assert pf.pick_integration({"ads": [{"integrations": [other, offer, buy]}]}) is buy
    assert pf.pick_integration({"ads": [{"integrations": [other, offer]}]}) is offer
    assert pf.pick_integration({"ads": [{"integrations": [other]}]}) is None
    assert pf.pick_integration({}) is None and pf.pick_integration(None) is None


class _Sess:
    user = {"uuid": "me-uuid"}

    def __init__(self, text):
        self.text, self.calls, self.opened = text, [], []

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path))
        return {"ads": [{"integrations": [{"initName": "at.willhaben.messaging.p2p-buynow",
                                            "button": {"baseHref": "https://p2p.example/buy"}}]}]}

    async def read_hidden(self, url, wait_for, timeout_ms=25000):
        self.opened.append(url)
        return {"text": self.text, "found": True, "requests": [{"method": "POST", "url": "u", "blocked": True}],
                "bodies": {}}


async def test_fetch_reads_the_window_read_only(monkeypatch):
    async def ids(ad_id):
        return "ad-uuid", "seller-uuid"

    monkeypatch.setattr(pf, "_ids", ids)
    pf._cache.clear()
    s = _Sess("Käuferschutzgebühr\n€ 29,40")
    assert (await pf.fetch(s, "123", 420.0))["fee"] == 29.4
    assert s.calls == [("GET", "/webapi/convoad/v1/chat/ad_list/me-uuid?ids=123")]
    assert s.opened == ["https://p2p.example/buy?itemId=ad-uuid&partnerId=seller-uuid&activeUserId=me-uuid"]
    await pf.fetch(s, "123", 420.0)
    assert len(s.opened) == 1                                     # kept for an hour
    s2 = _Sess("Bitte melde dich an")
    assert (await pf.fetch(s2, "124", 50.0))["fee"] is None


def test_the_breakdown_loads_the_fee(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    async def fetch(sess, ad_id, price=None):
        if ad_id == "2":
            raise pf.FeeError("kein PayLivery-Fenster")
        return {"fee": 29.4}

    monkeypatch.setattr(pf, "fetch", fetch)
    with TestClient(app) as c:
        frag = c.get("/wh-fee/1?price=420&sp=3.49").text
        assert "€ 29,40" in frag and "€ 452,89" in frag
        frag = c.get("/wh-fee/2?price=420").text
        assert "kein PayLivery-Fenster" in frag and "buyer protection fee" in frag
        assert c.get("/wh-fee/abc").status_code == 400
