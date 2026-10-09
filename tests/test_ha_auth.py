from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.services import ha_auth, notify as notify_mod
from tests.test_dashboard import client  # noqa: F401,F811  (pytest fixture)


@pytest.fixture
def ha(monkeypatch, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization"),
                      request.content.decode()))
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "short", "refresh_token": "refresh", "expires_in": 1800})
        if request.url.path == "/auth/revoke":
            return httpx.Response(200)
        if request.url.path == "/api/services":
            return httpx.Response(200, json=[{"domain": "light", "services": {"turn_on": {}}},
                                             {"domain": "notify", "services": {"notify": {}, "mobile_app_pixel_8": {},
                                                                               "persistent_notification": {}}}])
        if request.url.path.startswith("/api/"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(ha_auth, "_transport", transport)
    monkeypatch.setattr(notify_mod, "_transport", transport)
    monkeypatch.setattr(ha_auth, "_token_path", lambda: tmp_path / "ha-token")

    async def fake_long_lived(url, access_token):
        assert (url, access_token) == ("https://ha.example.com", "short")
        return "LONG-LIVED"

    monkeypatch.setattr(ha_auth, "create_long_lived_token", fake_long_lived)
    return calls


async def test_connect_flow(ha, tmp_path):
    url = ha_auth.authorize_url("ha.example.com/", "http://agent.local:8010/")
    u = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://ha.example.com/auth/authorize"
    assert q["client_id"] == "http://agent.local:8010/"
    assert q["redirect_uri"] == "http://agent.local:8010/settings/ha/callback"

    with pytest.raises(ha_auth.HAError):
        await ha_auth.finish("code", "wrong-state")
    await ha_auth.finish("the-code", q["state"])
    assert (tmp_path / "ha-token").read_text() == "LONG-LIVED"
    assert (tmp_path / "ha-token").stat().st_mode & 0o777 == 0o600
    assert ha_auth.ha_url() == "https://ha.example.com" and ha_auth.ha_token() == "LONG-LIVED"
    token_call = next(c for c in ha if c[1] == "/auth/token")
    assert "grant_type=authorization_code" in token_call[3] and "code=the-code" in token_call[3]
    assert any(c[1] == "/auth/revoke" and "token=refresh" in c[3] for c in ha)

    with pytest.raises(ha_auth.HAError):          # state is single-use
        await ha_auth.finish("the-code", q["state"])

    st = await ha_auth.status()
    assert st == {"ok": True, "services": ["mobile_app_pixel_8"]}


async def test_notify_uses_stored_token_and_push_choice(ha):
    from app.db import set_setting

    q = parse_qs(urlsplit(ha_auth.authorize_url("https://ha.example.com", "http://x:8010/")).query)
    await ha_auth.finish("c", q["state"][0])
    set_setting("ha_notify_service", "mobile_app_pixel_8")
    assert await notify_mod.notify("test", "T", "M")
    paths = [(c[1], c[2]) for c in ha if c[1].startswith("/api/")]
    assert ("/api/events/willhaben_test", "Bearer LONG-LIVED") in paths
    assert ("/api/services/notify/mobile_app_pixel_8", "Bearer LONG-LIVED") in paths

    set_setting("ha_notify_service", ha_auth.NO_PUSH)
    ha.clear()
    await notify_mod.notify("test", "T", "M")
    assert not any("/services/notify/" in c[1] for c in ha)


def test_callback_route_errors(client):  # noqa: F811
    r = client.get("/settings/ha/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert r.status_code == 303 and "access_denied" in r.headers["location"]
    r = client.get("/settings/ha/callback", params={"code": "x", "state": "nope"}, follow_redirects=False)
    assert "expired" in httpx.URL(r.headers["location"]).params["msg"]
