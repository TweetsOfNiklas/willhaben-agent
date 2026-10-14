"""WireGuard for the anonymous requests: one connection every site shares, shuffled to another server now and then.

Only the anonymous clients use it (search agents, screening, price checks, the ad viewer, the product search and
their photo downloads, Geizhals); the logged-in browser never does. Each site has its own switch (setting
`platform_<id>_vpn`; Geizhals `geizhals_vpn`, off by default).

The source is uploaded in Settings → VPN and kept in DATA_DIR/wireguard (folder 700, files 600):
  profile/<name>.conf    one dedicated profile, used as it is (tested, never swapped)
  collection/*.conf      many configs (e.g. Mullvad's zip of every server); shuffles pick among the EU ones
A new upload replaces the old source.

One connection: the server in use runs as one wireproxy process (userspace WireGuard: no root, no tun device) with an
HTTP CONNECT proxy on 127.0.0.1, and every site with its switch on sends through it (`tunnel_for(pid)` is a site's
view of it). A new server always replaces the old process: two tunnels with one key knock each other off (Mullvad's zip
has one key for all servers). The clients get the proxy explicitly; nothing sets HTTP(S)_PROXY, so Chromium never sees
it. Host names are resolved inside the tunnel (the clients send CONNECT host:port).

Shuffles (app/services/vpn_check.py: on a schedule, on demand, and when a site refuses the server) pick a random
server (`pick()`), connect it (`connect()`) and test it with every site. This module keeps what they need: the
process, the connection state (setting `vpn_conn`: server, since, last shuffle, paused sites, automatic shuffles) and
the marks of servers a site refused (setting `vpn_state`), skipped by later picks until they expire (MARK_TTL_S).

Fail closed: with the switch on and no working server, while a shuffle runs (after waiting up to SHUFFLE_WAIT_S) and
for a site paused on the current server, `Tunnel.ensure()` raises PlatformError; nothing falls back to the home IP.
"""
from __future__ import annotations

import asyncio
import ctypes
import io
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import tempfile
import time
import zipfile
from collections.abc import Collection, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from app.config import get_settings
from app.i18n import t
from app.platforms.base import PlatformError

log = logging.getLogger(__name__)

MODES = ("profile", "collection")
MARK_TTL_S = {"blocked": 24 * 3600, "rate": 3600, "unreachable": 3600, "broken": 24 * 3600,
              "botcheck": 3600}      # botcheck: a page in place of the one asked for (only the test marks it)
SHUFFLE_WAIT_S = 60            # a request waits this long for a shuffle under way, then fails (closed)
PROBE_TIMEOUT_S = 15           # WireGuard repeats a lost handshake after 5 s
START_TIMEOUT_S = 5
MAX_UPLOAD = 20 << 20
MAX_MEMBERS = 2000
MAX_CONF = 16 << 10
# EU servers only: outside the EU sites show other prices (through a Swiss exit eBay.at adds Swiss VAT, +8.1 %, to
# every price, and later checks through EU exits saw price drops that were none). Never a fallback to other servers.
EU = frozenset({"at", "be", "bg", "cy", "cz", "de", "dk", "ee", "es", "fi", "fr", "gr", "hr", "hu", "ie", "it", "lt",
                "lu", "lv", "mt", "nl", "pl", "pt", "ro", "se", "si", "sk"})
NEAR = (("at",), ("de", "cz", "sk", "hu", "si", "it"), ("pl", "nl", "be", "lu", "dk", "fr", "hr"))  # home first
GEIZHALS = "geizhals"          # not a platform, but its lookups can go through the connection too
STATE_KEY = "vpn_state"        # marks: {pid: {"marks": {server: [until, reason]}}}
CONN_KEY = "vpn_conn"
_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")       # a WireGuard key, base64 of 32 bytes
_ENDPOINT = re.compile(r"^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):\d{1,5}$")
_choice = random.choice        # tests pick deterministically


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


