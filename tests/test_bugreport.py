"""Bug report: anonymiser (every category, stable placeholders, no over-redaction), log ring buffer,
unhandled route errors, page + downloads. No network anywhere."""
import json
import logging
import socket

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import bugreport, logbuffer
from app.services.bugreport import Anonymizer

CONV = "3f2a8c1e-5b7d-4e9f-a1b2-c3d4e5f60718"
USER_UUID = "9a8b7c6d-1111-4222-8333-444455556666"
OTHER_UUID = "0b1c2d3e-aaaa-4bbb-8ccc-dddddddddddd"
SK = "sk-ant-oat01-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcdef"
TG = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMDAwIn0.c2lnbmF0dXJlX19fX19fX19fX18"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access in a bug report test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def anon(**kw):
    return Anonymizer(**kw)


# ------------------------------------------------------------------ secrets
@pytest.mark.parametrize("raw, secret", [
    (f"claude failed with token {SK} oops", SK),
    (f"GET https://api.telegram.org/bot{TG}/getUpdates", TG),
    (f"token {TG} rejected", TG),
    (f"jwt was {JWT}", JWT),
    ("Authorization: Bearer abcdEFGH12345678xyz", "abcdEFGH12345678xyz"),
    ("headers {'authorization': 'Basic YWRtaW46c2VjcmV0'}", "YWRtaW46c2VjcmV0"),
    ("Cookie: x-bbx-csrf-token=abc123def; __cf_bm=qwertz987", "abc123def"),
    ("set-cookie: aws-waf-token=zzz999yyy; Path=/", "zzz999yyy"),
    ("login with password=hunter2222 failed", "hunter2222"),
    ('{"access_token": "ya29.a0AfH6SMBx", "x": 1}', "ya29.a0AfH6SMBx"),
    ("refresh_token=1//0gAbCdEf failed", "1//0gAbCdEf"),
    ("signature mismatch: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"),
    ("blob Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1O end", "Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1O"),
])
def test_token_shapes_removed(raw, secret):
    out = anon()(raw)
    assert secret not in out, out


def test_known_secret_values_removed_even_without_token_shape():
    a = anon(secrets=["my-dashboard-pw", "vnc-pass-77", "abc"])
    out = a("auth failed for my-dashboard-pw and vnc-pass-77; abc words stay")
    assert "my-dashboard-pw" not in out and "vnc-pass-77" not in out
    assert "abc words stay" in out                    # values under 4 characters are not used (over-redaction)


def test_short_known_secrets_as_whole_words(monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "abc12")
    a = bugreport.build_anonymizer()
    assert a("dashboard pw abc12 used") == "dashboard pw <secret> used"
    assert a("xabc12y stays") == "xabc12y stays"      # short secrets only as whole words


def test_carrier_logins_and_keyed_json_values_are_secrets():
    d = get_settings().data_dir
    (d / "carrier-logins.json").write_text(json.dumps({"post": {"user": "mmuster7", "password": "Pw.4711"},
                                                       "gls": {"user": "K12345", "password": "kurz9"}}))
    (d / "other-login.json").write_text(json.dumps({"login": {"user_name": "maxm", "client_pin": "a1b2"},
                                                    "label": "harmless"}))
    try:
        a = bugreport.build_anonymizer()
        out = a("login mmuster7 / Pw.4711 failed; gls K12345 kurz9; other maxm a1b2; label harmless")
        for leak in ("mmuster7", "Pw.4711", "K12345", "kurz9", "maxm", "a1b2"):
            assert leak not in out, out
        assert "harmless" in out
    finally:
        (d / "carrier-logins.json").unlink(missing_ok=True)
        (d / "other-login.json").unlink(missing_ok=True)


@pytest.mark.parametrize("raw, leaks", [
    ("{'cookie': 'bbxsess=abc123def; x-bbx-csrf=zz9; IADVISITOR=a1b2c3d4e5f6'}", ["abc123def", "zz9", "a1b2c3d4e5f6"]),
    ('headers {"Cookie": "IADVISITOR=a1b2c3d4e5f6; other=qq77"}', ["a1b2c3d4e5f6", "qq77"]),
    ("cookies [{'name': 'IADVISITOR', 'value': '8f3k2l9q0w1e2r3t4y5u', 'domain': '.willhaben.at'}]",
     ["8f3k2l9q0w1e2r3t4y5u"]),
    ("password: correct horse battery", ["correct", "horse", "battery"]),
    ('{"password": "correct horse battery", "x": 1}', ["correct", "horse", "battery"]),
    ("x-bbx-session: 0123456789abcdef0123", ["0123456789", "abcdef0123"]),
    ("sessionid=s3ss10nvalu3 ok", ["s3ss10nvalu3"]),
])
def test_cookie_dicts_and_password_values(raw, leaks):
    out = anon()(raw)
    for leak in leaks:
        assert leak not in out, out
    assert "<redacted>" in out


