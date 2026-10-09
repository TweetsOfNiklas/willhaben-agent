"""Deactivate / delete own ads on willhaben via the ad's own action links, and hide-only."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import MyAd, session
from app.services import my_ads
from app.willhaben.account import EndpointUnknown
from app.willhaben.session import ApiResponse
from tests.test_my_ads import NEXT


def _links(ad_id):
    return {"contextLink": [
        {"id": "2", "description": "Deaktivieren", "relativePath": f"/bap/deactivate/{ad_id}", "serviceName": "myatz"},
        {"id": "3", "description": "Löschen", "relativePath": f"/bap/{ad_id}", "serviceName": "myatz"}]}


class _Account:
    """Stateful fake of the logged-in browser: the list page plus willhaben's PUT/DELETE action links."""

    def __init__(self, delete_works=True):
        self.page = copy.deepcopy(NEXT)
        self.calls, self.delete_works = [], delete_works
        for ad in self._ads():
            self._relink(ad)

    @staticmethod
    def _relink(ad):
        if ad["advertStatus"]["id"] == "active":
            ad["advertEditStatusActionsList"] = {"contextLinkList": _links(ad["id"])}
        else:   # deactivated: willhaben offers "Aktivieren" (and delete)
            ad["advertEditStatusActionsList"] = {"contextLinkList": {"contextLink": [
                {"id": "4", "description": "Aktivieren", "relativePath": f"/bap/activate/{ad['id']}", "serviceName": "myatz"},
                {"id": "3", "description": "Löschen", "relativePath": f"/bap/{ad['id']}", "serviceName": "myatz"}]}}

    def _ads(self):
        return self.page["props"]["pageProps"]["initialUserAdsList"]["verticalList"][1]["advertDetails"]

    async def api(self, method, path, body=None, accept="application/json"):
        self.calls.append((method, path))
        if method == "GET":
            html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(self.page)}</script>'
            return ApiResponse(status=200, text=html, url="https://www.willhaben.at" + path)
        ad_id = path.rsplit("/", 1)[1]
        ads = self._ads()
        if method == "PUT" and path == f"/webapi/myatz/bap/deactivate/{ad_id}":
            for ad in ads:
                if ad["id"] == ad_id:
                    ad["advertStatus"] = {"id": "deactivated", "description": "Deaktiviert"}
                    self._relink(ad)
            return ApiResponse(status=200, text="", url=path)
        if method == "PUT" and path == f"/webapi/myatz/bap/activate/{ad_id}":
            for ad in ads:
                if ad["id"] == ad_id:
                    ad["advertStatus"] = {"id": "active", "description": "Aktiv"}
                    self._relink(ad)
            return ApiResponse(status=200, text="", url=path)
        if method == "DELETE" and path == f"/webapi/myatz/bap/{ad_id}":
            if self.delete_works:
                ads[:] = [a for a in ads if a["id"] != ad_id]
            return ApiResponse(status=204, text="", url=path)
        return ApiResponse(status=404, text="unexpected", url=path)


@pytest.fixture
def account(monkeypatch):
    acct = _Account()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    return acct


async def test_deactivate_on_willhaben(account):
    await my_ads.import_from_account()
    my_ads.set_auto_renew("1000000001", True)
    row = await my_ads.deactivate("1000000001")
    assert ("PUT", "/webapi/myatz/bap/deactivate/1000000001") in account.calls
    assert row.status == "Deaktiviert" and row.auto_renew is False and "deactivated" in row.last_action


async def test_delete_on_willhaben_removes_row(account):
    await my_ads.import_from_account()
    my_ads.set_auto_renew("1000000001", True)
    await my_ads.delete("1000000001")
    assert ("DELETE", "/webapi/myatz/bap/1000000001") in account.calls
    with session() as db:
        assert db.get(MyAd, "1000000001") is None                   # never seen as "expired" by auto-renew
    await my_ads.import_from_account()
    with session() as db:
        assert db.get(MyAd, "1000000001") is None                   # and not re-imported


async def test_delete_not_confirmed_keeps_row(monkeypatch):
    acct = _Account(delete_works=False)
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    await my_ads.import_from_account()
    with pytest.raises(EndpointUnknown, match="still lists"):
        await my_ads.delete("1000000001")
    with session() as db:
        assert db.get(MyAd, "1000000001") is not None


