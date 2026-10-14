"""WireGuard exits for the anonymous requests: one dedicated profile, or a collection the app works through.

Only the anonymous clients use them (search agents, screening, price checks, the ad viewer, the product search and
their photo downloads); the logged-in browser never does. Each site has its own switch (setting `platform_<id>_vpn`).

The source is uploaded in Settings → VPN and kept in DATA_DIR/wireguard (folder 700, files 600):
  profile/<name>.conf    one dedicated profile, used as it is
  collection/*.conf      many configs (e.g. Mullvad's zip of every server); a site moves on to the next one when the
                         current exit is blocked or does not answer
A new upload replaces the old source.

A config in use runs as one wireproxy process (userspace WireGuard: no root, no tun device) with an HTTP CONNECT proxy
on 127.0.0.1. The clients get that proxy explicitly; nothing sets HTTP(S)_PROXY, so Chromium never sees it. Host
names are resolved inside the tunnel (the clients send CONNECT host:port). Sites that need the same config share its
process: two tunnels with one key on the same server would knock each other off (Mullvad's zip has one key for all).

Fail closed: with the switch on and no working exit, `Tunnel.ensure()` raises PlatformError and nothing falls back to
the home IP. Exits a site refused or could not reach are marked for that site (setting `vpn_state`) and skipped until
the mark expires (MARK_TTL_S).

Settings → VPN → "Test the sites" (app/services/vpn_check.py) walks each site through its exits on demand, one probe
at a time (`ensure(probes=1)`); `Tunnel.listen()` lets it show every exit the site tries.
"""
from __future__ import annotations

import asyncio
import ctypes
import io
import json
import logging
import os
import re
import shutil
import signal
import socket
import tempfile
import time
import zipfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from app.config import get_settings
from app.i18n import t
from app.platforms.base import PlatformError

log = logging.getLogger(__name__)

MODES = ("profile", "collection")
MARK_TTL_S = {"blocked": 24 * 3600, "rate": 3600, "unreachable": 3600, "broken": 24 * 3600,
              "botcheck": 3600}      # botcheck: a page in place of the one asked for (only the VPN test marks it)
MAX_PROBES = 4                 # exits one ensure() call tries before it gives up for this request
EXHAUSTED_S = 600              # after a pass without a working exit, requests fail at once for this long
PROBE_TIMEOUT_S = 15           # WireGuard repeats a lost handshake after 5 s
START_TIMEOUT_S = 5
MAX_UPLOAD = 20 << 20
MAX_MEMBERS = 2000
MAX_CONF = 16 << 10
# exits close to the site first: its home country, then these, then the rest by name
NEAR = ("at", "de", "ch", "cz", "sk", "hu", "si", "it", "pl", "nl", "be", "lu", "dk", "fr", "hr")
STATE_KEY = "vpn_state"
_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")       # a WireGuard key, base64 of 32 bytes
_ENDPOINT = re.compile(r"^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):\d{1,5}$")
Listener = Callable[[str, str, str], None]   # (exit, event, detail): see Tunnel.listen()


# ------------------------------------------------------------------ the uploaded source
def root() -> Path:
    return get_settings().wireguard_dir


def source() -> tuple[str | None, list[str]]:
    """(mode, config names) of the uploaded source; (None, []) when there is none."""
    for mode in MODES:
        d = root() / mode
        names = sorted(p.stem for p in d.glob("*.conf")) if d.is_dir() else []
        if names:
            return mode, names
    return None, []


def config_path(name: str) -> Path:
    mode, _ = source()
    return root() / (mode or "collection") / f"{name}.conf"


def country(name: str) -> str:
    """"at-vie-wg-001" -> "at" (Mullvad's names); "" when the name does not start with a country code."""
    m = re.match(r"([a-z]{2})[-_]", name)
    return m.group(1) if m else ""


def ordered(names: list[str], home: str) -> list[str]:
    prio = ([home.lower()] if home else []) + [c for c in NEAR if c != home.lower()]
    return sorted(names, key=lambda n: (prio.index(country(n)) if country(n) in prio else len(prio), n))