def test_secrets_from_token_files_are_redacted(monkeypatch):
    d = get_settings().data_dir
    files = {"telegram-token": "987654:telegram-file-secret-value-xyz", "action-secret": "action-hmac-secret-123456",
             "claude-token": "plain-claude-token-value-0001", "ha-token": "ha-long-lived-token-value-42"}
    for name, value in files.items():
        (d / name).write_text(value)
    (d / "google-calendar.json").write_text(json.dumps({"client_secret": "GOCSPX-gcal-client-secret",
                                                         "refresh_token": "1//refresh-token-from-google"}))
    monkeypatch.setenv("VNC_PASSWORD", "vnc-env-password")
    try:
        a = bugreport.build_anonymizer()
        text = " | ".join(list(files.values()) + ["GOCSPX-gcal-client-secret", "1//refresh-token-from-google",
                                                  "vnc-env-password"])
        out = a(text)
        for v in list(files.values()) + ["GOCSPX-gcal-client-secret", "1//refresh-token-from-google", "vnc-env-password"]:
            assert v not in out, out
    finally:
        for name in list(files) + ["google-calendar.json"]:
            (d / name).unlink(missing_ok=True)


# ------------------------------------------------------------------ contact data, network
@pytest.mark.parametrize("raw", ["max.muster@example.org", "Max_Muster+wh@mail.example.at"])
def test_emails(raw):
    out = anon()(f"mail to {raw} bounced")
    assert raw not in out and "<email>" in out


@pytest.mark.parametrize("raw", ["+43 664 1234567", "+436641234567", "0043 1 234 56 78", "0664/1234567",
                                 "0664 123 45 67", "06641234567", "01 234 5678", "0660.123.4567"])
def test_phone_numbers(raw):
    out = anon()(f"Ruf mich an: {raw} danke")
    assert raw not in out and "<phone>" in out, out


@pytest.mark.parametrize("raw, gone", [
    ("connect to 192.168.1.20:8123 refused", "192.168.1.20"),
    ("peer 2001:db8::1 reset", "2001:db8::1"),
    ("from fe80::1ff:fe23:4567:890a", "fe80::1ff:fe23:4567:890a"),
])
def test_ip_addresses(raw, gone):
    out = anon()(raw)
    assert gone not in out and "<ip>" in out


def test_loopback_kept():
    assert anon()("listening on 127.0.0.1:8000") == "listening on 127.0.0.1:8000"


def test_url_userinfo_query_and_fragment():
    out = anon()("fetch https://bob:pw123@example.org/path/x?token=abc&q=rad#frag failed.")
    assert out == "fetch https://example.org/path/x?<query> failed."


def test_relative_query_strings():
    out = anon()("GET /iad/object?adId=1000000001&email=a 404")
    assert "adId=1000000001" not in out and "?<query>" in out


def test_dates_are_not_phone_numbers():
    assert anon()("abholen am 01.10.2026 oder 09.10.26") == "abholen am 01.10.2026 oder 09.10.26"