async def test_no_link_no_action(account):
    await my_ads.import_from_account()
    with pytest.raises(EndpointUnknown, match="offers no"):
        await my_ads.deactivate("1000000002")                        # already deactivated: no such link
    assert not any(m in ("PUT", "DELETE") for m, _ in account.calls)


async def test_bulk_deactivate_activate_delete(account):
    await my_ads.import_from_account()
    my_ads.set_auto_renew("1000000001", True)
    res = await my_ads.bulk("deactivate", ["1000000001", "1000000002"])
    assert res["done"] == ["Anzeige 1000000001"] and res["failed"][0][0] == "Anzeige 1000000002"
    with session() as db:
        assert db.get(MyAd, "1000000001").status == "Deaktiviert" and db.get(MyAd, "1000000001").auto_renew is False
    res = await my_ads.bulk("activate", ["1000000001", "1000000002"])
    assert sorted(res["done"]) == ["Anzeige 1000000001", "Anzeige 1000000002"] and not res["failed"]
    with session() as db:
        assert db.get(MyAd, "1000000002").status == "active"
    lists_before = sum(1 for m, p in account.calls if m == "GET")
    res = await my_ads.bulk("delete", ["1000000001", "1000000002"])
    assert len(res["done"]) == 2 and not res["failed"]
    assert sum(1 for m, p in account.calls if m == "GET") - lists_before == 2   # one list before, one to confirm
    with session() as db:
        assert db.exec(select(MyAd)).first() is None


async def test_bulk_route(account):
    from app.main import app

    await my_ads.import_from_account()
    with TestClient(app) as c:
        r = c.post("/myads/bulk/delete", data={"ids": ["1000000001"]}, follow_redirects=False)
        assert r.status_code == 303 and "1" in r.headers["location"]
        assert c.post("/myads/bulk/nuke", data={"ids": ["1"]}).status_code == 404
        page = c.get("/myads?show_inactive=1").text
        assert "btn-danger" in page and 'name="ids"' in page and "Activate on willhaben" in page


async def test_hide_only_survives_import_and_routes(account):
    from app.main import app

    await my_ads.import_from_account()
    with TestClient(app) as c:
        assert c.post("/myads/1000000001/hide", data={"hidden": "1"}, follow_redirects=False).status_code == 303
        page = c.get("/myads?show_inactive=1").text
        assert "Anzeige 1000000001" not in page and "Show 1 hidden ads" in page
        assert "Anzeige 1000000001" in c.get("/myads?show_hidden=1&show_inactive=1").text
        assert "Delete on willhaben" in page and "Activate on willhaben" in page   # the visible ad is deactivated
    await my_ads.import_from_account()
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    assert rows["1000000001"].hidden is True                          # the import keeps it hidden
    assert not any(m in ("PUT", "DELETE") for m, _ in account.calls)  # hiding never touches willhaben


async def test_bulk_stops_at_logout(account, monkeypatch):
    from app.willhaben.session import NotLoggedIn

    await my_ads.import_from_account()
    real = account.api

    async def api(method, path, body=None, accept="application/json"):
        if method == "DELETE":
            raise NotLoggedIn("HTTP 401")
        return await real(method, path, body, accept)

    monkeypatch.setattr(account, "api", api)
    res = await my_ads.bulk("delete", ["1000000001", "1000000002"])
    assert res["done"] == [] and "401" in res["failed"][0][1] and "not done" in res["failed"][1][1]
    with session() as db:
        assert len(db.exec(select(MyAd)).all()) == 2