def check(text: str) -> str | None:
    """None when `text` is a usable wg-quick config, else what is wrong (in the UI language)."""
    section, found = None, set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in ("interface", "peer"):
                return t("unexpected section [{name}]", name=line[1:-1].strip()[:40])
            found.add(section)
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or section is None:
            return t("not a WireGuard config")
        if section == "interface" and key == "privatekey" and _KEY.match(value):
            found.add("privatekey")
        elif section == "interface" and key == "address" and value:
            found.add("address")
        elif section == "peer" and key == "publickey" and _KEY.match(value):
            found.add("publickey")
        elif section == "peer" and key == "endpoint" and _ENDPOINT.match(value):
            found.add("endpoint")
    missing = [k for k in ("interface", "privatekey", "address", "peer", "publickey", "endpoint") if k not in found]
    return t("missing or invalid: {fields}", fields=", ".join(missing)) if missing else None


def _safe_name(filename: str) -> str:
    """The base name as a file name wireproxy's config can point to: lower case, [a-z0-9._-] only."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base[:-5] if base.lower().endswith(".conf") else base
    return re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-.")[:80] or "profile"


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", "replace")


def _from_zip(data: bytes) -> tuple[dict[str, str], list[str]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError(t("Not a valid zip file")) from None
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_MEMBERS:
        raise ValueError(t("The zip holds more than {n} files", n=MAX_MEMBERS))
    files: dict[str, str] = {}
    skipped: list[str] = []
    for info in infos:
        base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not base.lower().endswith(".conf") or base.startswith("._") or "__MACOSX/" in info.filename:
            continue
        try:
            with zf.open(info) as f:
                raw = f.read(MAX_CONF + 1)          # bounded: a lying header cannot unpack a bomb
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError, RuntimeError) as e:
            skipped.append(f"{base}: {e}")
            continue
        if len(raw) > MAX_CONF:
            skipped.append(f"{base}: {t('too large')}")
            continue
        text = _decode(raw)
        if err := check(text):
            skipped.append(f"{base}: {err}")
            continue
        name, n = _safe_name(base), 2
        while name in files:
            name, n = f"{_safe_name(base)}-{n}", n + 1
        files[name] = text
    return files, skipped


def import_upload(filename: str, data: bytes) -> tuple[str, int, list[str]]:
    """Replace the source with an uploaded .zip (collection) or .conf (dedicated profile).

    Returns (mode, number of configs, skipped files with the reason). Raises ValueError with a message for the user
    and keeps the old source when the upload holds nothing usable.
    """
    if len(data) > MAX_UPLOAD:
        raise ValueError(t("The file is larger than {n} MB", n=MAX_UPLOAD >> 20))
    lower = (filename or "").lower()
    if lower.endswith(".zip") or data[:4] == b"PK\x03\x04":
        mode = "collection"
        files, skipped = _from_zip(data)
    elif lower.endswith(".conf"):
        mode, skipped = "profile", []
        if len(data) > MAX_CONF:
            raise ValueError(t("{name}: too large", name=filename))
        if err := check(_decode(data)):
            raise ValueError(t("{name}: {error}", name=filename, error=err))
        files = {_safe_name(filename): _decode(data)}
    else:
        raise ValueError(t("Upload a .zip with WireGuard configs or one .conf file"))
    if not files:
        raise ValueError(t("No usable WireGuard config in {name}", name=filename or "upload")
                         + (f" ({skipped[0]})" if skipped else ""))
    _replace(mode, files)
    return mode, len(files), skipped


def _replace(mode: str, files: dict[str, str]) -> None:
    r = root()
    r.mkdir(parents=True, exist_ok=True)
    os.chmod(r, 0o700)
    tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=r))      # mode 700
    for name, text in files.items():
        fd = os.open(tmp / f"{name}.conf", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    for m in MODES:
        shutil.rmtree(r / m, ignore_errors=True)
    tmp.rename(r / mode)


def remove_source() -> None:
    for m in MODES:
        shutil.rmtree(root() / m, ignore_errors=True)


def secure() -> None:
    """Folder 700, files 600 (after a backup restore, which writes with the default mode)."""
    r = root()
    if not r.is_dir():
        return
    for d, _dirs, names in os.walk(r):
        os.chmod(d, 0o700)
        for n in names:
            os.chmod(os.path.join(d, n), 0o600)


# ------------------------------------------------------------------ switch and marks (setting vpn_state)
def enabled(pid: str) -> bool:
    from app.db import get_setting

    return get_setting(f"platform_{pid}_vpn") == "1"


def set_enabled(pid: str, on: bool) -> None:
    from app.db import set_setting

    set_setting(f"platform_{pid}_vpn", "1" if on else "0")


def _load() -> dict[str, Any]:
    from app.db import get_setting

    try:
        state = json.loads(get_setting(STATE_KEY) or "{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _save(state: dict[str, Any]) -> None:
    from app.db import set_setting

    set_setting(STATE_KEY, json.dumps(state, separators=(",", ":")))


def marks(pid: str) -> dict[str, tuple[float, str]]:
    """The exits this site skips: name -> (until, reason), expired ones left out."""
    now = time.time()
    raw = (_load().get(pid) or {}).get("marks") or {}
    return {n: (float(m[0]), str(m[1])) for n, m in raw.items()
            if isinstance(m, list) and len(m) == 2 and float(m[0]) > now}


def mark(pid: str, name: str, reason: str) -> None:
    state = _load()
    site = state.setdefault(pid, {})
    now = time.time()
    kept = {n: m for n, m in (site.get("marks") or {}).items() if isinstance(m, list) and len(m) == 2 and m[0] > now}
    kept[name] = [now + MARK_TTL_S.get(reason, 3600), reason]
    site["marks"] = kept
    _save(state)


def _set_current(pid: str, name: str | None) -> None:
    state = _load()
    state.setdefault(pid, {})["current"] = name
    _save(state)


def forget(pid: str | None = None) -> None:
    """Clear the marks of every site ("Forget blocks"), or of `pid` only; those sites start from the top again."""
    state = _load()
    for key, site in state.items():
        if isinstance(site, dict) and pid in (None, key):
            site["marks"] = {}
    _save(state)
    for key, tn in _tunnels.items():
        if pid in (None, key):
            tn.reset_failure()


def unmark(pid: str, name: str) -> None:
    """Drop one exit's mark for this site (the VPN test asks a dedicated profile again)."""
    state = _load()
    site = state.get(pid)
    if isinstance(site, dict) and name in (site.get("marks") or {}):
        del site["marks"][name]
        _save(state)