def test_sibling_subdomains_of_home_domain(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_url", "https://willhaben.lush.ws")
    monkeypatch.setenv("PROXY_HOSTS", "nas.example.co.at")
    hosts = bugreport.known_hosts()
    assert "lush.ws" in hosts and "example.co.at" in hosts
    a = anon(hosts=hosts)
    out = a("proxy error ha.lush.ws and paperless.lush.ws unreachable; https://cloud.example.co.at/x; "
            "willhaben.at ok")
    assert "lush" not in out and "example" not in out, out
    assert out.count("<host>") == 3 and "willhaben.at ok" in out


def test_home_hosts():
    a = anon(hosts=["homeserver.lan", "ha.example.net"])
    out = a("HA at https://ha.example.net:8123/api failed; novnc http://homeserver.lan:6080/vnc.html; "
            "host homeserver.lan unreachable")
    assert "ha.example.net" not in out and "homeserver.lan" not in out
    assert "https://<host>:8123/api" in out and out.count("<host>") == 3


# ------------------------------------------------------------------ ids -> stable placeholders
def test_ids_get_stable_placeholders():
    a = anon(ids={CONV: "conv", USER_UUID: "user", "55511122": "user", "777888999": "tg"})
    out = a(f"ad 1000000001 in conv {CONV} by {USER_UUID} (login 55511122), ad 1000000002, again 1000000001")
    assert out == "ad ad#1 in conv conv#1 by user#1 (login user#2), ad ad#2, again ad#1"
    # same instance: the next text continues the numbering
    assert a(f"{CONV.upper()} / {OTHER_UUID} / ad 1000000002") == "conv#1 / id#1 / ad ad#2"
    assert a("telegram chat 777888999 linked") == "telegram chat <telegram-chat> linked"


def test_ad_ids_in_willhaben_urls():
    a = anon()
    out = a("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/ktm-kinderrad-1000000001/ and adId: 1000000001")
    assert "1000000001" not in out
    assert out.count("ad#1") == 2
    assert "ktm" not in out and "kinderrad" not in out, out


@pytest.mark.parametrize("raw, leaks", [
    ("GET https://www.willhaben.at/iad/kaufen-und-verkaufen/d/kinderfahrrad-20-zoll-blau-1000000001/ 404",
     ["kinderfahrrad", "zoll", "blau", "1000000001"]),
    ("Page.goto: net::ERR_ABORTED at https://www.willhaben.at/iad/kaufen-und-verkaufen/d/rotes-sofa-von-oma-1000000002",
     ["rotes", "sofa", "oma", "1000000002"]),
    ("GET /iad/kaufen-und-verkaufen/d/rotes-sofa-von-oma-1000000003/ failed", ["rotes", "sofa", "1000000003"]),
    ("HTTP 500 for https://www.willhaben.at/iad/verkaeuferprofil/12345678", ["12345678"]),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/geschenk-fuer-max-muster-zum-geburtstag",
     ["geschenk", "muster"]),
])
def test_url_paths_without_titles_or_user_ids(raw, leaks):
    out = anon()(raw)
    for leak in leaks:
        assert leak not in out, out
    assert "willhaben.at/iad/" in out or "GET /iad/" in out


# ------------------------------------------------------------------ names, texts, paths
def test_names_and_texts():
    a = anon(names=["Max Muster", "maxi_1990"],
             texts=["Ist das Fahrrad noch zu haben? Ich zahle bar und hole morgen ab.", "KTM Kinderrad 20 Zoll blau"])
    out = a("draft for Max Muster (maxi_1990): Ist das Fahrrad noch zu haben? Ich zahle\nnext line Muster ok")
    assert "Max" not in out and "maxi_1990" not in out and "Fahrrad" not in out
    assert out.splitlines()[0] == "draft for <name> (<name>): <text>"
    assert out.splitlines()[1] == "next line <name> ok"
    assert a("hit KTM Kinderrad 20 Zoll blau saved") == "hit <text>"


def test_names_any_case_and_short_full_names():
    a = anon(names=["Max Muster", "Bo"])
    assert a("reply to max muster / MAX MUSTER / Bo / bo") == "reply to <name> / <name> / <name> / <name>"
    assert a("bonus and Bob stay") == "bonus and Bob stay"


def test_texts_in_json_other_case_and_spacing():
    a = anon(texts=["Kühlschrank Liebherr top Zustand", "Kinderfahrrad 20 Zoll blau", 'Sofa "wie neu" abzugeben'])
    for raw in ('payload {"text": "K\\u00fchlschrank Liebherr top Zustand"}',
                "payload " + json.dumps({"text": 'Sofa "wie neu" abzugeben'}),
                "hit kinderfahrrad   20 zoll BLAU saved"):
        out = a(raw)
        assert "<text>" in out and "Liebherr" not in out and "zoll" not in out.lower() and "neu" not in out, out


def test_relative_data_dir_does_not_garble_words():
    out = anon(data_dir=Path("data"))("metadata error in data/app.db, update_data done")
    assert "metadata error" in out and "update_data done" in out and "<<" not in out, out