async def test_delete_unlinks_draft_and_publish_restarts_after_409(account, monkeypatch):
    from app.db import AdDraft
    from app.services import ad_creator
    from app.willhaben import insert

    await my_ads.import_from_account()
    plan = {"options": [{"path": [{"label": "X", "code": "X"}], "label": "X", "fields": []}]}
    with session() as db:
        db.add(AdDraft(id=50, input_text="", title="Testartikel", description="Text", image_paths=[],
                       wh_plan=plan, wh_ad_id="1000000001"))
        db.add(AdDraft(id=51, input_text="", title="Zweiter", description="Text", image_paths=[],
                       wh_plan=plan, wh_ad_id="999"))       # points to an ad that was deleted elsewhere
        db.commit()
    await my_ads.delete("1000000001")
    with session() as db:
        assert db.get(AdDraft, 50).wh_ad_id is None            # unlinked: next publish starts fresh

    calls = []

    async def fake_publish(sess, ad, photos, existing_ad_id=None, on_presaved=None, stop_after=None, **_kw):
        calls.append(existing_ad_id)
        if existing_ad_id:
            raise RuntimeError("POST /webapi/iad/products/privates/atz/u/carts: HTTP 409 ")
        return {"ad_id": "2000000001", "url": "https://www.willhaben.at/iad/object?adId=2000000001"}

    async def fake_form(sess):
        return {"ad": {}}

    monkeypatch.setattr(insert, "publish", fake_publish)
    monkeypatch.setattr(insert, "load_form", fake_form)
    monkeypatch.setattr(insert, "compose_ad", lambda *a, **k: {})
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: account)
    await ad_creator.publish_willhaben(51, 0, {})
    assert calls == ["999", None]                               # stale willhaben draft -> one fresh insertion
    with session() as db:
        d = db.get(AdDraft, 51)
    assert d.status == "published" and d.wh_ad_id == "2000000001"


async def test_publish_409_never_duplicates_a_live_ad(account, monkeypatch):
    from app.db import AdDraft
    from app.services import ad_creator
    from app.willhaben import insert

    plan = {"options": [{"path": [{"label": "X", "code": "X"}], "label": "X", "fields": []}]}
    with session() as db:   # its willhaben ad 1000000001 is still active in the account list
        db.add(AdDraft(id=52, input_text="", title="Live", description="Text", image_paths=[], wh_plan=plan,
                       wh_ad_id="1000000001"))
        db.commit()
    calls = []

    async def fake_publish(sess, ad, photos, existing_ad_id=None, on_presaved=None, stop_after=None, **_kw):
        calls.append(existing_ad_id)
        raise RuntimeError("POST /webapi/iad/products/privates/atz/u/carts: HTTP 409 ")

    async def fake_form(sess):
        return {"ad": {}}

    monkeypatch.setattr(insert, "publish", fake_publish)
    monkeypatch.setattr(insert, "load_form", fake_form)
    monkeypatch.setattr(insert, "compose_ad", lambda *a, **k: {})
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: account)
    with pytest.raises(insert.InsertError, match="schon als 1000000001"):
        await ad_creator.publish_willhaben(52, 0, {})
    assert calls == ["1000000001"]                              # no second, fresh insertion
    with session() as db:
        assert db.get(AdDraft, 52).status == "published"


class _Editor(_Account):
    """Fake willhaben editor: GET /webapi/iad/bap/<id> returns the full ad, PUT bap/edit replaces it."""

    def __init__(self, applies=True):
        super().__init__()
        self.applies, self.put_body = applies, None
        self.ad = {"adId": 1000000001, "heading": "Alter Titel", "price": 40, "categoryPath": {"path": ["X"]},
                   "description": "<p>Zeile eins<br>Zeile zwei</p><ul><li>Netzteil</li><li>Tasche</li></ul>",
                   "advertImageList": {"advertImage": [{"id": 1}]}, "contextLinkList": {"contextLink": []}}

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/webapi/iad/bap/1000000001":
            return dict(self.ad)
        if method == "PUT" and path == "/webapi/iad/bap/edit":
            self.put_body = body
            if self.applies:
                self.ad = dict(body)
            return body
        raise AssertionError((method, path))


def test_description_text_roundtrip():
    from app.willhaben import insert
    text = "Zeile eins\nZeile zwei\n\n• Netzteil\n• Tasche"
    assert insert.description_text(insert.description_html(text)) == text
    assert insert.description_text("<p>A &amp; B</p>") == "A & B"


async def test_edit_changes_only_three_fields(monkeypatch):
    acct = _Editor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    await my_ads.import_from_account()
    cur = await my_ads.load_for_edit("1000000001")
    assert {k: cur[k] for k in ("heading", "description", "price")} == {
        "heading": "Alter Titel", "description": "Zeile eins\nZeile zwei\n\n• Netzteil\n• Tasche", "price": 40}
    assert cur["images"] == [] and cur["images_error"]        # no imageList link: text editing still works
    row = await my_ads.edit("1000000001", "Neuer Titel", "Neu\n\n• Netzteil", 35.0)
    body = acct.put_body
    assert body["heading"] == "Neuer Titel" and body["price"] == 35 and body["description"] == "<p>Neu</p><ul><li>Netzteil</li></ul>"
    assert body["advertImageList"] == {"advertImage": [{"id": 1}]} and body["categoryPath"] == {"path": ["X"]}   # full object
    assert "edited" in row.last_action                       # heading comes back from the account list
    assert not any("cart" in p or "checkout" in p for _, p in acct.calls)