# ------------------------------------------------------------------ wireproxy processes
class Route(NamedTuple):
    proxy: str           # http://127.0.0.1:<port>
    gen: int             # which switch of the tunnel it belongs to; an older one changes nothing in failed()
    name: str = ""       # the exit (config) behind it


@dataclass
class _Proc:
    name: str
    port: int
    handle: Any
    users: set[str] = field(default_factory=set)

    @property
    def alive(self) -> bool:
        return self.handle.returncode is None


_libc: Any = None


def _die_with_parent() -> None:     # runs in the child between fork and exec: no imports, no locks
    if _libc is not None:
        _libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG: wireproxy never outlives a crashed app


class Runner:
    """Starts and stops wireproxy and probes its proxy; tests swap in a fake (vpn.runner = ...)."""

    def binary(self) -> str | None:
        return shutil.which(get_settings().wireproxy_bin)

    async def start(self, conf: Path, port: int) -> Any:
        global _libc
        if _libc is None:
            with suppress(OSError):
                _libc = ctypes.CDLL("libc.so.6", use_errno=True)
        binary = self.binary()
        if binary is None:
            raise OSError(t("wireproxy is not installed"))
        run = Path(tempfile.gettempdir()) / "willhaben-vpn"
        run.mkdir(mode=0o700, exist_ok=True)
        cfg, err_log = run / f"{conf.stem}.conf", run / f"{conf.stem}.log"
        cfg.write_text(f"WGConfig = {conf}\n\n[http]\nBindAddress = 127.0.0.1:{port}\n", encoding="utf-8")
        with open(err_log, "wb") as err:     # a file, not a pipe: an unread pipe would stall wireproxy
            proc = await asyncio.create_subprocess_exec(binary, "-s", "-c", str(cfg), stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=err,
                                                        preexec_fn=_die_with_parent)
        try:
            end = time.monotonic() + START_TIMEOUT_S
            while time.monotonic() < end:
                if proc.returncode is not None:
                    tail = err_log.read_text("utf-8", "replace").strip().splitlines()[-1:] if err_log.exists() else []
                    raise OSError(t("wireproxy stopped: {error}", error=(tail[0] if tail else proc.returncode)))
                try:
                    _r, w = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    await asyncio.sleep(0.1)
                    continue
                w.close()
                return proc
            raise OSError(t("wireproxy did not open its port"))
        except BaseException:          # did not start, or cancelled meanwhile (VPN test, shutdown): no orphan
            await self.stop(proc)
            raise

    async def probe(self, port: int, host: str) -> bool:
        """CONNECT host:443 through the proxy: wireproxy dials through the tunnel and answers 200 only when the site
        is reachable that way. Checks handshake and route without sending an HTTP request to the site."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            await writer.drain()
            parts = (await asyncio.wait_for(reader.readline(), PROBE_TIMEOUT_S)).split()
            return len(parts) >= 2 and parts[0].startswith(b"HTTP/1.") and parts[1] == b"200"
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def stop(self, handle: Any) -> None:
        if handle.returncode is not None:
            return
        with suppress(ProcessLookupError):
            handle.terminate()
        try:
            await asyncio.wait_for(handle.wait(), 3)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                handle.kill()
            await handle.wait()


runner = Runner()
_procs: dict[str, _Proc] = {}
_tunnels: dict[str, Tunnel] = {}
_plock_: tuple[Any, asyncio.Lock] | None = None


def _loop_lock(holder: tuple[Any, asyncio.Lock] | None) -> tuple[Any, asyncio.Lock]:
    """An asyncio.Lock of the running loop (tests run one loop per test)."""
    loop = asyncio.get_running_loop()
    return holder if holder is not None and holder[0] is loop else (loop, asyncio.Lock())


def _plock() -> asyncio.Lock:
    global _plock_
    _plock_ = _loop_lock(_plock_)
    return _plock_[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _join(name: str, pid: str) -> _Proc:
    """The running process of this config (started when needed), with `pid` among its users. OSError: no start."""
    async with _plock():
        p = _procs.get(name)
        if p is not None and not p.alive:
            _procs.pop(name, None)
            p = None
        if p is None:
            port = _free_port()
            handle = await runner.start(config_path(name), port)
            p = _procs[name] = _Proc(name, port, handle)
        p.users.add(pid)
        return p


async def _leave(name: str | None, pid: str) -> None:
    if not name:
        return
    async with _plock():
        p = _procs.get(name)
        if p is None:
            return
        p.users.discard(pid)
        if not p.users or not p.alive:
            _procs.pop(name, None)
            await runner.stop(p.handle)


# ------------------------------------------------------------------ one site's tunnel
class Tunnel:
    """One site's way out: which exit it uses, the route its client sends through, and moving on when refused."""

    def __init__(self, pid: str, host: str, home: str, label: str | None = None) -> None:
        self.pid, self.host, self.home, self.label = pid, host, (home or "").lower(), label or pid
        self.route: Route | None = None
        self.config: str | None = None
        self.gen = 0
        self.error: str | None = None
        self.failing = False         # the last ensure() found no working exit (health, settings page)
        self.since: float | None = None
        self._exhausted_until = 0.0
        self._lock_: tuple[Any, asyncio.Lock] | None = None
        self._listeners: list[Listener] = []

    def _lock(self) -> asyncio.Lock:
        self._lock_ = _loop_lock(self._lock_)
        return self._lock_[1]

    def _alive(self) -> bool:
        p = _procs.get(self.config) if self.config else None
        return p is not None and p.alive and self.pid in p.users

    def current(self) -> Route | None:
        """The route requests go through right now; None without one or when its process died (a read, no lock)."""
        return self.route if self.route is not None and self._alive() else None

    @contextmanager
    def listen(self, fn: Listener) -> Iterator[None]:
        """While the block runs, `fn(exit, event, detail)` hears of every exit this site tries, whoever asked (the VPN
        test lists them): trying | using | unreachable | broken | blocked | rate | botcheck."""
        self._listeners.append(fn)
        try:
            yield
        finally:
            self._listeners.remove(fn)

    def _tell(self, name: str, event: str, detail: str = "") -> None:
        for fn in list(self._listeners):
            try:
                fn(name, event, detail)
            except Exception as e:  # noqa: BLE001 - a display hook must never break a request
                log.debug("VPN %s listener: %s", self.pid, e)

    def reset_failure(self) -> None:
        self._exhausted_until, self.failing, self.error = 0.0, False, None

    def candidates(self, mode: str, names: list[str]) -> list[str]:
        """Exits to try, in order: not marked for this site; in a collection the ones no other site uses first; the
        current exit first again after its process died."""
        skip = marks(self.pid)
        out = [n for n in ordered(names, self.home) if n not in skip]
        if mode == "collection":
            busy = {n for n, p in _procs.items() if p.users - {self.pid}}
            out.sort(key=lambda n: n in busy)      # stable: the order stays within both groups
        if self.config in out:
            out.remove(self.config)
            out.insert(0, self.config)
        return out

    def _fail(self, message: str, *, hard: bool = True) -> PlatformError:
        """`hard`: no exit works now (health and settings show it); else more exits are left for the next request."""
        self.error, self.failing, self.route, self.config = message, hard, None, None
        return PlatformError(message)

    async def ensure(self, *, probes: int = MAX_PROBES) -> Route:
        """The route to send through, starting or switching the exit when needed. PlatformError when there is no
        working exit right now (fail closed: the caller must not send without it).

        `probes`: exits this call starts and probes at most (the VPN test passes 1, so Stop and the site's own requests
        wait for one probe at most). Cancelled meanwhile, it leaves no process behind for an exit it did not take."""
        async with self._lock():
            if self.route is not None and self._alive():
                return self.route
            if self.route is not None:           # its process died: start the same exit again
                log.info("VPN %s: wireproxy for %s stopped, starting it again", self.pid, self.config)
                await _leave(self.config, self.pid)
                self.route = None
            if time.monotonic() < self._exhausted_until:
                raise PlatformError(self.error or t("No working VPN exit"))
            mode, names = source()
            if not names:
                raise self._fail(t("The VPN is on for {name}, but no WireGuard config is uploaded (Settings → VPN)",
                                   name=self.label))
            if runner.binary() is None:
                raise self._fail(t("The VPN is on for {name}, but wireproxy is not installed", name=self.label))
            cands = self.candidates(mode, names)
            for name in cands[:probes]:
                self._tell(name, "trying")
                try:
                    proc = await _join(name, self.pid)
                except OSError as e:
                    log.warning("VPN %s: %s does not start: %s", self.pid, name, e)
                    mark(self.pid, name, "broken")
                    self._tell(name, "broken", str(e))
                    continue
                try:
                    reached = await runner.probe(proc.port, self.host)
                except asyncio.CancelledError:   # VPN test stopped, shutdown, job timeout: not this site's exit
                    await _leave(name, self.pid)
                    raise
                if reached:
                    self.gen += 1
                    self.route = Route(f"http://127.0.0.1:{proc.port}", self.gen, name)
                    self.config, self.since = name, time.time()
                    self.reset_failure()
                    _set_current(self.pid, name)
                    log.info("VPN %s: exit %s", self.pid, name)
                    self._tell(name, "using")
                    return self.route
                log.warning("VPN %s: %s does not reach %s", self.pid, name, self.host)
                mark(self.pid, name, "unreachable")
                self._tell(name, "unreachable", t("no connection to {host}", host=self.host))
                await _leave(name, self.pid)
            if len(cands) > probes:              # more to try: the next request goes on with them
                raise self._fail(t("No working VPN exit for {name} yet ({n} tried, the next request tries more)",
                                   name=self.label, n=probes), hard=False)
            self._exhausted_until = time.monotonic() + EXHAUSTED_S
            _set_current(self.pid, None)
            if mode == "profile":
                why = next(iter(marks(self.pid).values()), (0, "unreachable"))[1]
                raise self._fail(t("The VPN profile does not work for {name} right now ({reason}); upload another one "
                                   "or press “Forget blocks”", name=self.label, reason=reason_label(why)))
            raise self._fail(t("No working VPN exit for {name}: all {n} configs are blocked or unreachable",
                               name=self.label, n=len(names)))

    async def failed(self, route: Route | None, reason: str, detail: str = "") -> bool:
        """The site refused this exit (`blocked`, `rate`, the VPN test's `botcheck`) or it did not answer
        (`unreachable`): mark it for this site and leave it; the next ensure() takes the next one. A route from before
        the last switch changes nothing. `detail` ("HTTP 403", the network error) goes to the log and the listeners.
        True when the exit was marked and left; False for a route from before the last switch, or a dead process."""
        if route is None:
            return False
        async with self._lock():
            if self.route is None or route.gen != self.gen:
                return False
            name, p = self.config, _procs.get(self.config or "")
            self.route = None
            if reason == "unreachable" and p is not None and not p.alive:
                return False                     # the process died, not the exit: ensure() starts it again
            mark(self.pid, name or "", reason)
            self.config = None
            self.error = t("{name}: exit {exit} {reason}, switching", name=self.label, exit=name,
                           reason=reason_label(reason))
            log.warning("VPN %s: exit %s %s (%s), switching", self.pid, name, reason, detail or "-")
            self._tell(name or "", reason, detail)
            await _leave(name, self.pid)
            return True

    async def stop(self) -> None:
        async with self._lock():
            name, self.route, self.config = self.config, None, None
            self.reset_failure()
            await _leave(name, self.pid)


