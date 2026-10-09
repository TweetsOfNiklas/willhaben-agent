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