def pick(exclude: Collection[str] = (), skip_for: Iterable[str] = ()) -> str | None:
    """The next server to try: the profile as it is; in a collection a random EU server, nearest first (AT, then DE
    and the neighbours, then the rest of NEAR, then any EU country), not in `exclude` and not marked for a site in
    `skip_for` (when every one is, the marks are ignored). None when no server is left: never one outside the EU."""
    mode, names = source()
    if mode == "profile":
        return None if names[0] in exclude else names[0]
    eu = [n for n in names if country(n) in EU and n not in exclude]
    marked = {n for pid in skip_for for n in marks(pid)}
    pool = [n for n in eu if n not in marked] or eu
    for tier in (*NEAR, EU):
        group = [n for n in pool if country(n) in tier]
        if group:
            return _choice(group)
    return None


def eu_count(names: list[str]) -> int:
    return sum(country(n) in EU for n in names)


def allowed(name: str) -> bool:
    """A server of the source that may be used: the profile, or an EU server of the collection."""
    mode, names = source()
    return name in names and (mode == "profile" or country(name) in EU)


# ------------------------------------------------------------------ switches and marks (setting vpn_state)
def _switch(pid: str) -> str:
    return "geizhals_vpn" if pid == GEIZHALS else f"platform_{pid}_vpn"


def enabled(pid: str) -> bool:
    from app.db import get_setting

    return get_setting(_switch(pid)) == "1"


def set_enabled(pid: str, on: bool) -> None:
    from app.db import set_setting

    set_setting(_switch(pid), "1" if on else "0")


def users() -> list[tuple[str, str]]:
    """(id, name) of every site with its VPN switch on, Geizhals last."""
    from app import platforms

    out = [(p.id, p.name) for p in platforms.all(enabled_only=False) if enabled(p.id)]
    return out + ([(GEIZHALS, "Geizhals")] if enabled(GEIZHALS) else [])


