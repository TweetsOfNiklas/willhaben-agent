"""Photos of an own ad: reorder, cover, remove, add - one order-and-associate call like willhaben's edit page."""
import pytest
from fastapi.testclient import TestClient

from app.db import MyAd, session
from app.services import my_ads
from app.willhaben.session import ApiResponse
from tests.test_myads_actions import _Editor
from tests.conftest import wait_jobs  # noqa: E402

LIST = "/webapi/iad/atimage/1000000001"
ASSOC = "/webapi/iad-upload/atimage/1000000001/order-and-associate"


def _img(ref):
    return {"id": ref, "reference": f"1/100/000/0001_{ref}.jpg",
            "thumbnailImageUrl": f"https://cache.willhaben.at/mmo/1/100/000/0001_{ref}_thumb.jpg"}


class _PhotoEditor(_Editor):
    """_Editor plus the imageList link, atimage/temp uploads and order-and-associate."""

    def __init__(self, applies=True):
        super().__init__(applies)
        self.ad["contextLinkList"] = {"contextLink": [
            {"id": "imageList", "serviceName": "iad", "relativePath": "/atimage/1000000001"}]}
        self.images = [_img("a"), _img("b"), _img("c")]
        self.assoc_body, self.uploads = None, []

    async def api_json(self, method, path, body=None):
        if path == LIST and method == "GET":
            self.calls.append((method, path))
            return {"advertImage": [dict(i) for i in self.images], "floorPlans": []}
        if path == ASSOC and method == "PUT":
            self.calls.append((method, path))
            self.assoc_body = body
            if self.applies:
                by_ref = {i["reference"]: i for i in self.images}
                self.images = [by_ref.get(r["reference"]) or {"id": "n", "reference": "1/new/" + r["reference"]}
                               for r in body["advertImage"]]
            return {"advertImage": [dict(i) for i in self.images], "floorPlans": []}
        return await super().api_json(method, path, body)

    async def upload(self, path, field, filename, data, mime):
        self.calls.append(("POST", path))
        self.uploads.append((path, filename, mime))
        return ApiResponse(status=200, text=f'{{"reference": "temp-{len(self.uploads)}"}}', url=path)


@pytest.fixture
def editor(monkeypatch):
    acct = _PhotoEditor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    return acct


def ref(x):
    return f"1/100/000/0001_{x}.jpg"


async def test_load_lists_photos_with_thumbs(editor):
    cur = await my_ads.load_for_edit("1000000001")
    assert [i["reference"] for i in cur["images"]] == [ref("a"), ref("b"), ref("c")]
    assert cur["images"][0]["thumb"].endswith("0001_a_thumb.jpg") and cur["images_error"] is None


async def test_reorder_remove_and_add(editor):
    await my_ads.import_from_account()
    row = await my_ads.set_photos("1000000001", ["new:0", ref("c"), ref("a")], [("neu.JPG", b"x")])
    assert editor.uploads == [("/webapi/iad-upload/atimage/temp", "neu.JPG", "image/jpeg")]
    assert editor.assoc_body == {"advertImage": [{"reference": "temp-1"}, {"reference": ref("c")},
                                                 {"reference": ref("a")}], "floorPlans": []}
    assert "photos changed" in row.last_action
    assert not any("cart" in p or "checkout" in p or p == "/webapi/iad/bap/edit" for _, p in editor.calls)


async def test_unchanged_order_sends_nothing(editor):
    from app.willhaben import insert
    await insert.set_own_images(editor, "1000000001", [ref("a"), ref("b"), ref("c")], [])
    assert editor.assoc_body is None and not editor.uploads


@pytest.mark.parametrize("order, match", [
    (["1/999/foreign.jpg"], "gehört nicht"),
    ([], "mindestens ein Foto"),
    ([ref("a"), ref("a")], "doppelt"),
    (["new:3"], "unbekanntes"),
])
async def test_invalid_orders_are_refused(editor, order, match):
    from app.willhaben import insert
    with pytest.raises(insert.InsertError, match=match):
        await insert.set_own_images(editor, "1000000001", order, [])
    assert editor.assoc_body is None


async def test_result_mismatch_is_reported(monkeypatch):
    from app.willhaben import insert
    acct = _PhotoEditor(applies=False)
    with pytest.raises(insert.InsertError, match="nicht wie gewünscht"):
        await insert.set_own_images(acct, "1000000001", [ref("b"), ref("a")], [])


async def test_unsupported_format(editor):
    from app.willhaben import insert
    with pytest.raises(insert.InsertError):
        await my_ads.set_photos("1000000001", [ref("a"), "new:0"], [("scan.pdf", b"%PDF")])
    assert not editor.uploads


def test_page_and_route(editor):
    from app.main import app
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Alter Titel", url="https://www.willhaben.at/iad/object?adId=1000000001"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/myads/1000000001/edit").text
        assert 'id="photo-form"' in page and f'value="{ref("b")}"' in page and "0001_b_thumb.jpg" in page
        r = c.post("/myads/1000000001/photos", data={"order": [ref("b"), ref("a")], "new_first": "1"},
                   files=[("photos", ("neu.png", b"x", "image/png"))], follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/myads?ad=1000000001&msg=")
        wait_jobs()                                                          # uploaded by the background job
        assert [x["reference"] for x in editor.assoc_body["advertImage"]] == ["temp-1", ref("b"), ref("a")]
        r = c.post("/myads/1000000001/photos", data={"order": ["1/999/foreign.jpg"]}, follow_redirects=False)
        assert r.status_code == 303
        wait_jobs()
        with session() as db:                                                # the reason lands on the ad
            err = db.get(MyAd, "1000000001").last_error
        assert "failed" in err or "fehlgeschlagen" in err
