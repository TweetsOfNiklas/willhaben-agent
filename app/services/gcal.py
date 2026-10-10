"""Google Calendar: free times for chat replies, and appointments agreed in a chat as calendar entries.

OAuth with the owner's own Google Cloud client of type "Desktop app" (no app verification needed for
one user). Google only redirects such clients to loopback addresses, and the dashboard usually runs on
plain http in the LAN, so the redirect goes to http://127.0.0.1:53682/ where nothing answers: the user
copies the address from the browser's error page and pastes it into Settings. PKCE + state as usual.

Client id/secret and the refresh token live in DATA_DIR/google-calendar.json (mode 600). Only free/busy
blocks are read (no titles of other appointments), so nothing private ends up in a prompt.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.db import get_setting, set_setting
from app.i18n import t

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Vienna")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
REDIRECT_URI = "http://127.0.0.1:53682/"
# events: free/busy + create entries; readonly: list calendars to pick from
SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"
MIN_SLOT = timedelta(minutes=30)
BUFFER_MAX = 240                                     # minutes kept free before and after each busy block
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]   # German on purpose: goes into the prompt

_transport: httpx.AsyncBaseTransport | None = None   # tests inject a mock transport
_pending: dict[str, tuple[str, float]] = {}          # state -> (code_verifier, created)
_cache: dict[str, tuple[float, Any]] = {}            # short-lived free/busy cache


class CalendarError(RuntimeError):
    pass


# ------------------------------------------------------------------ stored credentials
def _path():
    return get_settings().data_dir / "google-calendar.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def has_client() -> bool:
    d = _load()
    return bool(d.get("client_id") and d.get("client_secret"))


def connected() -> bool:
    return bool(_load().get("refresh_token"))


def save_client(client_id: str = "", client_secret: str = "", client_json: str = "") -> None:
    """Client id + secret, typed in or as the JSON file Google offers for download."""
    if client_json.strip():
        try:
            raw = json.loads(client_json)
        except ValueError as e:
            raise CalendarError(t("This is not the JSON file from Google Cloud")) from e
        kind = "desktop" if "installed" in raw else "web" if "web" in raw else "unknown"
        inner = raw.get("installed") or raw.get("web") or raw
        client_id, client_secret = inner.get("client_id", ""), inner.get("client_secret", "")
        if kind == "web" and REDIRECT_URI not in (inner.get("redirect_uris") or []):
            kind = "web-unregistered"     # Google will answer redirect_uri_mismatch
    else:
        kind = "unknown"                  # typed in: the type is not visible
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
        raise CalendarError(t("Client ID or secret missing"))
    _save({"client_id": client_id, "client_secret": client_secret, "kind": kind})   # a new client invalidates old tokens
    _cache.clear()


def status() -> dict[str, Any]:
    d = _load()
    return {"client": has_client(), "connected": bool(d.get("refresh_token")), "account": d.get("account"),
            "client_id": d.get("client_id", ""), "kind": d.get("kind", "unknown")}


# ------------------------------------------------------------------ OAuth
def authorize_url() -> str:
    d = _load()
    if not has_client():
        raise CalendarError(t("Client ID or secret missing"))
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    now = time.monotonic()
    for k, (_, created) in list(_pending.items()):
        if now - created > 1800:
            _pending.pop(k, None)
    _pending[state] = (verifier, now)
    q = {"client_id": d["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code", "scope": SCOPES,
         "access_type": "offline", "prompt": "consent", "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{AUTH_URL}?{urlencode(q)}"


async def finish(pasted: str) -> str:
    """The address (or just the code) the user copied after consenting -> refresh token. Returns the account."""
    pasted = pasted.strip()
    if urlsplit(pasted).netloc.endswith("accounts.google.com"):   # the real result also carries iss=…accounts.google.com
        reason = google_error(pasted)
        log.warning("google sign-in: a Google page was pasted instead of the 127.0.0.1 address (%s)", reason or "no error in it")
        if "redirect_uri_mismatch" in reason:
            raise CalendarError(t("That is Google's error page, not the sign-in result. With “redirect_uri_mismatch” the client is a “Web application”: upload a “Desktop app” client and connect again."))
        if "access_denied" in reason or "admin_policy" in reason:
            raise CalendarError(t("Google denied access ({reason}). Add your Google account as test user under Google Auth Platform → Audience, or publish the app, then connect again.", reason=reason))
        if reason:
            raise CalendarError(t("Google reports: {reason}", reason=reason))
        raise CalendarError(t("That is an address on accounts.google.com. Finish the sign-in first; the address to copy starts with {url}", url=REDIRECT_URI))
    q = {k: v[0] for k, v in parse_qs(urlsplit(pasted).query).items()} if "?" in pasted else {"code": pasted}
    if q.get("error"):
        raise CalendarError(t("Google: {error}", error=q["error"]))
    code, state = q.get("code", ""), q.get("state")
    if not code:
        raise CalendarError(t("No code found in the pasted address"))
    if state is not None:
        pending = _pending.pop(state, None)
        if pending is None:
            raise CalendarError(t("Sign-in expired or unknown – please connect again"))
        verifier = pending[0]
    elif len(_pending) == 1:              # only the code was pasted: use the one open sign-in
        verifier = _pending.popitem()[1][0]
    else:
        raise CalendarError(t("Please paste the whole address from the browser"))
    d = _load()
    async with httpx.AsyncClient(timeout=20, transport=_transport) as c:
        try:
            r = await c.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                              "client_id": d.get("client_id"), "client_secret": d.get("client_secret"),
                                              "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        except httpx.HTTPError as e:
            raise CalendarError(t("Google not reachable: {error}", error=e)) from e
    if r.status_code != 200:
        raise CalendarError(t("Google rejected the code: {error}", error=_err(r)))
    tok = r.json()
    if not tok.get("refresh_token"):
        raise CalendarError(t("Google sent no refresh token – remove the app's access in your Google account and connect again"))
    d.update(refresh_token=tok["refresh_token"], access_token=tok.get("access_token"),
             expires_at=time.time() + int(tok.get("expires_in", 3600)) - 60)
    _save(d)
    _cache.clear()
    try:
        primary = next((c for c in await calendars() if c.get("primary")), None)
    except CalendarError:
        primary = None
    if primary:
        d["account"] = primary["id"]
        _save(d)
    log.info("google calendar connected: %s", d.get("account"))
    return d.get("account") or ""


def google_error(url: str) -> str:
    """Readable reason from a Google error page address (authError is base64 protobuf with text in it)."""
    import re
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    parts = [q[k] for k in ("error", "error_description") if q.get(k)]
    raw = q.get("authError", "")
    if raw:
        try:
            blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError:
            blob = b""
        parts += [m.decode() for m in re.findall(rb"[ -~]{6,}", blob)]
    return "; ".join(dict.fromkeys(p.strip() for p in parts if p.strip()))[:300]


async def disconnect() -> None:
    d = _load()
    token = d.get("refresh_token")
    if token:
        try:
            async with httpx.AsyncClient(timeout=10, transport=_transport) as c:
                await c.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError as e:
            log.info("google revoke failed: %s", e)
    for k in ("refresh_token", "access_token", "expires_at", "account"):
        d.pop(k, None)
    _save(d)
    _cache.clear()


def _err(r: httpx.Response) -> str:
    try:
        j = r.json()
        e = j.get("error")
        return (e.get("message") if isinstance(e, dict) else j.get("error_description") or e) or f"HTTP {r.status_code}"
    except ValueError:
        return f"HTTP {r.status_code}"


async def _access_token(c: httpx.AsyncClient) -> str:
    d = _load()
    if not d.get("refresh_token"):
        raise CalendarError(t("Google Calendar is not connected"))
    if d.get("access_token") and d.get("expires_at", 0) > time.time():
        return d["access_token"]
    try:
        r = await c.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                                          "client_id": d.get("client_id"), "client_secret": d.get("client_secret")})
    except httpx.HTTPError as e:
        raise CalendarError(t("Google not reachable: {error}", error=e)) from e
    if r.status_code != 200:
        if r.status_code in (400, 401) and "invalid_grant" in r.text:
            raise CalendarError(t("Google access expired or revoked – connect again in Settings"))
        raise CalendarError(t("Google token refresh failed: {error}", error=_err(r)))
    tok = r.json()
    d.update(access_token=tok["access_token"], expires_at=time.time() + int(tok.get("expires_in", 3600)) - 60)
    _save(d)
    return d["access_token"]


async def _api(method: str, path: str, **kw: Any) -> Any:
    async with httpx.AsyncClient(timeout=20, transport=_transport) as c:
        token = await _access_token(c)
        try:
            r = await c.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError as e:
            raise CalendarError(t("Google not reachable: {error}", error=e)) from e
    if r.status_code >= 400:
        raise CalendarError(t("Google Calendar: {error}", error=_err(r)))
    return r.json() if r.content else {}


# ------------------------------------------------------------------ settings
def config() -> dict[str, Any]:
    return {
        "calendars": [c for c in (get_setting("gcal_calendars") or "primary").split(",") if c],
        "target": get_setting("gcal_target") or "primary",
        "days": int(get_setting("gcal_days") or 7),
        "window_start": get_setting("gcal_window_start") or "09:00",
        "window_end": get_setting("gcal_window_end") or "20:00",
        "duration": int(get_setting("gcal_duration") or 30),
        "in_drafts": get_setting("gcal_in_drafts") != "0",
        "in_auto": get_setting("gcal_in_auto") == "1",
        "buffer": min(max(int(get_setting("gcal_buffer") or 0), 0), BUFFER_MAX),
    }


def save_config(calendars: list[str], target: str, days: int, window_start: str, window_end: str,
                duration: int, in_drafts: bool, in_auto: bool, buffer: int) -> None:
    for v in (window_start, window_end):
        datetime.strptime(v, "%H:%M")
    set_setting("gcal_calendars", ",".join(c.strip() for c in calendars if c.strip()) or "primary")
    set_setting("gcal_target", target.strip() or "primary")
    set_setting("gcal_days", str(min(max(1, days), 21)))
    set_setting("gcal_window_start", window_start)
    set_setting("gcal_window_end", window_end)
    set_setting("gcal_duration", str(min(max(5, duration), 480)))
    set_setting("gcal_in_drafts", "1" if in_drafts else "0")
    set_setting("gcal_in_auto", "1" if in_auto else "0")
    set_setting("gcal_buffer", str(min(max(0, buffer), BUFFER_MAX)))
    _cache.clear()


async def calendars() -> list[dict[str, Any]]:
    data = await _api("GET", "/users/me/calendarList", params={"minAccessRole": "reader"})
    return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or c["id"],
             "primary": bool(c.get("primary")), "writable": c.get("accessRole") in ("writer", "owner")}
            for c in data.get("items", [])]


# ------------------------------------------------------------------ free times
def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


async def busy(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    cfg = config()
    key = f"{start:%Y%m%d%H%M}-{end:%Y%m%d%H%M}-{','.join(cfg['calendars'])}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    data = await _api("POST", "/freeBusy", json={
        "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": "Europe/Vienna",
        "items": [{"id": c} for c in cfg["calendars"]]})
    blocks = []
    for cal_id, cal in (data.get("calendars") or {}).items():
        if cal.get("errors"):
            log.info("free/busy for %s: %s", cal_id, cal["errors"])
        for b in cal.get("busy", []):
            blocks.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ),
                           datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ)))
    blocks.sort()
    _cache[key] = (time.monotonic(), blocks)
    return blocks


def free_slots(blocks: list[tuple[datetime, datetime]], now: datetime, days: int, window_start: str,
               window_end: str, buffer_min: int = 0) -> list[tuple[date, list[tuple[datetime, datetime]]]]:
    """Free intervals (≥ 30 min) inside the daily window, per day, starting now. Every busy block is widened by
    buffer_min on both sides first (travel time between appointments)."""
    out = []
    (sh, sm), (eh, em) = _hm(window_start), _hm(window_end)
    if buffer_min:
        pad = timedelta(minutes=buffer_min)
        blocks = [(b0 - pad, b1 + pad) for b0, b1 in blocks]
    for i in range(days):
        day = (now + timedelta(days=i)).date()
        lo = datetime(day.year, day.month, day.day, sh, sm, tzinfo=TZ)
        hi = datetime(day.year, day.month, day.day, eh, em, tzinfo=TZ)
        if i == 0:   # today: not before now, rounded up to the next quarter hour
            rounded = now.replace(second=0, microsecond=0) + timedelta(minutes=(-now.minute) % 15)
            lo = max(lo, rounded)
        cur, free = lo, []
        for b0, b1 in blocks:
            if b1 <= cur or b0 >= hi:
                continue
            if b0 - cur >= MIN_SLOT:
                free.append((cur, b0))
            cur = max(cur, b1)
        if hi - cur >= MIN_SLOT:
            free.append((cur, hi))
        out.append((day, free))
    return out


def format_slots(slots: list[tuple[date, list[tuple[datetime, datetime]]]]) -> str:
    lines = []
    for day, free in slots:
        label = f"{WEEKDAYS[day.weekday()]} {day:%d.%m.}"
        if free:
            lines.append(f"- {label}: " + ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in free))
        else:
            lines.append(f"- {label}: keine Zeit")
    return "\n".join(lines)


async def availability_text(now: datetime | None = None) -> str | None:
    """Prompt block with my free times, or None if not connected / failing (drafts work without it)."""
    if not connected():
        return None
    cfg = config()
    now = (now or datetime.now(TZ)).astimezone(TZ)
    end = datetime.combine((now + timedelta(days=cfg["days"])).date(), datetime.min.time(), TZ)
    try:
        blocks = await busy(now, end)
    except CalendarError as e:
        log.warning("calendar availability failed: %s", e)
        return None
    slots = free_slots(blocks, now, cfg["days"], cfg["window_start"], cfg["window_end"], cfg["buffer"])
    return format_slots(slots)


# ------------------------------------------------------------------ entries
def parse_local(value: str) -> datetime:
    """'2026-10-10T15:30' (or with a space) as Vienna time."""
    try:
        dt = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError as e:
        raise CalendarError(t("Invalid date/time: {value}", value=value)) from e
    return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)


async def add_event(*, summary: str, start: datetime, minutes: int, location: str = "",
                    description: str = "") -> dict[str, Any]:
    body = {"summary": summary, "location": location or None, "description": description or None,
            "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Vienna"},
            "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(), "timeZone": "Europe/Vienna"},
            "source": {"title": "willhaben", "url": "https://www.willhaben.at/iad/myprofile/chat"}}
    ev = await _api("POST", f"/calendars/{_q(config()['target'])}/events", json={k: v for k, v in body.items() if v})
    _cache.clear()
    return {"id": ev.get("id"), "link": ev.get("htmlLink")}


def _q(cal_id: str) -> str:
    return quote(cal_id, safe="")
