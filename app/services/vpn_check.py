"""Shuffles of the VPN connection (app/platforms/vpn.py) and the test after each: a random server, one cheap real
request per site through it, and the server stays when every site answers.

Triggers: the schedule (every `vpn_shuffle_h` hours after the last shuffle, default 6; tick() looks every minute and
the time is stored, so a restart keeps the schedule), "Shuffle now" in Settings → VPN, new configs, and a site that
refuses the server or gets no answer between two shuffles (Tunnel.failed(), or a request that finds no server). The
automatic ones are guarded against storms: AUTO_GAP_S after the last shuffle and AUTO_PER_HOUR an hour; one that may
not run yet waits (`pending`) and tick() starts it when its turn comes. One shuffle at a time; requests wait for it
(vpn.SHUFFLE_WAIT_S) or fail closed.

Each server (vpn.pick(): random, EU only, nearest first, none that a site with the VPN on refused recently) is started
and probed, then every site with the VPN on (and Geizhals when its switch is on) sends one request through it, side by
side, each through its own adapter, client, limiter and checks under a Trial (httpclient: that server only, never
directly). An answer counts, also the site's own error (not the server's fault); a refusal (blocked, rate, no answer,
a bot check in place of the result list) marks the server for that site. When every site answers, the server stays.
Else the next one, up to MAX_SERVERS; when none suits all, the one most sites answer through stays and the sites it
fails pause (fail closed) until the next shuffle. A dedicated profile is tested, never swapped.

A site whose limiter pauses after a block, or whose hourly budget cannot afford another request (per shuffle a quarter
of its hourly budget and half of what the hour has left, never past BUDGET_FULL of it), is not tested on that server:
it counts neither for nor against it, and the page says why. eBay's warm-up (vpn_test_requests) and Kleinanzeigen's
credential errors (NotTheExit) keep their meaning.

State in memory while it runs (the page polls it) and in the setting `vpn_test` (the last shuffle) for reloads and
restarts; the connection itself (server, since, paused sites, the end of the last shuffle) in vpn's `vpn_conn`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from app import platforms
from app.db import get_setting, set_setting
from app.i18n import lang, t
from app.platforms import vpn
from app.platforms.base import Blocked, NotTheExit, Platform
from app.platforms.httpclient import ExitMoved, Trial, trial
from app.platforms.ratelimit import RateLimiter
from app.services import jobs
from app.willhaben import replay

log = logging.getLogger(__name__)

KEYWORD = "fahrrad"       # every site answers it; an empty result list counts as an answer too
MAX_SERVERS = 10          # servers one shuffle tries
BUDGET_FULL = 0.9         # no test request that could take a site past this share of its hourly budget
BOTCHECK_STREAK = 3       # bot-check pages through this many servers in a row: the site changed, not the servers
AUTO_GAP_S = 300          # an automatic shuffle starts at least this long after the end of the last one
AUTO_PER_HOUR = 3         # and at most this many an hour
INTERVAL_H = 6            # default of the setting vpn_shuffle_h
INTERVAL_KEY = "vpn_shuffle_h"
STOP_WAIT_S = 10          # cancel(): longest wait for the shuffle to end
STATE_KEY = "vpn_test"
ANSWERED = ("ok", "site_error", "odd_page")
REFUSED = ("blocked", "rate", "unreachable", "botcheck")


@dataclass
class Answer:
    """One site through one server."""
    verdict: str = "waiting"   # waiting | asking | ANSWERED | REFUSED | budget | paused | off | error
    detail: str = ""           # "HTTP 403", the network error, the site's message
    requests: int = 0
    secs: float | None = None
    params: dict[str, Any] = field(default_factory=dict)   # budget: used, per_hour, why; site_error: fix


@dataclass
class Server:
    name: str
    status: str = "starting"   # starting | testing | done
    problem: str = ""          # broken | unreachable: no site was asked
    detail: str = ""
    answers: dict[str, Answer] = field(default_factory=dict)
    secs: float | None = None

    def refused(self) -> list[str]:
        return [pid for pid, a in self.answers.items() if a.verdict in REFUSED]

    def answered(self) -> int:
        return sum(a.verdict in ANSWERED for a in self.answers.values())

    def usable(self) -> bool:
        """Some site may use it: it answered, or it was not tested there."""
        return not self.problem and any(a.verdict not in REFUSED for a in self.answers.values())


@dataclass
class Site:
    pid: str
    name: str
    requests: int = 0          # sent by this shuffle


@dataclass
class Shuffle:
    trigger: str               # schedule | manual | auto | new
    sites: list[Site]
    by: str = ""               # auto: the site that refused the server or found none
    why: str = ""              # its reason: blocked | rate | unreachable | broken | none
    detail: str = ""
    profile: bool = False      # a dedicated profile: tested, never swapped
    before: str | None = None  # the server before
    servers: list[Server] = field(default_factory=list)
    kept: str | None = None
    status: str = "running"    # running | done | failed | stopped | interrupted
    reason: str = ""           # failed: no_eu | none; stopped: uploaded | removed | interrupted
    started: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    discarded: bool = field(default=False, repr=False, compare=False)    # its configs are gone: never saved again


@dataclass
class Target:
    """What a shuffle asks: a site's search, or Geizhals' (not a platform, its own switch)."""
    pid: str
    name: str
    host: str                  # probed when a server starts
    limiter: RateLimiter
    cost: int                  # requests one ask may send (eBay: its warm-up)
    ask: Callable[[], Awaitable[Any]]

    def on(self) -> bool:
        return vpn.enabled(self.pid) and (self.pid == vpn.GEIZHALS or platforms.enabled(self.pid))


_run: Shuffle | None = None


# ------------------------------------------------------------------ who, when, guards
def targets() -> list[Target]:
    """The sites switched on under Platforms (willhaben always is) with their VPN switch on, then Geizhals."""
    out = [Target(p.id, p.name, p.api_host or p.host, p.limiter, p.vpn_test_requests, _searcher(p))
           for p in platforms.all() if vpn.enabled(p.id)]
    if vpn.enabled(vpn.GEIZHALS):
        from app.services import geizhals

        out.append(Target(vpn.GEIZHALS, "Geizhals", "geizhals.at", geizhals._limiter, 1,
                          lambda: geizhals.search(KEYWORD, limit=1)))
    return out


def _searcher(p: Platform) -> Callable[[], Awaitable[Any]]:
    return lambda: p.search(p.keyword_search(KEYWORD), rows=1)


def blocker() -> str | None:
    """Why no shuffle can start now; None when one can."""
    if replay.enabled():
        return t("Not available in the offline replay: the test needs real requests")
    if not vpn.source()[1]:
        return t("Upload WireGuard configs first")
    if vpn.runner.binary() is None:
        return t("wireproxy is not installed")
    if not targets():
        return t("Switch the VPN on for a site first")
    return None


def interval_h() -> int:
    try:
        h = int(get_setting(INTERVAL_KEY) or INTERVAL_H)
    except ValueError:
        return INTERVAL_H
    return min(max(h, 1), 168)


def set_interval(h: int) -> None:
    set_setting(INTERVAL_KEY, str(min(max(int(h), 1), 168)))


def next_at() -> float | None:
    """When the next shuffle starts: a pending automatic one, else the regular one; None when none is due yet
    (never shuffled: the next tick)."""
    st = vpn.conn_state()
    if p := _pending(st):
        return p[0]
    return float(st["shuffled"]) + interval_h() * 3600 if st.get("shuffled") else None


def _pending(st: dict[str, Any]) -> tuple[float, str, str, str] | None:
    p = st.get("pending")
    if isinstance(p, list) and len(p) == 4:
        return float(p[0]), str(p[1]), str(p[2]), str(p[3])
    return None


def _auto_at(st: dict[str, Any], now: float) -> float:
    """The earliest time an automatic shuffle may start: AUTO_GAP_S after the last shuffle ended, and no more than
    AUTO_PER_HOUR in an hour."""
    at = float(st.get("shuffled") or 0) + AUTO_GAP_S
    recent = sorted(float(x) for x in st.get("auto") or [] if now - float(x) < 3600)
    if len(recent) >= AUTO_PER_HOUR:
        at = max(at, recent[-AUTO_PER_HOUR] + 3600)
    return at


def running() -> bool:
    return _run is not None and _run.task is not None and not _run.task.done()


def trigger(pid: str, reason: str, detail: str = "") -> bool:
    """A site refused the server (reason blocked | rate | unreachable | broken) or found none (none): shuffle now, or
    once the guards allow it (pending, started by tick()). True when one runs now."""
    if running():
        return True
    if blocker():
        return False
    st, now = vpn.conn_state(), time.time()
    at = _auto_at(st, now)
    if at > now:
        if not _pending(st):
            vpn.update_conn(pending=[at, pid, reason, detail])
            log.info("VPN: %s %s, the next shuffle may start in %.0f s", pid, reason, at - now)
        return False
    return start("auto", by=pid, why=reason, detail=detail) is None


async def tick() -> None:
    """Scheduler, every minute: a pending automatic shuffle whose turn came, else the regular one when it is due."""
    if running() or blocker():
        return
    st, now = vpn.conn_state(), time.time()
    if (p := _pending(st)) is not None:
        if p[0] <= now:
            start("auto", by=p[1], why=p[2], detail=p[3])
        return
    if now >= float(st.get("shuffled") or 0) + interval_h() * 3600:
        start("schedule")


# ------------------------------------------------------------------ starting and stopping
def start(trigger: str = "manual", *, by: str = "", why: str = "", detail: str = "") -> str | None:
    """Shuffle in the background (the header lists the job); None when it started, else why not. No await before the
    connection is busy: two clicks, two tabs or two refusals cannot start two."""
    global _run
    if running():
        return t("The VPN is changing its server")
    if reason := blocker():
        return reason
    tgs = targets()
    st, now = vpn.conn_state(), time.time()
    auto = [x for x in st.get("auto") or [] if now - float(x) < 3600] + ([now] if trigger == "auto" else [])
    vpn.update_conn(pending=None, auto=auto)
    _run = Shuffle(trigger, [Site(tg.pid, tg.name) for tg in tgs], by=by, why=why, detail=detail,
                   profile=vpn.source()[0] == "profile", before=st.get("server"))
    vpn.conn.begin()
    _save(_run)
    _run.task = asyncio.create_task(_main(_run, tgs))
    return None


async def cancel(reason: str) -> None:
    """New or removed configs, app shutdown: cancel now and wait for the shuffle to end (≤ STOP_WAIT_S)."""
    if not running():
        return
    run = _run
    run.reason = reason
    run.task.cancel()
    await asyncio.wait({run.task}, timeout=STOP_WAIT_S)


async def source_changed(reason: str) -> None:
    """Before vpn.source_changed(): cancel a running shuffle (its servers are about to go) and forget the last one
    (it names servers of the old configs), also when the cancel fails."""
    try:
        await cancel(reason)
    finally:
        clear()


async def shutdown() -> None:
    await cancel("interrupted")


def clear() -> None:
    """Forget the last shuffle. One still winding down after cancel() saves nothing when it ends."""
    global _run
    if _run is not None:
        _run.discarded = True
        if not running():
            _run = None
    set_setting(STATE_KEY, "")


def reset() -> None:
    """Tests: every test has its own event loop and database."""
    global _run
    _run = None


def _stop_verdict(reason: str) -> str:
    return "interrupted" if reason == "interrupted" else "stopped"


# ------------------------------------------------------------------ the shuffle
async def _main(sh: Shuffle, tgs: list[Target]) -> None:
    global _run
    label = t("VPN test") if sh.profile else t("VPN server change")
    try:
        async with jobs.track("vpn-shuffle", label, lane="vpn", link="/settings/vpn",
                              quiet=sh.trigger != "manual") as job:
            try:
                await _servers(sh, tgs, job)
                await _keep(sh, tgs[0].host)
            except asyncio.CancelledError:      # cancel(): new configs, shutdown
                sh.status = _stop_verdict(sh.reason)
                with suppress(Exception):
                    await vpn.conn.disconnect()   # the server under test; a request starts the stored one again
            job.result = _summary(sh)
            job.problems = len(_paused_sites(sh)) + (sh.status == "failed")
            if job.problems:
                job.quiet = False
    except Exception as e:  # noqa: BLE001 - the connection must leave the busy state whatever happened
        log.warning("VPN shuffle: %s", e)
        sh.status, sh.reason = "failed", _short(e)
        with suppress(Exception):
            await vpn.conn.disconnect()           # whatever ran untested; a request starts the stored server again
    finally:
        sh.finished = time.time()
        if sh.status == "running":
            sh.status = "interrupted"
        if not sh.discarded:
            vpn.update_conn(shuffled=sh.finished)
            _save(sh)
        vpn.conn.end()
        if sh.discarded and _run is sh:
            _run = None


async def _servers(sh: Shuffle, tgs: list[Target], job: jobs.Job) -> None:
    """Server after server until every site answers through one (the module docstring)."""
    tried: set[str] = set()
    skip = [tg.pid for tg in tgs]
    caps = {tg.pid: _cap(tg.limiter, tg.cost) for tg in tgs}
    sites = {s.pid: s for s in sh.sites}
    streak = {tg.pid: 0 for tg in tgs}
    most = 1 if sh.profile else MAX_SERVERS
    while len(sh.servers) < most:
        name = vpn.pick(tried | {sh.before or ""}, skip) or vpn.pick(tried, skip)   # another one when there is one
        if name is None:
            break
        tried.add(name)
        srv = Server(name)
        sh.servers.append(srv)
        job.step = t("Server {n}: {server}", n=len(sh.servers), server=name)
        t0 = time.monotonic()
        try:
            route = await vpn.conn.connect(name, tgs[0].host)
        except (OSError, vpn.Unreachable) as e:
            srv.problem = "unreachable" if isinstance(e, vpn.Unreachable) else "broken"
            srv.detail, srv.status, srv.secs = _short(e), "done", round(time.monotonic() - t0, 1)
            log.warning("VPN shuffle: %s %s: %s", name, srv.problem, e)
            for tg in tgs:
                vpn.mark(tg.pid, name, srv.problem)
            continue
        srv.status = "testing"
        srv.answers = {tg.pid: Answer() for tg in tgs}
        tasks = [asyncio.create_task(_one(srv, route, tg, sites[tg.pid], caps[tg.pid], streak)) for tg in tgs]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await asyncio.wait(tasks)
            raise
        srv.status, srv.secs = "done", round(time.monotonic() - t0, 1)
        log.info("VPN shuffle: %s answered by %d of %d (%s)", name, srv.answered(), len(tgs),
                 ", ".join(f"{p}: {a.verdict}" for p, a in srv.answers.items()))
        if not srv.refused():
            break


async def _one(srv: Server, route: vpn.Route, tg: Target, site: Site, cap: tuple[int, str],
               streak: dict[str, int]) -> None:
    try:
        a = await _ask(srv, route, tg, site, cap, streak)
    except Exception as e:  # noqa: BLE001 - one site's surprise must not end the others' tests
        log.warning("VPN test of %s through %s: %s", tg.pid, srv.name, e)
        a = Answer("error", _short(e))
    srv.answers[tg.pid] = a
    if a.verdict in REFUSED:
        vpn.mark(tg.pid, srv.name, a.verdict)


async def _ask(srv: Server, route: vpn.Route, tg: Target, site: Site, cap: tuple[int, str],
               streak: dict[str, int]) -> Answer:
    """One site's request through `route` under a Trial, and what it says about the server."""
    lim, cost = tg.limiter, tg.cost
    if not tg.on():
        return Answer("off")
    if streak[tg.pid] >= BOTCHECK_STREAK:
        return Answer("odd_page", params={"n": streak[tg.pid]})     # asked no more: not the servers
    if why := _paused(lim):
        return Answer("paused", why)
    if site.requests + cost > cap[0] or _budget_full(lim, cost):
        return Answer("budget", params={"used": lim.last_hour, "per_hour": lim.per_hour,
                                        "why": "full" if _budget_full(lim, cost) else cap[1], "n": site.requests})
    srv.answers[tg.pid] = Answer("asking")
    tr, t1, exc = Trial(route, limit=cost), time.monotonic(), None
    with trial(tr):
        try:
            await tg.ask()
        except Exception as e:  # noqa: BLE001 - PlatformError, GeizhalsError: judged below
            exc = e
    site.requests += tr.sent
    secs = round(time.monotonic() - t1, 1)
    v = _verdict(tg, tr, exc)
    if v == "botcheck":
        streak[tg.pid] += 1
        if streak[tg.pid] >= BOTCHECK_STREAK:
            return Answer("odd_page", _short(exc), tr.sent, secs, {"n": streak[tg.pid]})
    else:
        streak[tg.pid] = 0
    if v in ("blocked", "rate", "unreachable"):
        detail = tr.detail
    elif v == "site_error":
        detail = _short(exc)
    elif v in ("botcheck", "paused", "error"):
        detail = _short(exc) or (t("answered without a request (cache or replay)") if v == "error" else "")
    else:
        detail = ""
    return Answer(v, detail, tr.sent, secs, {"fix": "settings"} if isinstance(exc, NotTheExit) else {})


