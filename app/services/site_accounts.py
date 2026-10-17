"""Accounts on other sites (Kleinanzeigen, Vinted) in the app's browser, for their messages.

The owner logs in once in the remote view (the same browser profile as willhaben, so the session stays like
willhaben's). Searches never use it: the search agents, the price check and the product search keep using the
anonymous clients (app/platforms/*), so they are not linkable to these accounts either.

Whether a session is still valid is seen by loading a page that needs one in a short-lived hidden tab: a real page,
so the sites' bot protection (DataDome on Vinted) lets it through, where a bare request would be refused. A redirect
to the login page means logged out. The state is kept in the setting `account_<site>` ({logged_in, checked_at,
error}); it is checked on demand and every few hours once the owner logged in.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db import get_setting, set_setting, utcnow
from app.i18n import t

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SiteAccount:
    id: str
    name: str
    login_url: str
    check_url: str                   # a page only a logged-in visitor gets (else a redirect to the login)
    logged_out: tuple[str, ...]      # parts of the URL a logged-out visitor lands on
    hosts: tuple[str, ...]           # what the request recorder keeps of this site


SITES: dict[str, SiteAccount] = {
    "kleinanzeigen": SiteAccount(
        "kleinanzeigen", "Kleinanzeigen", "https://www.kleinanzeigen.de/m-einloggen.html",
        "https://www.kleinanzeigen.de/m-nachrichten.html", ("m-einloggen", "/login"),
        ("kleinanzeigen.de",)),
    "vinted": SiteAccount(
        "vinted", "Vinted", "https://www.vinted.at/member/signup/select_type?ref_url=%2Finbox",
        "https://www.vinted.at/inbox", ("/member/signup", "/member/login", "/auth/"),
        ("vinted.at", "vinted.de", "vinted.com")),
}


def get(pid: str) -> SiteAccount | None:
    return SITES.get(pid)


def state(pid: str) -> dict[str, Any]:
    """{logged_in: True | False | None (never checked), checked_at: ISO | None, error: str | None}."""
    try:
        data = json.loads(get_setting(f"account_{pid}") or "{}")
    except ValueError:
        data = {}
    return {"logged_in": data.get("logged_in"), "checked_at": data.get("checked_at"), "error": data.get("error")}


def checked_at(pid: str) -> datetime | None:
    raw = state(pid)["checked_at"]
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _save(pid: str, logged_in: bool | None, error: str | None = None) -> dict[str, Any]:
    data = {"logged_in": logged_in, "checked_at": utcnow().isoformat(), "error": error}
    set_setting(f"account_{pid}", json.dumps(data))
    return data


def logged_out_url(site: SiteAccount, url: str) -> bool:
    return any(part in (url or "") for part in site.logged_out)


async def open_login(pid: str) -> None:
    """The site's login page in the visible browser window (the remote view)."""
    from app.willhaben.session import get_session

    site = SITES[pid]
    await get_session().open_url(site.login_url)


async def check(pid: str) -> dict[str, Any]:
    """Load the site's members-only page in a hidden tab; the state as stored. Never raises."""
    from app.willhaben.session import SessionUnavailable, get_session

    site = SITES[pid]
    sess = get_session()
    try:
        final = await sess.load_hidden(site.check_url)
    except SessionUnavailable as e:
        return _save(pid, None, str(e))
    except Exception as e:  # noqa: BLE001 - a timeout or a closed browser: unknown, said so
        log.info("%s login check failed: %s", site.name, e)
        return _save(pid, None, t("The check did not finish: {error}", error=str(e) or type(e).__name__))
    return _save(pid, not logged_out_url(site, final))


async def check_job() -> None:
    """Every few hours: the sites the owner logged in on (others are never asked)."""
    from app.willhaben.session import get_session

    if not get_session().started:
        return
    for pid in SITES:
        if state(pid)["logged_in"] is not None:
            await check(pid)