def reason_label(reason: str) -> str:
    return {"blocked": t("blocked by the site"), "rate": t("rate-limited by the site"),
            "unreachable": t("not reachable"), "broken": t("does not start"),
            "botcheck": t("bot check")}.get(reason, reason)


def tunnel_for(pid: str) -> Tunnel | None:
    """The site's tunnel while its VPN switch is on, else None (requests go out directly)."""
    if not enabled(pid):
        return None
    tn = _tunnels.get(pid)
    if tn is None:
        from app import platforms

        try:
            p = platforms.get(pid)
            host, home, label = p.api_host or p.host, p.home_country, p.name
        except Exception:  # noqa: BLE001 - a client without an adapter (tests)
            host, home, label = pid, "", pid
        tn = _tunnels[pid] = Tunnel(pid, host, home, label)
    return tn


async def stop_site(pid: str) -> None:
    if (tn := _tunnels.get(pid)) is not None:
        await tn.stop()


async def source_changed() -> None:
    """After an upload or removal: every site starts over with the new source (old exits and marks are gone)."""
    for tn in list(_tunnels.values()):
        await tn.stop()
    await shutdown()
    _save({})


async def shutdown() -> None:
    for tn in list(_tunnels.values()):
        tn.route, tn.config = None, None
    for p in list(_procs.values()):
        with suppress(Exception):
            await runner.stop(p.handle)
    _procs.clear()


