"""Login details for the sites the app's browser logs in to (willhaben, Vinted), so a login after a logout is one
button: the login page opens in the remote view, the app types the e-mail and password and presses the button – as a
person would, character by character – and the owner finishes anything else there (a captcha, a code by e-mail).

Kept in DATA_DIR/login-<site>.json (mode 600), never logged, never shown again (the page only says that a password is
stored). Part of a backup like the other secrets of DATA_DIR. Nothing is submitted without the owner pressing
"Log in automatically".
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

SITES = ("willhaben", "vinted")
# where each site asks for them (seen 2026-10-21: Vinted's e-mail login page, then POST /web/api/auth/oauth)
LOGIN_PAGES = {
    "willhaben": "https://www.willhaben.at/iad/myprofile/login?r=/iad/myprofile/myadverts",
    "vinted": "https://www.vinted.at/member/login/email?ref_url=%2Finbox",
}
USER_FIELDS = ('input[type="email"]', 'input[name="email"]', 'input[name="username"]', 'input[name="login"]',
               'input[autocomplete="username"]', 'input[id*="mail" i]', 'input[id*="user" i]')
PASSWORD_FIELDS = ('input[type="password"]',)
SUBMIT = ('button[type="submit"]', 'input[type="submit"]')


def _path(site: str):
    return get_settings().data_dir / f"login-{site}.json"


def get(site: str) -> dict[str, str] | None:
    """{"user", "password"} or None."""
    try:
        data = json.loads(_path(site).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("user") and data.get("password") else None


def stored(site: str) -> dict[str, Any]:
    """What a page may show: the user name, and whether a password is there (never the password)."""
    c = get(site)
    return {"user": c["user"] if c else "", "has_password": bool(c)}


def save(site: str, user: str, password: str | None) -> None:
    """A blank password keeps the stored one (the field is never filled in again)."""
    if site not in SITES:
        raise ValueError(site)
    user = (user or "").strip()
    old = get(site)
    password = password if password else (old["password"] if old else "")
    if not user or not password:
        raise ValueError("user and password needed")
    p = _path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"user": user, "password": password}, f)


def forget(site: str) -> None:
    _path(site).unlink(missing_ok=True)


async def _first(page, selectors: tuple[str, ...], timeout_ms: int = 15000):
    """The first visible element of these selectors (waiting for the page to render it)."""
    loc = page.locator(", ".join(selectors)).locator("visible=true").first
    await loc.wait_for(state="visible", timeout=timeout_ms)
    return loc


async def type_in(page, site: str) -> None:
    """Type the stored e-mail and password into the open login page and press its button. Typed key by key with
    small pauses: bot protections look at how a form is filled. Raises when a field is missing (the page shows
    something else: already logged in, a captcha) – the owner sees it in the remote view."""
    c = get(site)
    if c is None:
        raise ValueError("no login details stored")
    user = await _first(page, USER_FIELDS)
    await user.click()
    await user.fill("")
    await user.press_sequentially(c["user"], delay=70)
    pw = await _first(page, PASSWORD_FIELDS)
    await pw.click()
    await pw.press_sequentially(c["password"], delay=80)
    await page.wait_for_timeout(500)
    button = page.locator(", ".join(SUBMIT)).locator("visible=true")
    if await button.count():
        await button.first.click()
    else:
        await pw.press("Enter")
    log.info("%s: login details typed in and sent", site)
