"""The platforms the app knows: which exist (env PLATFORMS_ENABLED), which the user switched on (Settings → Platforms),
and which one a pasted link belongs to. Instances are created lazily; tests swap them with set_platform()."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from app.i18n import t
from app.platforms.base import InvalidSearchUrl, Platform, UnknownPlatform
from app.platforms.types import DEFAULT_PLATFORM

log = logging.getLogger(__name__)

DEFAULT = DEFAULT_PLATFORM
_FACTORIES: dict[str, str] = {        # id -> "module:Class"; order = display order
    "willhaben": "app.platforms.willhaben:WillhabenPlatform",
    "kleinanzeigen": "app.platforms.kleinanzeigen:KleinanzeigenPlatform",
    "shpock": "app.platforms.shpock:ShpockPlatform",
    "vinted": "app.platforms.vinted:VintedPlatform",
    "preisjaeger": "app.platforms.preisjaeger:PreisjaegerPlatform",
}
_instances: dict[str, Platform] = {}
_overrides: dict[str, Platform | None] = {}


def _known_ids() -> list[str]:
    from app.config import get_settings

    raw = get_settings().platforms_enabled or DEFAULT
    wanted = [x.strip().lower() for x in raw.split(",") if x.strip()]
    ids = [DEFAULT] + [x for x in wanted if x != DEFAULT and x in _FACTORIES]
    return ids + [x for x in _overrides if x not in ids and _overrides[x] is not None]


def _build(pid: str) -> Platform:
    import importlib

    module, _, cls = _FACTORIES[pid].partition(":")
    return getattr(importlib.import_module(module), cls)()


def get(pid: str) -> Platform:
    """The adapter with this id (also when it is switched off; check enabled()). UnknownPlatform otherwise."""
    pid = (pid or DEFAULT).lower()
    if pid in _overrides:
        if _overrides[pid] is None:
            raise UnknownPlatform(pid)
        return _overrides[pid]
    if pid not in _instances:
        if pid not in _FACTORIES or pid not in _known_ids():
            raise UnknownPlatform(pid)
        _instances[pid] = _build(pid)
    return _instances[pid]


def enabled(pid: str) -> bool:
    """willhaben is always on; the others need Settings → Platforms (setting platform_<id>_enabled)."""
    pid = (pid or DEFAULT).lower()
    if pid == DEFAULT:
        return True
    if pid in _overrides:
        return _overrides[pid] is not None
    if pid not in _known_ids():
        return False
    from app.db import get_setting

    return get_setting(f"platform_{pid}_enabled") == "1"


def all(enabled_only: bool = True) -> list[Platform]:  # noqa: A001 - module-level API like the builtin, on purpose
    out = []
    for pid in _known_ids():
        if enabled_only and not enabled(pid):
            continue
        try:
            out.append(get(pid))
        except Exception as e:  # noqa: BLE001 - one broken adapter must not hide the others
            log.warning("platform %s unavailable: %s", pid, e)
    return out


def names(enabled_only: bool = True) -> str:
    return ", ".join(p.name for p in all(enabled_only))


def for_url(url: str) -> Platform:
    """The platform a pasted link belongs to. InvalidSearchUrl for unknown or switched-off sites."""
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        host = ""
    for p in all(enabled_only=False):
        if p.matches(url):
            if not enabled(p.id):
                raise InvalidSearchUrl(t("{name} is switched off in Settings → Platforms", name=p.name))
            return p
    raise InvalidSearchUrl(t("Not a search link of a supported site ({sites})", sites=names()) if host
                           else t("Not a search link of a supported site ({sites})", sites=names()))


def for_host(host: str | None) -> Platform | None:
    """The platform owning this host (site or image CDN), else None."""
    for p in all(enabled_only=False):
        if p.matches_host(host):
            return p
    return None


def multi() -> bool:
    """Agents watch more than one platform, one agent several sites or several agents different ones (the UI then
    shows platform labels on hits and pushes)."""
    from sqlmodel import select

    from app.db import SearchAgent, session

    try:
        with session() as db:
            rows = db.exec(select(SearchAgent.platform, SearchAgent.queries)).all()
    except Exception:  # noqa: BLE001 - no database yet
        return False
    ids: set[str] = set()
    for pid, queries in rows:
        ids.add(pid or DEFAULT)
        ids.update(str(q["platform"]) for q in queries or [] if isinstance(q, dict) and q.get("platform"))
        if len(ids) > 1:
            return True
    return False


def set_platform(pid: str, platform: Platform | None) -> None:
    """Tests: register a fake adapter under `pid` (None = behave as unknown). reset() restores the real ones."""
    _overrides[pid] = platform


def reset() -> None:
    _overrides.clear()
    _instances.clear()


async def aclose_all() -> None:
    for p in list(_instances.values()):
        try:
            await p.aclose()
        except Exception as e:  # noqa: BLE001
            log.debug("closing platform %s: %s", p.id, e)


def status(pid: str) -> dict[str, Any]:
    p = get(pid)
    return {**p.status(), "enabled": enabled(pid)}