def _verdict(tg: Target, tr: Trial, exc: BaseException | None) -> str:
    """What one try says about the server, checked in this order."""
    if not tg.on():
        return "off"                    # also the Trial's "VPN is off": nothing went out directly
    if isinstance(exc, ExitMoved):
        return "unreachable"            # wireproxy stopped before the request went out
    if tr.refused:
        return tr.refused               # blocked | rate | unreachable
    if isinstance(exc, Blocked) and not exc.sent:
        return "paused"                 # the site's own limiter: a pause after a block, or the hour's budget
    if not tr.sent:
        return "error"                  # nothing went out: proves nothing about the server
    if exc is None:
        return "ok" if vpn.conn.current() == tr.route else "unreachable"
    if isinstance(exc, NotTheExit):
        return "site_error"             # e.g. rotated credentials: every server would get the same
    if tr.challenge or (tr.status == 200 and "json" not in tr.ctype):
        return "botcheck"               # Cloudflare's challenge (any status) or a page in place of the result list
    return "site_error"                 # the site answered through the server with an error (5xx, a JSON error)


async def _keep(sh: Shuffle, host: str) -> None:
    """The server that stays: the first every site answers through, else the one most answer through (it starts again
    when another one ran after it). The sites it fails pause; none usable: no server, requests fail closed."""
    tested = [s for s in sh.servers if s.usable()]
    full = [s for s in tested if not s.refused()]
    ranked = full[:1] or sorted(tested, key=lambda s: -s.answered())      # stable: nearer and earlier first
    for srv in ranked:
        try:
            await vpn.conn.connect(srv.name, host)
        except (OSError, vpn.Unreachable) as e:
            log.warning("VPN shuffle: %s does not start again: %s", srv.name, e)
            continue
        st = vpn.conn_state()
        sh.kept, sh.status = srv.name, "done"
        paused = {pid: [a.verdict, a.detail] for pid, a in srv.answers.items() if a.verdict in REFUSED}
        vpn.update_conn(server=srv.name, paused=paused, error="",
                        since=st.get("since") if st.get("server") == srv.name and st.get("since") else time.time())
        if not vpn.users():
            await vpn.conn.disconnect()          # switched off meanwhile
        return
    await vpn.conn.disconnect()
    sh.status = "failed"
    sh.reason = "none" if sh.servers else ("profile" if sh.profile else "no_eu")
    vpn.update_conn(server=None, since=None, paused={}, error=_failed_text(sh))


