"""Dashboard login: a login page with a signed session cookie (no browser popup).

Active only when DASHBOARD_PASSWORD is set. Browsers get a cookie from /login; scripts, the MCP server and
other API clients can keep sending HTTP Basic auth. Changing the password or user ends all sessions, because
the cookie signature covers both.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from app.config import get_settings

COOKIE = "wh_session"
REMEMBER_S = 30 * 86400      # "stay logged in"
SESSION_S = 12 * 3600        # otherwise: browser session, at most 12 h
PUBLIC_PATHS = {"/healthz", "/login", "/lang"}    # /lang: the language switch on the login page
PUBLIC_PREFIXES = ("/static/",)    # stylesheets: the login page needs them before any login

MAX_FAILS, FAIL_WINDOW_S = 10, 15 * 60
_fails: dict[str, deque[float]] = defaultdict(deque)


def enabled() -> bool:
    return bool(get_settings().dashboard_password)


def _secret() -> bytes:
    path = get_settings().data_dir / "session-secret"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        path.write_bytes(key)
        path.chmod(0o600)
        return key


def _sign(payload: str) -> str:
    s = get_settings()
    msg = f"{payload}|{s.dashboard_user}|{s.dashboard_password}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_token(remember: bool, now: float | None = None) -> str:
    exp = int((now or time.time()) + (REMEMBER_S if remember else SESSION_S))
    payload = f"{exp}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def valid_token(token: str | None, now: float | None = None) -> bool:
    if not token or token.count(".") != 2:
        return False
    payload, _, sig = token.rpartition(".")
    try:
        exp = int(payload.split(".")[0])
    except ValueError:
        return False
    return exp > (now or time.time()) and hmac.compare_digest(sig, _sign(payload))


def check_password(user: str, password: str) -> bool:
    s = get_settings()
    return bool(s.dashboard_password) and secrets.compare_digest(user.encode(), s.dashboard_user.encode()) \
        and secrets.compare_digest(password.encode(), s.dashboard_password.encode())


def valid_basic(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return False
    return check_password(user, pw)


def locked_out(client: str, now: float | None = None) -> bool:
    """Too many wrong passwords from this address in the last 15 minutes."""
    now = now or time.time()
    q = _fails[client]
    while q and q[0] < now - FAIL_WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILS


def record_failure(client: str, now: float | None = None) -> None:
    _fails[client].append(now or time.time())


def safe_next(target: str | None) -> str:
    """Only local paths, so the login form cannot be used to redirect elsewhere."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
