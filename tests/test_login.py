import base64

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.main import app


@pytest.fixture
def pw(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "dashboard_password", "geheim")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(auth, "_fails", type(auth._fails)(auth._fails.default_factory))
    monkeypatch.setattr("app.main.asyncio.sleep", _no_sleep)


async def _no_sleep(_s):
    return None


def test_browser_redirects_to_login_and_cookie_works(pw):
    with TestClient(app) as c:
        r = c.get("/myads?show_sold=1", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fmyads%3Fshow_sold%3D1"
        assert "www-authenticate" not in r.headers                      # no browser popup
        assert 'name="password"' in c.get("/login").text
        r = c.post("/login", data={"username": "admin", "password": "falsch", "next": "/myads"})
        assert r.status_code == 401 and "Wrong user name or password" in r.text
        r = c.post("/login", data={"username": "admin", "password": "geheim", "remember": "on",
                                   "next": "/myads?show_sold=1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/myads?show_sold=1"
        assert "max-age" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
        assert c.get("/myads").status_code == 200
        assert "Log out" in c.get("/").text
        c.post("/logout")
        assert c.get("/myads", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303


def test_api_clients_and_basic_auth(pw):
    with TestClient(app) as c:
        r = c.get("/api/agents")
        assert r.status_code == 401 and "www-authenticate" not in r.headers
        assert c.get("/hits", headers={"hx-request": "true"}).headers.get("hx-redirect", "").startswith("/login")
        basic = base64.b64encode(b"admin:geheim").decode()
        assert c.get("/myads", headers={"authorization": f"Basic {basic}"}).status_code == 200
        assert c.get("/healthz").status_code == 200


def test_token_and_redirect_safety(pw, monkeypatch):
    tok = auth.make_token(remember=False, now=1000)
    assert auth.valid_token(tok, now=1001) and not auth.valid_token(tok, now=1000 + auth.SESSION_S + 1)
    assert not auth.valid_token(tok[:-1] + ("0" if tok[-1] != "0" else "1"), now=1001)
    monkeypatch.setattr(get_settings(), "dashboard_password", "neu")       # password change ends sessions
    assert not auth.valid_token(tok, now=1001)
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "", None):
        assert auth.safe_next(bad) == "/"
    assert auth.safe_next("/chats/1#x") == "/chats/1#x"


def test_lockout(pw):
    with TestClient(app) as c:
        for _ in range(auth.MAX_FAILS):
            c.post("/login", data={"username": "admin", "password": "x"})
        r = c.post("/login", data={"username": "admin", "password": "geheim"}, follow_redirects=False)
        assert r.status_code == 401 and "Too many failed attempts" in r.text


def test_the_home_screen_app_gets_its_manifest_and_icons_before_any_login(pw):
    """iOS and Android fetch the manifest and the icon when the page is added to the home screen; the app then opens
    in a window of its own (standalone), the log-in page included."""
    with TestClient(app) as c:
        m = c.get("/static/manifest.json")
        assert m.status_code == 200 and m.json()["display"] == "standalone" and m.json()["start_url"] == "/"
        for icon in m.json()["icons"]:
            r = c.get(icon["src"])
            assert r.status_code == 200 and r.headers["content-type"] == icon["type"]
        assert c.get("/static/icons/apple-touch-icon.png").headers["content-type"] == "image/png"
        page = c.get("/login").text
        c.post("/login", data={"username": "admin", "password": "geheim", "next": "/"})
        for html in (page, c.get("/").text):
            assert '<link rel="manifest" href="/static/manifest.json">' in html
            assert 'name="apple-mobile-web-app-capable" content="yes"' in html and "viewport-fit=cover" in html
            assert 'rel="apple-touch-icon"' in html


def _launch(token, user_id, at, **extra):
    """Mini App launch data as Telegram signs it (core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
    import hashlib
    import hmac
    import json
    from urllib.parse import urlencode

    fields = {"auth_date": str(int(at)), "query_id": "AAH", "user": json.dumps({"id": user_id, "first_name": "Owner"}),
              **extra}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(key, check.encode(), hashlib.sha256).hexdigest()})


def test_a_dashboard_opened_from_the_telegram_bot_logs_in_the_linked_user_only(pw, monkeypatch):
    import time

    from app.db import set_setting

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123:secret")
    set_setting("telegram_chat_id", "4242")
    now = time.time()
    assert auth.telegram_user(_launch("123:secret", 4242, now), "123:secret") == 4242
    assert auth.telegram_user(_launch("123:other", 4242, now), "123:secret") is None          # another bot's
    assert auth.telegram_user(_launch("123:secret", 4242, now - 2 * 86400), "123:secret") is None   # stale
    forged = _launch("123:secret", 4242, now).replace("4242", "4243")
    assert auth.telegram_user(forged, "123:secret") is None
    with TestClient(app) as c:
        page = c.get("/login?next=%2Fchats").text
        assert "tgWebAppData" in page and "/login/telegram" in page
        r = c.post("/login/telegram", data={"init_data": _launch("123:secret", 9999, now), "next": "/chats"})
        assert r.status_code == 401 and c.get("/chats", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303
        r = c.post("/login/telegram", data={"init_data": _launch("123:secret", 4242, now), "next": "/chats"})
        assert r.status_code == 200 and r.json() == {"ok": True, "next": "/chats"}
        assert "max-age" in r.headers["set-cookie"].lower() and c.get("/chats").status_code == 200


def test_dashboard_links_in_telegram_open_as_a_mini_app_other_links_stay_links(monkeypatch):
    from app.db import set_setting
    from app.services import telegram

    monkeypatch.setattr(get_settings(), "public_url", "https://agent.example")
    set_setting("telegram_chat_id", "4242")
    assert telegram.open_button("Open", "https://agent.example/hits/1") == \
        {"text": "Open", "web_app": {"url": "https://agent.example/hits/1"}}
    assert telegram.open_button("Open", "https://www.willhaben.at/iad/object?adId=1") == \
        {"text": "Open", "url": "https://www.willhaben.at/iad/object?adId=1"}
    assert telegram.open_button("Open", "https://agent.example.evil/x")["url"]          # only the dashboard itself
    set_setting("telegram_chat_id", "-100123")                                             # a group: links only
    assert "url" in telegram.open_button("Open", "https://agent.example/hits/1")