def test_data_dir_paths_relative(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    out = anon(data_dir=d)(f"cannot write {d}/labels/x.pdf; profile at /home/maxmuster/.cache")
    assert str(tmp_path) not in out and "<data>/labels/x.pdf" in out and "maxmuster" not in out


def test_no_over_redaction_of_plain_text():
    plain = ("Search agent 3 failed: HTTP 429 Too Many Requests at 12:00:01 on 2026-10-09, playwright 1.63.0, "
             "retry in 120 s; maximum 10 per hour; ValueError: invalid literal for int() with base 10: 'abc'; "
             "File \"/app/app/services/search_agents.py\", line 123, in run_agent; price € 1.200; 3/4 done")
    a = anon(names=["Max Muster"], hosts=["homeserver.lan"], secrets=["topsecret-pw"])
    assert a(plain) == plain


# ------------------------------------------------------------------ ring buffer
def test_ring_buffer_captures_warnings_and_tracebacks():
    h = logbuffer.RingBufferHandler(warn_max=3, info_max=2)
    lg = logging.getLogger("test.bugreport.ring")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        lg.info("just info %s", 1)
        lg.warning("something odd: %s", "x")
        try:
            raise ValueError("boom 1000000001")
        except ValueError:
            lg.exception("job failed")
        for i in range(5):
            lg.error("err %d", i)
    finally:
        lg.removeHandler(h)
    recs = h.records()
    assert len(recs) == 3 and [r["message"] for r in recs] == ["err 2", "err 3", "err 4"]   # capped, newest kept
    h.clear()
    lg.addHandler(h)
    try:
        lg.info("info line")
        try:
            raise ValueError("boom")
        except ValueError:
            lg.exception("job failed")
    finally:
        lg.removeHandler(h)
    assert [r["message"] for r in h.records()] == ["job failed"]
    tb = h.records()[0]["traceback"]
    assert "Traceback" in tb and "ValueError: boom" in tb
    assert [r["level"] for r in h.records(include_info=True)] == ["INFO", "ERROR"]


# ------------------------------------------------------------------ report + routes
@pytest.fixture
def client():
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _seed():
    with dbmod.session() as db:
        db.add(dbmod.SearchAgent(name="Geschenk für Erika Musterfrau", source_url="u", webapi_url="u",
                                 last_error="HTTP 500 for https://www.willhaben.at/webapi/x?keyword=rad"))
        db.add(dbmod.Conversation(id=CONV, ad_id="1000000001", ad_title="KTM Kinderrad 20 Zoll blau",
                                  partner_name="Max Muster", last_message_text="Ist das Rad noch zu haben?"))
        db.add(dbmod.Message(conversation_id=CONV, ext_id="m1", direction="in",
                             text="Ist das Rad noch zu haben? Meine Nummer ist 0664 1234567"))
        db.add(dbmod.Hit(agent_id=1, ad_id="1000000004", heading="Rotes Sofa", location="Musterdorf, Waldviertel", url="u",
                         body="Sehr gut erhalten, Abholung in der Musterdorfer Hauptstraße 5\nzweite Zeile"))
        db.add(dbmod.Shipment(conversation_id=CONV, recipient={"name": "Erika Musterfrau", "street": "Testgasse", "house_no": "17",
                                         "zip": "1234", "city": "Musterstadt"}, note="Klingel defekt, bitte anrufen"))
        db.add(dbmod.LLMCall(purpose="chat", model="claude-opus-5-5", ok=False,
                             error=f"401 invalid token {SK} for max.muster@example.org"))
        db.commit()
    dbmod.set_setting("reply_rules", "Ich heiße Max und wohne in der Musterstraße 1")
    dbmod.set_setting("push_channel", "telegram")
    dbmod.set_setting("telegram_chat_name", "Maximilian")


def test_unhandled_route_exception_logged_and_anonymised(client):
    from app.main import app

    async def boom(ad_id: str):
        raise RuntimeError(f"cannot load ad {ad_id} for max.muster@example.org with {SK}")

    app.add_api_route("/__test_boom/{ad_id}", boom)
    try:
        r = client.get("/__test_boom/1000000001?email=max.muster@example.org")
        assert r.status_code == 500
    finally:
        app.router.routes[:] = [rt for rt in app.router.routes if getattr(rt, "path", "") != "/__test_boom/{ad_id}"]
    recs = [x for x in logbuffer.records() if x["logger"] == "secondhandler.errors"]
    assert recs and recs[-1]["message"] == "unhandled error: GET /__test_boom/{ad_id} -> 500 (RuntimeError)"
    assert "RuntimeError: cannot load ad" in recs[-1]["traceback"]
    text = bugreport.render_text(bugreport.build("it broke"))
    assert "GET /__test_boom/{ad_id} -> 500 (RuntimeError)" in text
    assert "cannot load ad ad#" in text
    assert "max.muster@example.org" not in text and SK not in text and "1000000001" not in text


def test_report_sections_and_anonymity(client):
    _seed()
    logging.getLogger("app.services.chats").warning(
        "auto reply for %s (Max Muster): Ist das Rad noch zu haben? Meine Nummer ist 0664 1234567", CONV)
    logging.getLogger("app.services.shipping").warning(
        "label failed for Testgasse 17, 1234 Musterstadt (Klingel defekt, bitte anrufen); hit at Musterdorf, "
        "Waldviertel: Abholung in der Musterdorfer Hauptstraße 5")
    rep = bugreport.build("Max Muster schrieb mir von max.muster@example.org, dann kam Fehler 500",
                          include_info=False, include_settings=True)
    text = bugreport.render_text(rep)
    for section in ("## What happened", "## App", "## Connections", "## Activity", "## Errors", "## Settings"):
        assert section in text
    assert rep["app"]["version"] and "fastapi" in rep["app"]["packages"]
    assert rep["activity"]["counts"]["conversations"] == 1 and rep["activity"]["counts"]["messages"] == 1
    assert any(g["purpose"] == "chat" and g["failures"] == 1 for g in rep["activity"]["llm_calls"])
    assert {i["item"] for i in rep["connections"]["items"]} >= {"willhaben", "claude", "ha", "telegram"}
    assert rep["settings"]["app"]["push_channel"] == "telegram"
    assert rep["settings"]["app"]["reply_rules"] == "<set>"
    assert rep["settings"]["app"]["telegram_chat_name"] == "<set>"
    assert rep["settings"]["environment"]["dashboard_password"] == "<empty>"
    assert rep["description"] == "<name> schrieb mir von <email>, dann kam Fehler 500"
    for leak in ("Max Muster", "Musterfrau", "0664 1234567", "Ist das Rad", "Kinderrad", "max.muster@", SK, CONV,
                 "keyword=rad", "Musterstraße", "Maximilian", "Testgasse 17", "1234 Musterstadt", "Klingel",
                 "Waldviertel", "Hauptstraße 5"):
        assert leak not in text, leak
    assert "conv#1" in text and "<redacted>" not in rep["description"]
    assert json.loads(json.dumps(rep, default=str))["format"] == "secondhandler-bugreport/1"
    no_settings = bugreport.build("", include_settings=False)
    assert "settings" not in no_settings


def test_info_logs_only_on_request(client, caplog):
    caplog.set_level(logging.INFO)
    logging.getLogger("app.test").info("an info line about agent 7")
    assert "an info line about agent 7" not in bugreport.render_text(bugreport.build(""))
    assert "an info line about agent 7" in bugreport.render_text(bugreport.build("", include_info=True))


def test_page_and_downloads(client):
    _seed()
    r = client.get("/bugreport")
    assert r.status_code == 200 and "What happened?" in r.text and "Create report" in r.text
    assert 'href="/bugreport"' in client.get("/").text            # footer link
    r = client.post("/bugreport", data={"description": "Max Muster: Seite leer", "include_settings": "1"})
    preview = r.text.split('id="br-text"')[1].split("</pre>")[0]
    assert r.status_code == 200 and "## App" in preview and "Max Muster" not in preview
    token = r.text.split('href="/bugreport/')[1].split(".txt")[0]
    t = client.get(f"/bugreport/{token}.txt")
    assert t.status_code == 200 and t.text.startswith("# SecondHandler bug report")
    assert "attachment" in t.headers["content-disposition"] and t.headers["content-disposition"].endswith('.txt"')
    j = client.get(f"/bugreport/{token}.json")
    assert j.status_code == 200 and j.json()["description"] == "<name>: Seite leer"
    assert client.get(f"/bugreport/{token}.exe").status_code == 404
    assert client.get("/bugreport/unknown.txt").status_code == 404


def test_page_in_german(client):
    dbmod.set_setting("ui_lang", "de")
    try:
        r = client.get("/bugreport")
        assert "Was ist passiert?" in r.text and "Bericht erstellen" in r.text and "Fehler melden" in r.text
        r = client.post("/bugreport", data={"description": "x"})
        assert "Kopieren" in r.text and "Herunterladen (.txt)" in r.text
    finally:
        dbmod.set_setting("ui_lang", "auto")