async def test_edit_not_applied_is_reported(monkeypatch):
    from app.willhaben.insert import InsertError
    acct = _Editor(applies=False)
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    with pytest.raises(InsertError, match="nicht übernommen"):
        await my_ads.edit("1000000001", "Neuer Titel", "Text", 10.0)


def test_edit_page_and_route(monkeypatch):
    from app.main import app

    acct = _Editor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Alter Titel", url="https://www.willhaben.at/iad/object?adId=1000000001"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/myads/1000000001/edit").text
        assert 'value="Alter Titel"' in page and "• Netzteil" in page and 'value="40"' in page
        r = c.post("/myads/1000000001/edit", data={"heading": "Neu", "description": "Text", "price": "12,50"},
                   follow_redirects=False)
        assert r.status_code == 303 and "/myads?msg=" in r.headers["location"]
        assert acct.put_body["price"] == 12.5
        r = c.post("/myads/1000000001/edit", data={"heading": "Neu", "description": "Text", "price": "abc"},
                   follow_redirects=False)
        assert "/edit?msg=" in r.headers["location"]


def test_ai_revision_fills_the_form_without_saving(monkeypatch):
    from app.llm.base import LLMResult
    from app.main import app

    acct = _Editor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)

    class Detail:
        images, category_path = [], ["Audio", "Lautsprecher"]

    async def fake_detail(ad_id):
        return Detail()

    class Backend:
        prompts = []

        async def complete(self, *, system, prompt, schema, images=None):
            Backend.prompts.append(prompt)
            return LLMResult(data={"title": "Kürzerer Titel", "description": "Neuer Text.", "price_estimate": 30,
                                   "note": "Titel gekürzt."}, model="fake")

    monkeypatch.setattr(my_ads, "get_public", lambda: type("P", (), {"get_detail": staticmethod(fake_detail)})())
    monkeypatch.setattr("app.llm.base.get_backend", lambda *a: Backend())
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Alter Titel", url="https://www.willhaben.at/iad/object?adId=1000000001"))
        db.commit()
    with TestClient(app) as c:
        page = c.post("/myads/1000000001/edit/ai", data={"heading": "Alter Titel", "description": "Alt", "price": "40",
                                                         "instruction": "kürzer"}).text
    assert 'value="Kürzerer Titel"' in page and "Neuer Text." in page and "Titel gekürzt." in page
    assert 'value="40"' in page and "Claude estimates" in page                   # price stays, estimate offered
    assert "kürzer" in Backend.prompts[0] and "Audio > Lautsprecher" in Backend.prompts[0]
    assert acct.put_body is None                                                 # nothing saved on willhaben


def test_myads_filters_and_order():
    from datetime import UTC, datetime, timedelta

    from app.db import MyAd, session
    from app.main import app
    from fastapi.testclient import TestClient
    now = datetime(2026, 10, 1, tzinfo=UTC)
    with session() as db:
        db.add(MyAd(ad_id="1", heading="Bald", status="active", end_date=now + timedelta(days=2)))
        db.add(MyAd(ad_id="2", heading="Lange", status="active", end_date=now + timedelta(days=40)))
        db.add(MyAd(ad_id="3", heading="Deaktiviert", status="Passiv", end_date=now))
        db.add(MyAd(ad_id="4", heading="Weg", status="sold", end_date=now))
        db.add(MyAd(ad_id="5", heading="Geschenkt", status="Verschenkt", end_date=now))
        db.add(MyAd(ad_id="6", heading="Reserviert", status="active", sale_status="sold", end_date=now))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/myads").text
        assert page.index("Lange") < page.index("Bald")       # most days left first
        for gone in ("Deaktiviert", "Weg", "Geschenkt", "Reserviert"):
            assert gone not in page
        assert "Show inactive ads (1)" in page and "Show sold ads (3)" in page
        page = c.get("/myads?show_inactive=1").text
        assert "Deaktiviert" in page and "Weg" not in page
        page = c.get("/myads?show_sold=1").text
        assert "Weg" in page and "Geschenkt" in page and "Deaktiviert" not in page