# ------------------------------------------------------------------ budgets and pauses
def _paused(lim: RateLimiter) -> str:
    """The limiter would refuse before sending: a pause after a block longer than it waits."""
    if lim.cooling_down_s > lim.max_wait_s:
        return t("{name} pause after a block, {s} s left", name=lim.name, s=f"{lim.cooling_down_s:.0f}")
    return ""


def _cap(lim: RateLimiter, cost: int) -> tuple[int, str]:
    """Requests one site may send in one shuffle, and which rule sets it: a quarter of its hourly budget (at least one
    server's worth), never more than half of what the hour has left; without a budget MAX_SERVERS servers' worth."""
    if not lim.per_hour:
        return MAX_SERVERS * cost, "servers"
    quarter, half_left = max(cost, lim.per_hour // 4), (lim.per_hour - lim.last_hour) // 2
    return (quarter, "quarter") if quarter <= half_left else (half_left, "left")


def _budget_full(lim: RateLimiter, cost: int) -> bool:
    """One more try could take the site past BUDGET_FULL of its hour (the agents' requests count too)."""
    return bool(lim.per_hour) and lim.last_hour + cost > lim.per_hour * BUDGET_FULL


def _short(exc: BaseException | None) -> str:
    return (str(exc) or type(exc).__name__)[:200] if exc is not None else ""


# ------------------------------------------------------------------ persistence
def _save(sh: Shuffle) -> None:
    if sh.discarded:
        return
    d = {f.name: getattr(sh, f.name) for f in fields(sh) if f.name not in ("task", "discarded")}
    d.update(sites=[asdict(s) for s in sh.sites], servers=[asdict(s) for s in sh.servers])
    set_setting(STATE_KEY, json.dumps(d, separators=(",", ":")))


def _stored() -> Shuffle | None:
    """The last shuffle from the setting (after a restart)."""
    try:
        d = json.loads(get_setting(STATE_KEY) or "null")
        if not isinstance(d, dict) or "servers" not in d:
            return None                 # nothing, or a test of the per-site exits before the shuffles
        d["sites"] = [Site(**x) for x in d.get("sites") or []]
        d["servers"] = [Server(**{**x, "answers": {p: Answer(**a) for p, a in (x.get("answers") or {}).items()}})
                        for x in d.get("servers") or []]
        sh = Shuffle(**d)
    except (ValueError, TypeError):
        return None
    if sh.status == "running" and not (sh is _run and running()):
        sh.status, sh.reason = "interrupted", "interrupted"     # stored while it ran, then the app restarted
    return sh


# ------------------------------------------------------------------ the view (Jinja global vpn_test)
def view() -> dict[str, Any]:
    """For settings/_vpn_test.html, in the viewer's language: the connection now, the schedule and the last shuffle
    (this process's, else the stored one) with one row per site and per server tried."""
    sh, live = _run or _stored(), running()
    st, mode = vpn.conn_state(), vpn.source()[0]
    pend, nxt = _pending(st), next_at()
    out: dict[str, Any] = {
        "live": live, "blocked": None if live else blocker(), "profile": mode == "profile",
        "server": st.get("server"), "since": _dt(st.get("since")), "connected": vpn.conn.current() is not None,
        "error": st.get("error") or "", "interval": interval_h(), "next_at": _dt(nxt),
        "next_due": nxt is not None and nxt <= time.time(), "pending": _pending_text(pend) if pend else "",
        "run": sh is not None, "progress": "", "elapsed": 0, "summary": "", "tone": "", "trigger": "",
        "finished_at": None, "rows": [], "servers": []}
    if sh is None:
        return out
    current = sh.servers[-1] if live and sh.servers else None
    shown = current or next((s for s in sh.servers if s.name == sh.kept), None)
    names = {s.pid: s.name for s in sh.sites}
    out.update(progress=_progress(sh) if live else "", elapsed=int(time.time() - sh.started),
               summary="" if live else _summary(sh), tone=_tone(sh), trigger=_trigger_text(sh, names),
               finished_at=_dt(sh.finished or sh.started),
               rows=[_row(sh, s, shown, live) for s in sh.sites],
               servers=[_server_row(sh, srv, names, live) for srv in sh.servers])
    return out


def _row(sh: Shuffle, s: Site, srv: Server | None, live: bool) -> dict[str, Any]:
    """One site on the server under test (live) or the server kept. Without a message the page shows it as a chip."""
    a = srv.answers.get(s.pid) if srv is not None else None
    row = {"pid": s.pid, "name": s.name, "label": "", "tone": "", "message": "", "spinner": False, "action": None}
    if live and (a is None or a.verdict in ("waiting", "asking")):
        if a is not None and a.verdict == "asking":
            row["spinner"] = True
        else:
            row["label"] = t("waiting")
        return row
    if a is None:
        if sh.status == "failed" and sh.servers:
            row.update(_overall(sh, s))
        return row                                           # stopped: the summary says it
    row["label"], row["tone"] = _pill(a.verdict)
    row["message"] = _message(s, a, srv.name, live)
    if a.params.get("fix") == "settings" and s.pid != vpn.GEIZHALS:
        row["action"] = {"label": t("Open {site} settings", site=s.name), "href": f"/settings/{s.pid}"}
    return row


def _overall(sh: Shuffle, s: Site) -> dict[str, Any]:
    """A site after a shuffle that kept no server: what the servers said."""
    said = [srv.answers[s.pid] for srv in sh.servers if s.pid in srv.answers]
    refused = [a for a in said if a.verdict in REFUSED]
    if said and len(refused) == len(said):
        return {"label": t("Refused"), "tone": "bad",
                "message": t("Refused every server tried ({n}).", n=len(refused))}
    return {"label": t("Not tested"), "tone": "", "message": t("No server got as far as asking {site}.", site=s.name)}


def _pill(v: str) -> tuple[str, str]:
    if v == "ok":
        return t("Works"), "ok"
    if v in REFUSED:
        return t("Refused"), "bad"
    if v in ("site_error", "odd_page"):
        return t("Site error"), "warn"
    if v == "off":
        return t("Off"), ""
    if v == "error":
        return t("Error"), "warn"
    return t("Not tested"), ""                               # budget | paused


def _message(s: Site, a: Answer, server: str, live: bool) -> str:
    site, v, p = s.name, a.verdict, a.params
    if v == "ok":
        return ""
    if v in REFUSED:
        msg = vpn.reason_label(v) + (f" ({_nb(a.detail)})" if a.detail else "")
        msg = msg[:1].upper() + msg[1:]
        return msg if live else msg + ". " + t("Its requests pause until the next shuffle.")
    if v == "site_error":
        return t("{site} answers through {server}, but with an error: {error}. That is not the server's fault.",
                 site=site, server=server, error=a.detail)
    if v == "odd_page":
        return t("{site} answered through {n} servers in a row with a page the app does not know. That points to a "
                 "change on the site, not to the VPN.", site=site, n=p.get("n", BOTCHECK_STREAK))
    if v == "budget":
        if p.get("why") == "full":
            return t("Not tested: {site} has used {used} of its {per_hour} requests this hour; the rest stays for the "
                     "search agents.", site=site, used=p.get("used", 0), per_hour=p.get("per_hour", 0))
        return t("Not tested: one shuffle sends {site} at most a quarter of its {per_hour} requests per hour and half "
                 "of what is left ({n} sent).", site=site, per_hour=p.get("per_hour", 0), n=p.get("n", 0)) \
            if p.get("per_hour") else t("Not tested: this shuffle sent {site} as many requests as it may ({n}).",
                                         site=site, n=p.get("n", 0))
    if v == "paused":
        return t("Not tested: {error}", error=a.detail)
    if v == "off":
        return t("The VPN or the site was switched off meanwhile.")
    if v == "error":
        return t("The test failed: {error}", error=a.detail)
    return ""


def _server_row(sh: Shuffle, srv: Server, names: dict[str, str], live: bool) -> dict[str, Any]:
    if srv.status != "done":
        label, tone = (t("starting …") if srv.status == "starting" else t("asking the sites …")), "brand"
    elif srv.problem:
        label, tone = vpn.reason_label(srv.problem), "bad"
    elif not srv.refused():
        label, tone = t("all answer"), "ok"
    else:
        label, tone = t("{ok} of {n} answer", ok=srv.answered(), n=len(srv.answers)), "warn"
    parts = [srv.detail] if srv.problem and srv.detail else []
    parts += [t("{site}: {reason}", site=names.get(pid, pid), reason=vpn.reason_label(a.verdict))
              + (f" ({_nb(a.detail)})" if a.detail else "") for pid, a in srv.answers.items() if a.verdict in REFUSED]
    return {"name": srv.name, "label": label, "tone": tone, "kept": srv.name == sh.kept and not live, "parts": parts,
            "secs": t("{n} s", n=_num(srv.secs)) if srv.secs is not None else ""}


def _nb(detail: str) -> str:
    """"HTTP 403" never breaks between its two words."""
    return detail.replace("HTTP ", "HTTP ")


def _paused_sites(sh: Shuffle) -> list[str]:
    srv = next((s for s in sh.servers if s.name == sh.kept), None)
    return srv.refused() if srv is not None else []


def _progress(sh: Shuffle) -> str:
    """The header while a shuffle runs."""
    if not sh.servers:
        return t("Picking a server …")
    srv = sh.servers[-1]
    if sh.profile:
        return t("Testing the profile {server}", server=srv.name)
    return t("Server {n} of up to {max}: {server}", n=len(sh.servers), max=MAX_SERVERS, server=srv.name)


def _summary(sh: Shuffle) -> str:
    if sh.status in ("stopped", "interrupted"):
        return _reason_text(sh.reason)
    if sh.status == "failed":
        return _failed_text(sh)
    srv = next((s for s in sh.servers if s.name == sh.kept), None)
    if srv is None:
        return ""
    total, refused = len(srv.answers), srv.refused()
    if not refused:
        if total == 1:
            return t("{server}: {site} answers", server=srv.name, site=sh.sites[0].name)
        return t("{server}: all {n} sites answer", server=srv.name, n=total)
    if len(sh.servers) == 1:                                 # a profile, or the only server left
        return t("{server}: {ok} of {n} sites answer", server=srv.name, ok=total - len(refused), n=total)
    return t("{server} stays, the best of {k} servers: {ok} of {n} sites answer", server=srv.name,
             k=len(sh.servers), ok=total - len(refused), n=total)


def _tone(sh: Shuffle) -> str:
    if sh.status == "failed":
        return "bad"
    return "warn" if _paused_sites(sh) else ""


def _failed_text(sh: Shuffle) -> str:
    if sh.reason == "no_eu":
        return t("No server works: the collection has no EU server left to try")
    if sh.reason == "profile":
        return t("The profile does not work for any site")
    if sh.reason == "none":
        return t("No server works: none of the {n} tried answers any site", n=len(sh.servers))
    return t("The shuffle failed: {error}", error=sh.reason)


def _trigger_text(sh: Shuffle, names: dict[str, str]) -> str:
    if sh.trigger == "schedule":
        return t("Scheduled")
    if sh.trigger == "manual":
        return t("Started by you")
    if sh.trigger == "new":
        return t("New configs")
    site = names.get(sh.by) or ("Geizhals" if sh.by == vpn.GEIZHALS else sh.by)
    if sh.why == "none":
        return t("{site} found no server", site=site)
    return t("{site}: {server} {reason}", site=site, server=sh.before or "?", reason=vpn.reason_label(sh.why)) \
        + (f" ({_nb(sh.detail)})" if sh.detail else "")


def _pending_text(p: tuple[float, str, str, str]) -> str:
    """Why the next shuffle comes early: an automatic one waits for its turn."""
    site = dict(vpn.users()).get(p[1], p[1])
    if p[2] == "none":
        return t("Brought forward: {site} found no server.", site=site)
    return t("Brought forward: {site} refused the server ({reason}).", site=site, reason=vpn.reason_label(p[2]))


def _reason_text(reason: str) -> str:
    return {"uploaded": t("Stopped: new configs were uploaded"), "removed": t("Stopped: the configs were removed"),
            "interrupted": t("Interrupted by a restart of the app")}.get(reason, t("Stopped"))


def _num(x: float) -> str:
    """2.9 → "2.9", in German "2,9"."""
    s = f"{x:g}"
    return s.replace(".", ",") if lang() == "de" else s


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None
