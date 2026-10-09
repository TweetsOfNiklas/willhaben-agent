"""Home Assistant connection from the dashboard (no token copying).

OAuth as HA implements it for any web app (IndieAuth style): client_id is the dashboard's own base
URL, the redirect goes back to the dashboard, so the browser lands here automatically. With the
short-lived access token the app then creates a long-lived token named "willhaben Agent" over the
websocket API (visible and revocable in the HA profile), stores it in DATA_DIR/ha-token (mode 600)
and revokes the temporary login. The stored token and URL take precedence over HA_URL/HA_TOKEN.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import get_settings
from app.db import get_setting, set_setting
from app.i18n import t

log = logging.getLogger(__name__)

NO_PUSH = "-"            # stored as ha_notify_service when push is switched off explicitly
CLIENT_NAME = "willhaben Agent"
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport
_pending: dict[str, tuple[str, str, float]] = {}     # state -> (ha_url, client_id, created)


class HAError(RuntimeError):
    pass


# ------------------------------------------------------------------ effective configuration
def _token_path():
    return get_settings().data_dir / "ha-token"


def ha_url() -> str | None:
    return (get_setting("ha_url") or get_settings().ha_url or "").rstrip("/") or None


def ha_token() -> str | None:
    try:
        token = _token_path().read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    return token or get_settings().ha_token or None


def token_source() -> str | None:
    if _token_path().exists():
        return "login"
    return "env" if get_settings().ha_token else None


def notify_service() -> str | None:
    v = get_setting("ha_notify_service")
    if v == NO_PUSH:
        return None
    return v or get_settings().ha_notify_service or None


def _save_token(token: str) -> None:
    p = _token_path()
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, p)


def disconnect() -> None:
    _token_path().unlink(missing_ok=True)


# ------------------------------------------------------------------ OAuth
def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HAError(t("Invalid Home Assistant address"))
    return f"{u.scheme}://{u.netloc}"


def authorize_url(ha: str, dashboard_base: str) -> str:
    """Where to send the browser. dashboard_base is the URL the browser uses for this app."""
    ha = normalize_url(ha)
    client_id = dashboard_base.rstrip("/") + "/"
    state = secrets.token_urlsafe(16)
    now = time.monotonic()
    for k, (_, _, created) in list(_pending.items()):
        if now - created > 900:
            _pending.pop(k, None)
    _pending[state] = (ha, client_id, now)
    q = {"client_id": client_id, "redirect_uri": client_id + "settings/ha/callback", "state": state,
         "response_type": "code"}
    return f"{ha}/auth/authorize?{urlencode(q)}"


async def finish(code: str, state: str) -> None:
    """Callback: code -> access token -> long-lived token, then drop the temporary login."""
    pending = _pending.pop(state, None)
    if not pending:
        raise HAError(t("Sign-in expired or unknown – please connect again"))
    ha, client_id, _ = pending
    async with httpx.AsyncClient(timeout=20, transport=_transport) as c:
        try:
            r = await c.post(f"{ha}/auth/token", data={"grant_type": "authorization_code", "code": code,
                                                       "client_id": client_id})
        except httpx.HTTPError as e:
            raise HAError(t("Home Assistant not reachable: {error}", error=e)) from e
        if r.status_code != 200:
            raise HAError(t("Home Assistant rejected the code (HTTP {status})", status=r.status_code))
        tok = r.json()
        long_lived = await create_long_lived_token(ha, tok["access_token"])
        _save_token(long_lived)
        set_setting("ha_url", ha)
        try:  # the refresh token is no longer needed; long-lived token is what we keep
            await c.post(f"{ha}/auth/revoke", data={"token": tok.get("refresh_token", "")})
        except httpx.HTTPError:
            pass
    log.info("home assistant connected: %s", ha)


async def create_long_lived_token(ha: str, access_token: str) -> str:
    import websockets

    ws_url = ha.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
    try:
        async with websockets.connect(ws_url, open_timeout=15, close_timeout=5) as ws:
            await ws.recv()                                             # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": access_token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise HAError(t("Home Assistant websocket: sign-in failed"))
            await ws.send(json.dumps({"id": 1, "type": "auth/long_lived_access_token",
                                      "client_name": CLIENT_NAME, "lifespan": 3650}))
            res = json.loads(await ws.recv())
    except (OSError, websockets.WebSocketException) as e:
        raise HAError(t("Home Assistant websocket not reachable: {error}", error=e)) from e
    if not res.get("success") or not isinstance(res.get("result"), str):
        raise HAError(t("Could not create the token: {error}", error=(res.get("error") or {}).get("message", res)))
    return res["result"]


# ------------------------------------------------------------------ helpers for the settings page
async def status() -> dict[str, Any]:
    """Connection check plus the notify services that can be chosen as push target."""
    url, token = ha_url(), ha_token()
    if not (url and token):
        return {"ok": False, "services": []}
    try:
        async with httpx.AsyncClient(timeout=8, transport=_transport) as c:
            r = await c.get(f"{url}/api/services", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": t("not reachable: {error}", error=e), "services": []}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}" + (" " + t("(token invalid?)") if r.status_code == 401 else ""),
                "services": []}
    services = sorted(name for d in r.json() if d.get("domain") == "notify"
                      for name in (d.get("services") or {}) if name not in ("notify", "persistent_notification", "send_message"))
    return {"ok": True, "services": services}