def _json_setting(key: str) -> dict[str, Any]:
    from app.db import get_setting

    try:
        d = json.loads(get_setting(key) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _set_json(key: str, d: dict[str, Any]) -> None:
    from app.db import set_setting

    set_setting(key, json.dumps(d, separators=(",", ":")))


def _load() -> dict[str, Any]:
    return _json_setting(STATE_KEY)


def _save(state: dict[str, Any]) -> None:
    _set_json(STATE_KEY, state)


def marks(pid: str) -> dict[str, tuple[float, str]]:
    """The servers this site refused: name -> (until, reason), expired ones left out."""
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


def forget(pid: str | None = None) -> None:
    """Clear the marks of every site ("Forget blocks"), or of `pid` only; those sites are no longer paused either."""
    state = _load()
    for key, site in state.items():
        if isinstance(site, dict) and pid in (None, key):
            site["marks"] = {}
    _save(state)
    unpause(pid)


# ------------------------------------------------------------------ the connection's state (setting vpn_conn)
def conn_state() -> dict[str, Any]:
    """server (in use or to start again after a restart), since, shuffled (end of the last finished shuffle), paused
    {pid: [reason, detail]} (sites that refused the server: their requests fail until it changes), failing (the sites
    the server a shuffle kept as the best one fails: their marks do not steer the next pick), odd (sites
    whose bot-check-like pages are the site's change, not the servers'), auto (starts of the automatic shuffles of
    the last hour), triggered (sites that started one since the last regular shuffle), pending ([at, pid, reason,
    detail]: an automatic shuffle waiting for its turn), error (why there is no server)."""
    return _json_setting(CONN_KEY)


def update_conn(**fields: Any) -> dict[str, Any]:
    d = conn_state()
    d.update(fields)
    _set_json(CONN_KEY, d)
    return d


def paused(pid: str) -> tuple[str, str] | None:
    """(reason, detail) when this site refused the current server; None when it may use it."""
    p = (conn_state().get("paused") or {}).get(pid)
    return (str(p[0]), str(p[1])) if isinstance(p, list) and len(p) == 2 else None


def pause(pid: str, reason: str, detail: str = "") -> None:
    d = conn_state()
    d.setdefault("paused", {})[pid] = [reason, detail]
    _set_json(CONN_KEY, d)


def unpause(pid: str | None = None) -> None:
    d = conn_state()
    if d.get("paused"):
        d["paused"] = {} if pid is None else {k: v for k, v in d["paused"].items() if k != pid}
        _set_json(CONN_KEY, d)


# ------------------------------------------------------------------ the wireproxy process
class Route(NamedTuple):
    proxy: str           # http://127.0.0.1:<port>
    gen: int             # which connection it belongs to; an older one changes nothing in failed()
    name: str = ""       # the server (config) behind it


class Unreachable(Exception):
    """The server started, but the site's host cannot be reached through it."""

    def __init__(self, host: str) -> None:
        super().__init__(t("no connection to {host}", host=host))
        self.host = host


@dataclass
class _Proc:
    name: str
    port: int
    handle: Any

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
_tunnels: dict[str, Tunnel] = {}
_plock_: tuple[Any, asyncio.Lock] | None = None


def _plock() -> asyncio.Lock:
    """An asyncio.Lock of the running loop (tests run one loop per test): one start or stop at a time."""
    global _plock_
    loop = asyncio.get_running_loop()
    if _plock_ is None or _plock_[0] is not loop:
        _plock_ = (loop, asyncio.Lock())
    return _plock_[1]


_last_port = 0


def _free_port() -> int:
    """A free local port, never the one handed out last: a client keeps its transport while the proxy URL stays the
    same, and a new server must get new connections (and a new visitor)."""
    global _last_port
    for _ in range(5):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != _last_port:
            break
    _last_port = port
    return port


class Connection:
    """The one process and its route; `busy` while a shuffle (vpn_check) starts and tests servers."""

    def __init__(self) -> None:
        self.proc: _Proc | None = None
        self.route: Route | None = None
        self.gen = 0
        self.busy = False
        self.closing = False             # the app shuts down: no new process
        self._idle: tuple[Any, asyncio.Event] | None = None

    def current(self) -> Route | None:
        """The route requests go through now; None without one or when its process died (a read, no lock). During
        a shuffle the server under test (its Trials send through it; live requests wait in ensure())."""
        return self.route if self.route is not None and self.proc is not None and self.proc.alive else None

    def _event(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self._idle is None or self._idle[0] is not loop:
            ev = asyncio.Event()
            if not self.busy:
                ev.set()
            self._idle = (loop, ev)
        return self._idle[1]

    def begin(self) -> None:
        """A shuffle starts: requests wait (no await before it, so two cannot start)."""
        self.busy = True
        self._event().clear()

    def end(self) -> None:
        self.busy = False
        self._event().set()

    async def wait_idle(self, timeout: float) -> bool:
        """Wait up to `timeout` for a shuffle under way; True when none runs (any more)."""
        if not self.busy:
            return True
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._event().wait(), timeout)
        return not self.busy

    async def connect(self, name: str, host: str) -> Route:
        """`name` as the one process, `host` reachable through it (CONNECT host:443, no HTTP request): the running
        process when it is that server and alive, else the old one stops first (one key, one tunnel). OSError: it
        does not start; Unreachable: it does not reach `host` (stopped again). Cancelled meanwhile: no process left."""
        async with _plock():
            if self.closing:
                raise PlatformError(t("The app is shutting down"))
            if self.current() is not None and self.proc.name == name:
                return self.route
            await self._stop()
            port = _free_port()
            handle = await runner.start(config_path(name), port)
            try:
                reached = await runner.probe(port, host)
            except BaseException:
                await runner.stop(handle)
                raise
            if not reached:
                await runner.stop(handle)
                raise Unreachable(host)
            self.proc, self.gen = _Proc(name, port, handle), self.gen + 1
            self.route = Route(f"http://127.0.0.1:{port}", self.gen, name)
            log.info("VPN: server %s", name)
            return self.route

    async def _stop(self) -> None:
        proc, self.proc, self.route = self.proc, None, None
        if proc is not None:
            await runner.stop(proc.handle)

    async def disconnect(self) -> None:
        async with _plock():
            await self._stop()


conn = Connection()


def _shuffle(pid: str, reason: str, detail: str) -> bool:
    """Ask for a shuffle (app/services/vpn_check.py tests the sites through their adapters, so it lives there); True
    when one runs now, False when its guards let it wait or none can run."""
    from app.services import vpn_check

    return vpn_check.trigger(pid, reason, detail)


# ------------------------------------------------------------------ one site's view of the connection
class Tunnel:
    """The route a site sends through (the shared one), and its report when the site refuses the server."""

    def __init__(self, pid: str, host: str, label: str | None = None) -> None:
        self.pid, self.host, self.label = pid, host, label or pid

    def current(self) -> Route | None:
        return conn.current()

    def odd(self) -> bool:
        """A shuffle found this site answering with bot-check-like pages through several servers: the site changed,
        not the servers, so its live bot checks refuse no server until the next regular shuffle."""
        return self.pid in (conn_state().get("odd") or [])

    async def ensure(self) -> Route:
        """The route to send through. Waits for a shuffle under way (up to SHUFFLE_WAIT_S); after a restart or a
        crash of wireproxy starts the stored server again; asks for a shuffle when there is none or it does not
        answer. PlatformError when this site has no working server now (fail closed: the caller must not send)."""
        error = ""
        for first in (True, False):
            if not await conn.wait_idle(SHUFFLE_WAIT_S):
                raise PlatformError(t("The VPN is changing its server; {name} waits for it", name=self.label))
            self._check()
            if (route := conn.current()) is not None:
                return route
            server = conn_state().get("server")
            if not first:
                break
            if server and allowed(server):
                try:
                    return await conn.connect(server, self.host)
                except (OSError, Unreachable) as e:
                    reason, error = ("unreachable" if isinstance(e, Unreachable) else "broken"), str(e)
                log.warning("VPN %s: server %s %s again: %s", self.pid, server, reason, error)
                mark(self.pid, server, reason)
                update_conn(server=None, error=t("{server} {reason}", server=server, reason=reason_label(reason)))
                if not _shuffle(self.pid, reason, error):
                    break
            elif not _shuffle(self.pid, "none", ""):
                break
        why = conn_state().get("error") or error
        raise PlatformError(t("No working VPN server for {name} right now", name=self.label)
                            + (f" ({why})" if why else ""))

    def _check(self) -> None:
        if not source()[1]:
            raise PlatformError(t("The VPN is on for {name}, but no WireGuard config is uploaded (Settings → VPN)",
                                  name=self.label))
        if runner.binary() is None:
            raise PlatformError(t("The VPN is on for {name}, but wireproxy is not installed", name=self.label))
        if (p := paused(self.pid)) is not None:
            raise PlatformError(t("{name} refused the VPN server {server} ({reason}); its requests pause until the "
                                  "server changes", name=self.label, server=conn_state().get("server") or "?",
                                  reason=reason_label(p[0])))

    async def failed(self, route: Route | None, reason: str, detail: str = "") -> bool:
        """The site refused the server (`blocked`, `rate`, `botcheck`) or it did not answer (`unreachable`): mark
        the server for the site, pause the site and ask for a shuffle (whose guards may let it wait). Nothing for a
        route of an earlier connection, during a shuffle (its test decides), for a site paused already, or when the
        process died (ensure() starts it again). `detail` ("HTTP 403", the network error) goes to the log and the
        page. True when the server was marked."""
        if route is None or conn.busy or route.gen != conn.gen or conn.route is None or paused(self.pid):
            return False
        if reason == "botcheck" and self.odd():
            return False
        if reason == "unreachable" and conn.current() is None:
            return False
        mark(self.pid, route.name, reason)
        pause(self.pid, reason, detail)
        log.warning("VPN %s: server %s %s (%s), asking for another", self.pid, route.name, reason, detail or "-")
        _shuffle(self.pid, reason, detail)
        return True


def describe(detail: str) -> str:
    """A refusal's detail for the page: common network errors in the viewer's language, "HTTP 403" in one piece."""
    low = detail.lower()
    if m := re.fullmatch(r"(?:no connection to|keine Verbindung zu) (\S+)", detail):
        return t("no connection to {host}", host=m.group(1))
    if "timed out" in low or "timeout" in low:
        return t("no answer in time")
    if "refused" in low:
        return t("connection refused")
    if "reset" in low or "closed" in low:
        return t("connection dropped")
    if "ssl" in low or "tls" in low or "certificate" in low:
        return t("TLS error")
    return detail.replace("HTTP ", "HTTP\u00a0")


def reason_label(reason: str) -> str:
    return {"blocked": t("blocked by the site"), "rate": t("rate-limited by the site"),
            "unreachable": t("not reachable"), "broken": t("does not start"),
            "botcheck": t("bot check")}.get(reason, reason)


def tunnel_for(pid: str) -> Tunnel | None:
    """The site's view of the connection while its VPN switch is on, else None (requests go out directly)."""
    if not enabled(pid):
        return None
    tn = _tunnels.get(pid)
    if tn is None:
        if pid == GEIZHALS:
            host, label = "geizhals.at", "Geizhals"
        else:
            from app import platforms

            try:
                p = platforms.get(pid)
                host, label = p.api_host or p.host, p.name
            except Exception:  # noqa: BLE001 - a client without an adapter (tests)
                host, label = pid, pid
        tn = _tunnels[pid] = Tunnel(pid, host, label)
    return tn


async def site_off(pid: str) -> None:
    """A switch went off: that site is no longer paused, and the process stops once no site uses it."""
    unpause(pid)
    if not users() and not conn.busy:
        await conn.disconnect()


async def source_changed() -> None:
    """After an upload or removal: the old server, its marks and the connection state are gone."""
    await conn.disconnect()
    _save({})
    _set_json(CONN_KEY, {})


async def shutdown() -> None:
    """Stop the process, after a start under way (it holds the lock); none starts after this."""
    conn.closing = True
    async with _plock():
        with suppress(Exception):
            await conn._stop()


def reset() -> None:
    """Tests: forget the connection and the tunnels (fakes; nothing to stop)."""
    global _plock_, conn
    _tunnels.clear()
    conn = Connection()
    _plock_ = None


# ------------------------------------------------------------------ status for Settings → VPN and the health check
def status() -> dict[str, Any]:
    from app import platforms

    mode, names = source()
    counts: dict[str, int] = {}
    for n in names:
        counts[country(n) or "?"] = counts.get(country(n) or "?", 0) + 1
    st = conn_state()

    def row(pid: str, name: str) -> dict[str, Any]:
        p = paused(pid)
        m = marks(pid)
        return {"id": pid, "name": name, "on": enabled(pid),
                "paused": {"reason": reason_label(p[0]), "detail": describe(p[1])} if p else None,
                "marks": sorted(((n, reason_label(r), until) for n, (until, r) in m.items()), key=lambda x: x[2],
                                reverse=True)}

    sites = [row(p.id, p.name) for p in platforms.all(enabled_only=False)]
    gz = row(GEIZHALS, "Geizhals")
    return {"mode": mode, "count": len(names), "eu": eu_count(names) if mode == "collection" else len(names),
            "profile": names[0] if mode == "profile" else None,
            "countries": sorted(counts.items(), key=lambda x: (-x[1], x[0])), "binary": runner.binary() is not None,
            "server": st.get("server"), "since": st.get("since"), "connected": conn.current() is not None,
            "shuffling": conn.busy, "error": st.get("error") or "", "sites": sites, "geizhals": gz,
            "any_on": any(s["on"] for s in sites) or gz["on"]}


def health() -> tuple[str, str]:
    """(state, reason) for the connection watchdog: off | ok | problem | unknown. Memory and settings only."""
    on = users()
    if not on:
        return "off", t("Not used")
    if not source()[1]:
        return "problem", t("No WireGuard config uploaded")
    if runner.binary() is None:
        return "problem", t("wireproxy is not installed")
    if conn.busy:
        return "unknown", t("Changing the server")
    st = conn_state()
    stopped = [name for pid, name in on if paused(pid)]
    if stopped:
        return "problem", t("{sites} paused: refused {server}", sites=", ".join(stopped),
                            server=st.get("server") or "?")
    if conn.current() is not None:
        return "ok", conn.current().name
    if st.get("server"):
        return "ok", t("{server}, starts with the next request", server=st["server"])
    if st.get("error"):
        return "problem", st["error"]
    return "unknown", t("Not used yet")
