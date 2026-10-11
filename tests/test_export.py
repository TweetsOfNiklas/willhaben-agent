"""Recordings -> redacted fixture files -> a fake willhaben that serves them to the real clients."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.services import timeline
from app.willhaben import export, recorder, redact
from app.willhaben.account import CHAT, AccountClient
from app.willhaben.parse import extract_next_data
from app.willhaben.public import PublicClient, RateLimiter
from app.willhaben.session import ME_PATH, ORIGIN
from tests.conftest import load
from tests.fake_willhaben import Cassette, FakeSession, transport_from_cassette

SEARCH = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=fahrrad&rows=3"
CONV = "3f2a8c1e-5b7d-4e9f-a1b2-c3d4e5f60718"
SECRETS = ("Maximilian Huber", "max.huber@example.com", "Ist das Rad noch zu haben", "0664 1234567", CONV,
           "AT61 1904 3002 3457 3201", "Hauptstraße 5")


@pytest.fixture(autouse=True)
def recording_on():
    timeline.set_recording(timeline=True, willhaben=True)


def _seed():
    recorder.record_http("anonymous", "search", "GET", SEARCH, status=200, resp_body=json.dumps(load("search_fahrrad_rows3.json")),
                         resp_headers={"content-type": "application/json;charset=UTF-8", "set-cookie": "bbxid=1"})
    nd = load("detail_922008415_nextdata.json")
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></body></html>'
    recorder.record_http("anonymous", "detail", "GET", "https://www.willhaben.at/iad/object?adId=922008415", status=200,
                         resp_body=html, resp_headers={"content-type": "text/html"})
    recorder.record_http("account", "login", "GET", ORIGIN + ME_PATH, status=200, resp_body=json.dumps(
        {"userData": {"loginId": 4242424, "uuid": "9a8b7c6d-1111-4222-8333-444455556666", "firstName": "Maximilian",
                      "lastName": "Huber", "emailAddress": "max.huber@example.com"}}))
    conversations = {"conversations": [{"id": CONV, "adId": 922008415, "adTitle": "Kinderrad", "partnerName": "Maximilian Huber",
                                        "unreadCount": 1, "lastMessage": {"text": "Ist das Rad noch zu haben? Ruf mich an: 0664 1234567",
                                                                          "createdAt": "2026-10-09T10:00:00+0200"}}]}
    recorder.record_http("account", "chat", "GET", ORIGIN + CHAT + "/conversations?limit=50&offset=0", status=200,
                         resp_body=json.dumps(conversations))
    recorder.record_ws("send", "wss://chat-realtime-web.willhaben.at/webapi/chat-ws/v1/ws/",
                       {"req_type": "send_message", "request_id": "r1", "conversation_id": CONV,
                        "text": "Gerne, IBAN AT61 1904 3002 3457 3201, Abholung Hauptstraße 5"},
                       {"status": "success", "payload": {"message_id": "m-77"}}, outcome="ok")
    recorder.record_http("download", "other", "GET", "https://cache.willhaben.at/mmo/1/a.jpg", status=200,
                         resp_body=b"\xff\xd8\xff", resp_headers={"content-type": "image/jpeg"})


def test_export_is_redacted_and_complete(tmp_path):
    _seed()
    recorder.record_http("anonymous", "search", "GET", SEARCH + "&_=1", status=200, resp_body='{"newer": true}')   # same request
    files = export.export_recordings(tmp_path)
    kinds = sorted(p.parent.name for p in files)
    assert kinds == ["chat", "detail", "login", "search", "send"]           # no image, one search (the newest)
    text = "\n".join(p.read_text(encoding="utf-8") for p in files)
    for secret in SECRETS:
        assert secret not in text, secret
    assert "set-cookie" not in text and "bbxid" not in text
    detail = json.loads(next(p for p in files if p.parent.name == "detail").read_text(encoding="utf-8"))
    nd = extract_next_data(detail["response"]["body"])
    assert nd["props"]["pageProps"]                                          # the page still parses
    search = json.loads(next(p for p in files if p.parent.name == "search").read_text(encoding="utf-8"))
    assert search["response"]["body"] == {"newer": True}                     # the newest version of a request wins


def test_fake_willhaben_serves_the_export(tmp_path):
    _seed()
    export.export_recordings(tmp_path)
    cassette = Cassette.load(tmp_path)
    client = PublicClient(transport=transport_from_cassette(cassette))
    client.limiter = RateLimiter(0)

    async def go():
        page = await client.search(SEARCH)
        detail = await client.get_detail("922008415")
        sess = FakeSession(cassette)
        assert await sess.check_login() and sess.user["email"] == "erika@example.org"
        convs = await AccountClient(sess).conversations()
        frame = json.loads(next(tmp_path.glob("send/*.json")).read_text(encoding="utf-8"))["request"]["body"]
        assert frame["conversation_id"] == convs[0].id and frame["text"].startswith("Nachricht")
        sent = await sess.ws_request("wss://chat-realtime-web.willhaben.at/webapi/chat-ws/v1/ws/",
                                     {**frame, "request_id": "other"}, "other", req_type="send_message")
        return page, detail, convs, sent

    import asyncio
    page, detail, convs, sent = asyncio.run(go())
    assert page.rows_found and page.ads and detail is not None
    assert convs[0].partner_name == "Erika M." and convs[0].id != CONV and "Nachricht" in (convs[0].last_text or "")
    assert sent["status"] == "success"
    assert cassette.misses == []

    async def miss():
        return await client._get("https://www.willhaben.at/iad/object?adId=1", json_api=False)
    assert asyncio.run(miss()).status_code == 599 and cassette.misses


def test_keep_texts_and_raw(tmp_path):
    _seed()
    kept = export.export_recordings(tmp_path / "k", kinds={"chat"}, keep_texts=True)
    text = kept[0].read_text(encoding="utf-8")
    assert "Ist das Rad noch zu haben" in text and "Maximilian Huber" not in text and "0664 1234567" not in text
    raw = export.export_recordings(tmp_path / "r", kinds={"chat"}, raw=True)
    assert "Maximilian Huber" in raw[0].read_text(encoding="utf-8")


def test_cli_and_settings_zip(tmp_path, capsys):
    _seed()
    assert export.main(["--out", str(tmp_path), "--kinds", "search,detail", "--since", "1d"]) == 0
    assert "2 files written" in capsys.readouterr().out
    with pytest.raises(ValueError):
        export.parse_since("soon")
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/settings/timeline/recordings/export", data={"since": "7d", "kinds": "chat,login"})
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(n.split("/")[0] for n in names) == ["chat", "login"]


def test_free_text_rules():
    ctx = redact.RedactContext()
    out = redact.redact_text("Schreib an anna.berger@gmx.at oder +43 660 1234567, IBAN AT611904300234573201. "
                             "Preis 0,65 €, am 2026-06-12 um 06:15", ctx)
    assert "anna.berger" not in out and "1234567" not in out and "AT6119" not in out
    assert "0,65 €" in out and "2026-06-12" in out and "06:15" in out


def test_redact_fixture_script_still_works(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    f = tmp_path / "x.json"
    f.write_text(json.dumps({"sellerProfileUserData": {"name": "Hans Gruber"}, "note": "Hans Gruber",
                             "uuid": "9a8b7c6d-1111-4222-8333-444455556666"}), encoding="utf-8")
    script = Path(__file__).parent / "redact_fixture.py"
    subprocess.run([sys.executable, str(script), str(f)], check=True, capture_output=True)
    text = f.read_text(encoding="utf-8")
    assert "Hans Gruber" not in text and "9a8b7c6d-1111" not in text
