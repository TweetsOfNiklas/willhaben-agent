import json

import pytest
from sqlmodel import select

from app.db import MyAd, session
from app.services import my_ads
from app.willhaben.account import AccountClient, parse_my_adverts
from app.willhaben.session import ApiResponse, NotLoggedIn


def _ad(ad_id, status, label, end):
    return {"adTypeId": 67, "id": ad_id, "uuid": "u", "description": f"Anzeige {ad_id}", "productId": 67,
            "startDate": "2026-10-09T12:47:24+02:00", "endDate": end,
            "advertStatus": {"description": label, "id": status, "rejectReason": None, "statusId": 50},
            "advertImageList": {"advertImage": [{"mainImageUrl": f"https://cache.willhaben.at/mmo/{ad_id}.jpg"}]},
            "attributes": {"attribute": [{"name": "PRICE_FOR_DISPLAY", "values": ["€ 50"]}]},
            "verticalId": 5}


# Shape of /iad/myprofile/myadverts __NEXT_DATA__ (values made up)
NEXT = {"page": "/iad/myprofile/myadverts", "buildId": "b", "props": {"pageProps": {
    "profileData": {"loginId": 1},
    "initialUserAdsList": {"totalNumberOfAdvertDetails": 2, "verticalList": [
        {"verticalId": 2, "verticalName": "Immobilien", "advertDetails": []},
        {"verticalId": 5, "verticalName": "Marktplatz", "advertDetails": [
            _ad("1000000001", "active", "Aktiv", "2026-11-23T12:47:24+01:00"),
            _ad("1000000002", "deactivated", "Deaktiviert", "2026-10-01T10:00:00+02:00")]}]}}}}


def test_parse_my_adverts():
    ads = parse_my_adverts(NEXT)
    assert [(a.ad_id, a.status, a.price_display) for a in ads] == [
        ("1000000001", "active", "€ 50"), ("1000000002", "deactivated", "€ 50")]
    assert ads[0].end.isoformat() == "2026-11-23T11:47:24+00:00"
    assert ads[0].url == "https://www.willhaben.at/iad/object?adId=1000000001"
    with pytest.raises(ValueError):
        parse_my_adverts({"props": {"pageProps": {}}})


class _FakeSession:
    def __init__(self, page):
        self.page = page

    async def api(self, method, path, body=None, accept="application/json"):
        assert (method, path, accept) == ("GET", "/iad/myprofile/myadverts", "text/html")
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(self.page)}</script>'
        return ApiResponse(status=200, text=html, url="https://www.willhaben.at" + path)


async def test_my_adverts_not_logged_in():
    login_page = {"page": "/iad/myprofile/login", "props": {"pageProps": {}}}
    with pytest.raises(NotLoggedIn):
        await AccountClient(_FakeSession(login_page)).my_adverts()


async def test_import_and_check_all_keep_account_status(monkeypatch):
    monkeypatch.setattr(my_ads, "get_session", lambda: _FakeSession(NEXT))

    async def no_public_check(ad_id):
        raise AssertionError("imported ads must not be re-checked via the public page")

    monkeypatch.setattr(my_ads, "check", no_public_check)
    res = await my_ads.check_all()
    assert res["imported"] == 2 and res["checked"] == 2 and res["reactivation"] == 0
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    assert rows["1000000001"].status == "active" and rows["1000000001"].heading == "Anzeige 1000000001"
    assert rows["1000000002"].status == "Deaktiviert"


def _links(ad, ids):
    ad["advertEditStatusActionsList"] = {"contextLinkList": {"contextLink": [
        {"id": i, "relativePath": f"/bap/{i}/{ad['id']}", "serviceName": "iad"} for i in ids]}}
    return ad


async def test_ended_passive_ad_is_renewed(monkeypatch):
    """willhaben lists a run-out ad as "Passiv" (only Neu veröffentlichen + Löschen), never as "expired"; an ad
    deactivated by hand is "Passiv" too but keeps Aktivieren and must stay offline."""
    page = json.loads(json.dumps(NEXT))
    page["props"]["pageProps"]["initialUserAdsList"]["verticalList"][1]["advertDetails"] = [
        _links(_ad("1000000003", "passive", "Passiv", "2026-10-09T21:53:08+02:00"), ["6", "3"]),
        _links(_ad("1000000004", "passive", "Passiv", "2026-11-01T10:00:00+01:00"), ["4", "6", "3"]),
        _links(_ad("1000000005", "passive", "Passiv", "2026-09-01T10:00:00+02:00"), ["6", "3"])]
    with session() as db:
        db.add(MyAd(ad_id="1000000003", status="Passiv", auto_renew=True))
        db.add(MyAd(ad_id="1000000004", status="Passiv", auto_renew=True))
        db.add(MyAd(ad_id="1000000005", status="replaced", replaced_by="1000000003"))
        db.commit()
    monkeypatch.setattr(my_ads, "get_session", lambda: _FakeSession(page))
    renewed = []

    async def reactivate(row):
        renewed.append(row.ad_id)

    monkeypatch.setattr(my_ads, "_reactivate", reactivate)
    res = await my_ads.check_all()
    assert renewed == ["1000000003"] and res["reactivation"] == 1
    with session() as db:
        assert db.get(MyAd, "1000000003").status == "expired"
        assert db.get(MyAd, "1000000004").status == "Passiv"
        assert db.get(MyAd, "1000000005").status == "replaced"   # the old copy of a renewed ad: never again


