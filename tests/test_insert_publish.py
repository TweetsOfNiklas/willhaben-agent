"""insert.publish against a fake logged-in session: call order, bodies, money guard, retry and stop_after."""
from types import SimpleNamespace

import pytest

from app.willhaben import insert

LINKS = {"contextLink": [{"id": "imageList", "relativePath": "/atz/123/images", "serviceName": "iad"},
                         {"id": "selfLinkSave", "relativePath": "/bap/save", "serviceName": "iad"}]}
TEMPLATE = {"heading": "", "postCode": "1150", "contextLinkList": {"contextLink": [
    {"id": "presave", "relativePath": "/bap/presave", "serviceName": "iad"}]}, "treeAttributeIds": [1]}


class FakeSession:
    def __init__(self, total_price=0, payment_url=None, status="ACTIVATED"):
        self.calls, self.total, self.payment, self.status = [], total_price, payment_url, status

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/presave"):
            return {"adId": 123, "adUuid": "uuid-1", "contextLinkList": LINKS, "productId": 67}
        if path == "/webapi/iad/bap/555":
            return {"adId": 555, "adUuid": "uuid-5", "contextLinkList": LINKS}
        if path.endswith("/order-and-associate"):
            return {"advertImage": body["advertImage"]}
        if path.endswith("/save"):
            return {"adId": body["adId"], "adUuid": body["adUuid"]}
        if path.endswith("/carts"):
            return {"shoppingCartId": "c9", "status": "Open"}
        if path.endswith("/items"):
            return {"totalPrice": self.total, **({"paymentUrl": self.payment} if self.payment else {})}
        if path.endswith("/checkout"):
            return {"status": self.status}
        raise AssertionError(path)

    async def upload(self, path, field, name, data, mime):
        self.calls.append(("UPLOAD", path, name))
        return SimpleNamespace(ok=True, status=200, text="", json=lambda: {"reference": f"ref-{name}"})


def _ad():
    return insert.compose_ad(TEMPLATE, heading="Logitech S125i", description="Gut erhalten.\n\n- Netzteil\n- Dock",
                             price=12.0, path=[{"label": "Mobile Lautsprecher", "code": "MOBILE_SPEAKER"}],
                             attrs={"Zustand": ("gebraucht", "Gebraucht")}, attr_labels={"Zustand": "Zustand"})


async def test_full_publish_order_and_bodies():
    s, seen = FakeSession(), []
    res = await insert.publish(s, _ad(), [("01.jpg", b"x", "image/jpeg"), ("02.jpg", b"y", "image/jpeg")],
                               on_presaved=seen.append)
    steps = [(m, p) for m, p, *_ in s.calls]
    assert steps == [("POST", "/webapi/iad/bap/presave"), ("UPLOAD", "/webapi/iad-upload/atimage/temp"),
                     ("UPLOAD", "/webapi/iad-upload/atimage/temp"),
                     ("PUT", "/webapi/iad-upload/atz/123/images/order-and-associate"),
                     ("POST", "/webapi/iad/bap/save"), ("POST", "/webapi/iad/products/privates/atz/uuid-1/carts"),
                     ("PUT", "/webapi/iad/products/privates/carts/c9/items"),
                     ("POST", "/webapi/iad/products/privates/carts/c9/checkout")]
    presave_body = s.calls[0][2]
    assert presave_body["price"] == 12 and presave_body["heading"] == "Logitech S125i"
    assert presave_body["description"] == "<p>Gut erhalten.</p><p><br></p><ul><li>Netzteil</li><li>Dock</li></ul>"
    assert presave_body["categoryPath"] == {"path": [{"label": "Mobile Lautsprecher", "code": "MOBILE_SPEAKER"}]}
    assert {"attributeNode": {"label": "Übergabe", "code": "Uebergabe"},
            "selectedAttributeValueNodes": [{"label": "Selbstabholung", "code": "PICKUP"}]} in presave_body["treeAttributes"]
    assert "treeAttributeIds" not in presave_body
    assert s.calls[3][2] == {"advertImage": [{"reference": "ref-01.jpg"}, {"reference": "ref-02.jpg"}], "floorPlans": []}
    assert s.calls[4][2]["adId"] == 123 and s.calls[6][2] == {"productReferences": []}
    assert s.calls[7][2] == {"billingData": None}
    assert seen == ["123"] and res["ad_id"] == "123" and res["url"].endswith("adId=123")


@pytest.mark.parametrize("kw", [{"total_price": 4.9}, {"payment_url": "https://pay"}])
async def test_money_guard_stops_before_checkout(kw):
    s = FakeSession(**kw)
    with pytest.raises(insert.InsertError, match="nichts bezahlt"):
        await insert.publish(s, _ad(), [])
    assert not any(p.endswith("/checkout") for _, p, *_ in s.calls)


async def test_not_activated_is_an_error():
    with pytest.raises(insert.InsertError, match="nicht aktiviert"):
        await insert.publish(FakeSession(status="PAYMENT_REQUIRED"), _ad(), [])


async def test_retry_skips_presave():
    s = FakeSession()
    await insert.publish(s, _ad(), [], existing_ad_id="555")
    paths = [p for _, p, *_ in s.calls]
    assert paths[0] == "/webapi/iad/bap/555" and not any(p.endswith("/presave") for p in paths)
    assert s.calls[1][2]["adId"] == 555


@pytest.mark.parametrize("step,last", [("presave", "/presave"), ("photos", "/order-and-associate"),
                                       ("save", "/save"), ("cart", "/items")])
async def test_stop_after_never_reaches_checkout(step, last):
    s = FakeSession()
    res = await insert.publish(s, _ad(), [("01.jpg", b"x", "image/jpeg")], stop_after=step)
    assert res["stopped"] == step and s.calls[-1][1].endswith(last)
    assert not any(p.endswith("/checkout") for _, p, *_ in s.calls)
    assert res["trace"]["presave"]["links"] == ["imageList", "selfLinkSave"]