def reset() -> None:
    """Tests: forget tunnels and processes (fakes; nothing to stop)."""
    global _plock_
    _tunnels.clear()
    _procs.clear()
    _plock_ = None


# ------------------------------------------------------------------ status for Settings → VPN and the health check
def status() -> dict[str, Any]:
    from app import platforms

    mode, names = source()
    counts: dict[str, int] = {}
    for n in names:
        counts[country(n) or "?"] = counts.get(country(n) or "?", 0) + 1
    sites = []
    for p in platforms.all(enabled_only=False):
        tn = _tunnels.get(p.id)
        m = marks(p.id)
        sites.append({"id": p.id, "name": p.name, "on": enabled(p.id),
                      "exit": tn.config if tn is not None and tn.route is not None else None,
                      "since": tn.since if tn is not None and tn.route is not None else None,
                      "error": tn.error if tn is not None else None, "failing": bool(tn and tn.failing),
                      "marks": sorted(((n, reason_label(r), until) for n, (until, r) in m.items()), key=lambda x: x[2],
                                      reverse=True)})
    return {"mode": mode, "count": len(names), "profile": names[0] if mode == "profile" else None,
            "countries": sorted(counts.items(), key=lambda x: (-x[1], x[0])), "binary": runner.binary() is not None,
            "sites": sites, "any_on": any(s["on"] for s in sites)}


def health() -> tuple[str, str]:
    """(state, reason) for the connection watchdog: off | ok | problem | unknown. Memory and disk only."""
    from app import platforms

    on = [p for p in platforms.all(enabled_only=False) if enabled(p.id)]
    if not on:
        return "off", t("Not used")
    if not source()[1]:
        return "problem", t("No WireGuard config uploaded")
    if runner.binary() is None:
        return "problem", t("wireproxy is not installed")
    tunnels = [(p, _tunnels.get(p.id)) for p in on]
    bad = [p.name for p, tn in tunnels if tn is not None and tn.failing]
    if bad:
        return "problem", t("No working exit for {sites}", sites=", ".join(bad))
    serving = [f"{p.name}: {tn.config}" for p, tn in tunnels if tn is not None and tn.route is not None]
    if serving:
        return "ok", ", ".join(serving)
    return "unknown", t("Not used yet")