async def test_check_keeps_renewed_ads_old_copy_replaced(monkeypatch):
    async def get_detail(ad_id):
        return None     # the old copy of a renewed ad is gone from the public site

    monkeypatch.setattr(my_ads, "get_public", lambda: type("P", (), {"get_detail": staticmethod(get_detail)})())
    with session() as db:
        db.add(MyAd(ad_id="1000000006", status="expired", replaced_by="1000000007"))
        db.commit()
    row = await my_ads.check("1000000006")
    assert row.status == "replaced"     # not listed as expired next to its new copy


def test_current_ad_follows_renewals():
    from app.db import current_ad

    with session() as db:
        db.add(MyAd(ad_id="1000000010", status="replaced", replaced_by="1000000011", auto_reply=False))
        db.add(MyAd(ad_id="1000000011", status="replaced", replaced_by="1000000012"))
        db.add(MyAd(ad_id="1000000012", status="active", auto_reply=True, min_price=50))
        db.add(MyAd(ad_id="1000000013", status="replaced", replaced_by="1000000099"))   # new copy not imported yet
        db.commit()
        assert current_ad(db, "1000000010").ad_id == "1000000012"    # old chats get the newest settings
        assert current_ad(db, "1000000013").ad_id == "1000000013"
        assert current_ad(db, "404") is None


def _page(total):
    page = json.loads(json.dumps(NEXT))
    page["props"]["pageProps"]["initialUserAdsList"]["totalNumberOfAdvertDetails"] = total
    return page


def test_list_knows_whether_it_is_complete():
    assert parse_my_adverts(NEXT).complete
    assert not parse_my_adverts(_page(60)).complete            # more ads than the page lists
    assert not parse_my_adverts(_page(None)).complete          # unknown: never conclude "deleted"


@pytest.mark.parametrize("total, expected", [(2, "deleted"), (60, "sold")])
async def test_sold_ads_deleted_on_willhaben_leave_the_list(monkeypatch, total, expected):
    """Sold ads stay in willhaben's account list until deleted there; the public page never shows them."""
    monkeypatch.setattr(my_ads, "get_session", lambda: _FakeSession(_page(total)))
    with session() as db:
        db.add(MyAd(ad_id="9", heading="Verkauft und gelöscht", status="sold"))
        db.add(MyAd(ad_id="8", heading="Verkauft (App)", status="active", sale_status="sold"))
        db.add(MyAd(ad_id="7", heading="Alt", status="replaced", replaced_by="1000000001"))
        db.commit()

    async def no_public_check(ad_id, off_account=False):
        raise AssertionError(f"{ad_id}: sold and renewed ads need no public lookup")

    monkeypatch.setattr(my_ads, "check", no_public_check)
    res = await my_ads.check_all()
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    assert rows["9"].status == expected and rows["7"].status == "replaced"
    assert rows["8"].status == ("deleted" if expected == "deleted" else "active")
    assert res["deleted"] == (2 if expected == "deleted" else 0)


async def test_ad_missing_from_complete_list_and_public_page_is_deleted(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(my_ads, "get_session", lambda: _FakeSession(NEXT))
    with session() as db:
        db.add(MyAd(ad_id="5", heading="Woanders gelöscht", status="active"))
        db.commit()
    looked_up = []

    class Public:
        async def get_detail(self, ad_id):
            looked_up.append(ad_id)
            return None

    monkeypatch.setattr(my_ads, "get_public", lambda: Public())
    assert (await my_ads.check_all())["deleted"] == 1
    assert (await my_ads.check_all())["deleted"] == 0 and looked_up == ["5"]    # looked up once, then left alone
    with TestClient(app) as c:
        page = c.get("/myads?show_active=1&show_inactive=1&show_sold=1").text
    assert "Woanders gelöscht" not in page and "Anzeige 1000000001" in page
