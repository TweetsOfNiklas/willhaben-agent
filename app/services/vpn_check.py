"""The VPN search (app/platforms/vpn.py): finds, in the background, the server(s) every site with its VPN switch on
answers through, and switches over once it found better ones.

Goal: one EU server every site answers (each site with its VPN switch on, and Geizhals when its switch is on); when
none does, two that share the sites, each site on one that answers it (vpn.MAX_LIVE); when not even two cover them
all, the pair that covers the most, and the rest pause with a note until the next regular search.

Triggers: the schedule (every `vpn_shuffle_h` hours after the last search, default 6), "Search now" and new configs
are regular searches: they look for a server other than the one in use, so the server changes now and then. A switch
turned on (is the new site answered by the server in use?) and a site that refuses its server or gets no answer
(Tunnel.failed(), or a request that finds no server) start one too; the automatic ones are guarded against storms:
AUTO_GAP_S after the last search and AUTO_PER_HOUR an hour (one that may not run yet waits: `pending`, started by
tick(), as is one a restart cut off), and a site starts at most one between two regular ones (refusing again, it waits
for the next regular one). One search at a time; a site switched on or off meanwhile joins or leaves the running one.

In the background: the search tests servers on a tunnel of its own (never a live server: one key twice on one server
knocks both off) while the live connections keep serving every site that has a server. A site whose server refused
it waits (VpnWait, no agent error) until the search finds it one; the others go on. Each better result goes live at
once (vpn.switch): a server found for the waiting sites becomes the second connection (the search's process is taken
over), and the search goes on for a single server; once it finds one, every site moves to it.

What it knows (the Board): per server the sites that answered through it in this search, or that use it live, and
the ones that refused it (in this search, or a mark that has not expired). The best plan covers the most sites; then
it has fewer servers; in a regular search it is a single server other than the one(s) in use (the change); then it
keeps servers in use; then nearer and earlier ones. A plan never takes the server away from a site that has one.

Fail fast: a site is asked through a server only while its answer could still make the best plan better (every site
not asked there answering, against none answering): the sites without a server first, then the ones that refused most
in this search and lately. A server the pickiest site refuses costs one request; one marked for a site whose answer
is needed costs none (it is skipped without starting it). Live servers are asked first, without a new process: a site
switched on is tested on the server in use before another server starts.

Order: the EU servers, nearest first, random within a tier (vpn.order()), the ones a waiting site answered through
lately first; never one outside the EU. All of them, no cap: the search ends with one server for every site, or when
no server is left that could make the result better.

In parallel: several servers start at once (vpn.SEARCH_TUNNELS; no site request, a dead one fails within seconds by
its missing handshake and is marked for a day) and ASK_PARALLEL of them are asked at once, each site through one
server at a time (its limiter paces it anyway, and the live requests never queue behind several tests). A test
request gives up after TRIAL_TIMEOUT_S. Once a server can no longer help (Board.could: neither as the one server nor
as the partner a site without a server needs), its slow asks stop (not sent yet, or eBay's warm-up) and its tunnel
goes; a single request under way finishes and counts, so a server one site refused still records the others' answers
and can become the partner of a pair.

Pacing: every test request goes through the site's own client and limiter, so it counts towards the site's hourly
budget. The search sends a site at most a quarter of that budget an hour (NO_BUDGET_PER_HOUR without one), never past
BUDGET_FULL of it with the agents' requests, and nothing during a pause after a block: it waits for the site instead of
skipping it, and the page says for which site and why. eBay's warm-up (vpn_test_requests: at most 3 requests per
server), Kleinanzeigen's credential errors (NotTheExit) and a site's own errors (5xx, a JSON error) are answers, not
refusals; bot-check pages through BOTCHECK_STREAK servers in a row are the site's change, not the servers' (`odd`).

A dedicated profile is never swapped: it is only tested, every site through it; the refusing ones pause.

State in memory while it runs (the page polls it) and in the setting `vpn_test` (the last search) for reloads and
restarts; the connections themselves in vpn's `vpn_conn`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, NamedTuple

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
SHARE = 4                 # the search sends a site at most 1/SHARE of its hourly budget an hour
NO_BUDGET_PER_HOUR = 60   # ... and a site without a budget (Geizhals) at most this many
BUDGET_FULL = 0.9         # no test request that could take a site past this share of its hourly budget
WAIT_STEP_S = 30          # a wait for a site's budget looks again this often (the agents' requests change it)
BOTCHECK_STREAK = 3       # bot-check pages through this many servers in a row: the site changed, not the servers
AUTO_GAP_S = 300          # an automatic search starts at least this long after the end of the last one
AUTO_PER_HOUR = 3         # and at most this many an hour
INTERVAL_H = 6            # default of the setting vpn_shuffle_h
INTERVAL_KEY = "vpn_shuffle_h"
STOP_WAIT_S = 10          # cancel(): longest wait for the search to end
STATE_KEY = "vpn_test"
REGULAR = ("schedule", "manual", "new")         # they look for another server: the change now and then
ANSWERED = ("ok", "site_error", "odd_page", "error")
REFUSED = ("blocked", "rate", "unreachable", "botcheck")
SHOW_SERVERS = 6          # servers the live panel lists (the newest)
ASK_PARALLEL = 3          # servers asked at the same time (each site asks through one at a time)

_clock: Callable[[], float] = time.monotonic          # tests move the hour on
_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep


@dataclass
class Answer:
    """One site through one server."""
    verdict: str = "waiting"   # waiting | asking | ANSWERED | REFUSED | off | skipped | moved
    detail: str = ""           # "HTTP 403", the network error, the site's message
    requests: int = 0
    secs: float | None = None
    params: dict[str, Any] = field(default_factory=dict)   # site_error: fix; odd_page: n


@dataclass
class Server:
    name: str
    status: str = "starting"   # starting | testing | done
    live: bool = False         # asked through a live connection (no process of its own)
    problem: str = ""          # broken | unreachable: no site was asked
    detail: str = ""
    answers: dict[str, Answer] = field(default_factory=dict)
    secs: float | None = None

    def refused(self) -> list[str]:
        return [pid for pid, a in self.answers.items() if a.verdict in REFUSED]

    def answered(self) -> list[str]:
        return [pid for pid, a in self.answers.items() if a.verdict in ANSWERED]


@dataclass
class Site:
    pid: str
    name: str
    requests: int = 0          # sent by this search


@dataclass
class Search:
    trigger: str               # schedule | manual | new | switch | auto
    sites: list[Site]
    by: str = ""               # auto, switch: the site that refused its server, found none or was switched on
    why: str = ""              # blocked | rate | unreachable | broken | botcheck | none | on
    detail: str = ""
    profile: bool = False      # a dedicated profile: tested, never swapped
    before: list[str] = field(default_factory=list)       # the live servers at the start
    total: int = 0             # EU servers of the collection
    skipped: int = 0           # servers not started: marks say they cannot make the result better
    servers: list[Server] = field(default_factory=list)
    best: dict[str, Any] = field(default_factory=dict)    # the best plan so far: {servers, assign}
    plan: dict[str, Any] = field(default_factory=dict)    # what it left live: {servers, assign, uncovered}
    switched: int = 0          # switch-overs it made
    odd: list[str] = field(default_factory=list)          # sites whose bot-check pages are the site's change
    status: str = "running"    # running | done | failed | stopped | interrupted
    reason: str = ""           # failed: no_eu | none | profile; stopped: stopped | uploaded | removed | interrupted
    started: float = field(default_factory=time.time)
    finished: float | None = None
    wait: dict[str, Any] = field(default_factory=dict, compare=False)   # what it waits for now (not saved)
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    discarded: bool = field(default=False, repr=False, compare=False)    # its configs are gone: never saved again
    streak: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    targets: dict[str, Target] = field(default_factory=dict, repr=False, compare=False)

    def site(self, pid: str) -> Site:
        return next(s for s in self.sites if s.pid == pid)

    def checked(self) -> int:
        """Servers it is done with: asked (or found dead) on its tunnels, or skipped."""
        return sum(not s.live and s.status == "done" for s in self.servers) + self.skipped


@dataclass
class Target:
    """What a search asks: a site's search, or Geizhals' (not a platform, its own switch)."""
    pid: str
    name: str
    host: str                  # probed when a server starts
    limiter: RateLimiter
    cost: int                  # requests one ask may send (eBay: its warm-up)
    ask: Callable[[], Awaitable[Any]]

    def on(self) -> bool:
        return vpn.enabled(self.pid) and (self.pid == vpn.GEIZHALS or platforms.enabled(self.pid))


_run: Search | None = None
_spent: dict[str, deque[float]] = {}     # per site the search's requests of the last hour (_clock), across searches


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
    """Why no search can start now; None when one can."""
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


def pending(st: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A search waiting for its turn: {at, trigger, by, why, detail} (an older state: [at, pid, reason, detail])."""
    p = (vpn.conn_state() if st is None else st).get("pending")
    if isinstance(p, list) and len(p) == 4:
        return {"at": float(p[0]), "trigger": "auto", "by": str(p[1]), "why": str(p[2]), "detail": str(p[3])}
    if isinstance(p, dict) and isinstance(p.get("at"), (int, float)):
        return {"at": float(p["at"]), "trigger": str(p.get("trigger") or "auto"), "by": str(p.get("by") or ""),
                "why": str(p.get("why") or ""), "detail": str(p.get("detail") or "")}
    return None


def next_at() -> float | None:
    """When the next search starts: a pending one, else the regular one; None when none is due yet (never searched:
    the next tick)."""
    st = vpn.conn_state()
    if p := pending(st):
        return p["at"]
    return float(st["shuffled"]) + interval_h() * 3600 if st.get("shuffled") else None


def _auto_at(st: dict[str, Any], now: float) -> float:
    """The earliest time an automatic search may start: AUTO_GAP_S after the last one ended, and no more than
    AUTO_PER_HOUR in an hour."""
    at = float(st.get("shuffled") or 0) + AUTO_GAP_S
    recent = sorted(float(x) for x in st.get("auto") or [] if now - float(x) < 3600)
    if len(recent) >= AUTO_PER_HOUR:
        at = max(at, recent[-AUTO_PER_HOUR] + 3600)
    return at


def running() -> bool:
    return _run is not None and _run.task is not None and not _run.task.done()


def trigger(pid: str, reason: str, detail: str = "") -> bool:
    """A site refused its server (reason blocked | rate | unreachable | broken | botcheck) or found none (none): a
    search now, or once the guards allow it (pending, started by tick()). True when one runs or comes; False when
    this site started one since the last regular search (it waits for the next regular one) or none can run."""
    if running():
        return True
    if blocker():
        return False
    st, now = vpn.conn_state(), time.time()
    if reason != "none" and pid in (st.get("triggered") or {}):
        log.info("VPN: %s %s again; it waits for the next regular search", pid, reason)
        return False
    at = _auto_at(st, now)
    if at > now:
        if not pending(st):
            vpn.update_conn(pending={"at": at, "trigger": "auto", "by": pid, "why": reason, "detail": detail},
                            triggered={**(st.get("triggered") or {}), **({pid: now} if reason != "none" else {})})
            log.info("VPN: %s %s, the next search may start in %.0f s", pid, reason, at - now)
        return True
    return start("auto", by=pid, why=reason, detail=detail) is None


def site_on(pid: str) -> None:
    """A VPN switch went on: a running search takes the site in; else one starts, which asks it through the server
    in use first."""
    if not running() and not blocker():
        start("switch", by=pid, why="on")


def _cut_off() -> dict[str, Any] | None:
    """The stored search when the app stopped hard while it ran (no shutdown to note it as pending)."""
    try:
        d = json.loads(get_setting(STATE_KEY) or "null")
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("status") == "running" and isinstance(d.get("trigger"), str) else None


async def tick() -> None:
    """Scheduler, every minute: a pending search whose turn came, one the app's end cut off, else the regular one
    when it is due."""
    if running() or blocker():
        return
    st, now = vpn.conn_state(), time.time()
    if (p := pending(st)) is not None:
        if p["at"] <= now:
            start(p["trigger"], by=p["by"], why=p["why"], detail=p["detail"])
        return
    if (d := _cut_off()) is not None:
        start(d["trigger"], by=str(d.get("by") or ""), why=str(d.get("why") or ""), detail=str(d.get("detail") or ""))
        return
    if now >= float(st.get("shuffled") or 0) + interval_h() * 3600:
        start("schedule")


# ------------------------------------------------------------------ starting and stopping
def start(trigger: str = "manual", *, by: str = "", why: str = "", detail: str = "") -> str | None:
    """Search in the background (the header lists the job); None when it started, else why not. No await before the
    task exists: two clicks, two tabs or two refusals cannot start two."""
    global _run
    if running():
        return t("A VPN search is running")
    if reason := blocker():
        return reason
    tgs = targets()
    st, now = vpn.conn_state(), time.time()
    auto = [x for x in st.get("auto") or [] if now - float(x) < 3600] + ([now] if trigger == "auto" else [])
    triggered = {} if trigger in REGULAR else dict(st.get("triggered") or {})
    if trigger == "auto" and why != "none":
        triggered[by] = now
    vpn.update_conn(pending=None, auto=auto, triggered=triggered)
    _run = Search(trigger, [Site(tg.pid, tg.name) for tg in tgs], by=by, why=why, detail=detail,
                  profile=vpn.source()[0] == "profile", before=vpn.live_servers(st))
    _save(_run)
    _run.task = asyncio.create_task(_main(_run))
    return None


async def cancel(reason: str) -> None:
    """Stop, new or removed configs, app shutdown: cancel now and wait for the search to end (≤ STOP_WAIT_S). The
    live connections stay as the search left them."""
    if not running():
        return
    run = _run
    run.reason = reason
    run.task.cancel()
    await asyncio.wait({run.task}, timeout=STOP_WAIT_S)


async def source_changed(reason: str) -> None:
    """Before vpn.source_changed(): cancel a running search (its servers are about to go) and forget the last one
    (it names servers of the old configs), also when the cancel fails."""
    try:
        await cancel(reason)
    finally:
        clear()


async def shutdown() -> None:
    await cancel("interrupted")


def clear() -> None:
    """Forget the last search. One still winding down after cancel() saves nothing when it ends."""
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
    _spent.clear()


# ------------------------------------------------------------------ what the search knows, and the best plan
class Plan(NamedTuple):
    servers: tuple[str, ...]   # primary first
    covered: int               # bitmask of the sites
    key: tuple[int, ...]       # (sites covered, -servers, the change, servers kept, -rank): larger is better


class Board:
    """What a search knows now, as bitmasks over the sites (see the module docstring)."""

    def __init__(self, sh: Search, tgs: list[Target]) -> None:
        st = vpn.conn_state()
        self.pids = [tg.pid for tg in tgs]
        self.cost = {tg.pid: tg.cost for tg in tgs}
        self.bit = {pid: 1 << i for i, pid in enumerate(self.pids)}
        self.full = (1 << len(self.pids)) - 1
        self.live = vpn.live_servers(st)
        self.before = set(sh.before)
        self.regular = sh.trigger in REGULAR
        self.odd = self.mask(sh.odd)
        self.next_rank = 10 + len(sh.servers)
        self.rank = {s: i for i, s in enumerate(self.live)}
        self.ok: dict[str, int] = {}
        self.no: dict[str, int] = {}
        self.asked: dict[str, int] = {}
        assign, paused = st.get("assign") or {}, st.get("paused") or {}
        self.using = {s: self.mask(p for p in self.pids if assign.get(p) == s and p not in paused) for s in self.live}
        for s in self.live:          # a site on a live server without trouble answers through it
            self.ok[s] = self.using[s]
        self.refusals = dict.fromkeys(self.pids, 0)
        for i, srv in enumerate(sh.servers):
            self.rank.setdefault(srv.name, 10 + i)
            if srv.problem:
                self.no[srv.name] = self.full
                continue
            for pid, a in srv.answers.items():
                if pid not in self.bit:
                    continue
                if a.verdict in ANSWERED:
                    self._add(self.ok, srv.name, pid)
                elif a.verdict in REFUSED:
                    self._add(self.no, srv.name, pid)
                    self.refusals[pid] += 1
                else:
                    continue
                self._add(self.asked, srv.name, pid)
        self.marks = {pid: vpn.marks(pid) for pid in self.pids}
        for pid, m in self.marks.items():
            for name in m:
                self._add(self.no, name, pid)
        for s, m in self.ok.items():                   # an answer of this search beats an older mark
            self.no[s] = self.no.get(s, 0) & ~m
        self._memo: dict[tuple[int, int, bool, bool, int], bool] = {}
        self.must = self._best({s: self.ok.get(s, 0) for s in self.live}, 0).covered   # never taken away
        self._plan_now: Plan | None = None

    def mask(self, pids: Any) -> int:
        m = 0
        for pid in pids:
            m |= self.bit.get(pid, 0)
        return m

    def _add(self, d: dict[str, int], s: str, pid: str) -> None:
        d[s] = d.get(s, 0) | self.bit[pid]

    def names(self, m: int) -> list[str]:
        return [pid for pid in self.pids if m & self.bit[pid]]

    def unknown(self, s: str) -> int:
        """Sites not asked through `s` whose answer there is not known either."""
        return self.full & ~(self.ok.get(s, 0) | self.no.get(s, 0) | self.asked.get(s, 0) | self.odd)

    def _best(self, ok: dict[str, int], must: int) -> Plan:
        """The best plan over `ok` (server -> sites answering) that covers `must`: one server or two."""
        reps: dict[tuple[int, bool, bool], str] = {}
        for s, m in ok.items():
            m = (m | self.odd) & self.full
            if not m:
                continue
            key = (m, s in self.live, s in self.before)       # one of each kind is enough
            if key not in reps or self.rank.get(s, self.next_rank) < self.rank.get(reps[key], self.next_rank):
                reps[key] = s
        best = Plan((), 0, (0, 0, 0, 0, 0))
        found = not must
        servers = list(reps.values())
        cover = {s: (ok[s] | self.odd) & self.full for s in servers}
        for i, a in enumerate(servers):
            for b in [None, *servers[i + 1:]]:
                cov = cover[a] | (cover[b] if b else 0)
                if cov & must != must:
                    continue
                plan = self._plan(a, b, cover, cov)
                if not found or plan.key > best.key:
                    best, found = plan, True
        return best

    def _plan(self, a: str, b: str | None, cover: dict[str, int], cov: int) -> Plan:
        servers = [a] if b is None else sorted(
            (a, b), key=lambda s: (-bin(cover[s]).count("1"), s not in self.live, self.rank.get(s, self.next_rank)))
        change = int(self.regular and b is None and cov == self.full and a not in self.before)
        kept = sum(s in self.live for s in servers)
        rank = sum(self.rank.get(s, self.next_rank) for s in servers)
        return Plan(tuple(servers), cov, (bin(cov).count("1"), -len(servers), change, kept, -rank))

    def current(self) -> Plan:
        """The connections as they are: the live servers and the sites that use them now."""
        cov = 0
        for m in self.using.values():
            cov |= m
        if not self.live:
            return Plan((), 0, (0, 0, 0, 0, 0))
        return self._plan(self.live[0], self.live[1] if len(self.live) > 1 else None,
                          {s: self.using[s] | self.odd & self.full for s in self.live}, cov | (self.odd & self.full))

    def best(self, only_live: bool = False) -> Plan:
        if only_live:
            return self._best({s: self.ok.get(s, 0) for s in self.live}, 0)
        if self._plan_now is None:
            self._plan_now = self._best(self.ok, self.must)
        return self._plan_now

    def could(self, s: str) -> bool:
        """Could answers through `s` still make the best plan better (every site not asked there answering)?"""
        unk = self.unknown(s)
        if not unk:
            return False
        memo = (self.ok.get(s, 0), unk, s in self.before, s in self.live, self.rank.get(s, self.next_rank))
        if memo not in self._memo:
            pess = self._best(self.ok, self.must) if s in self.ok else self._best({**self.ok, s: 0}, self.must)
            opt = self._best({**self.ok, s: self.ok.get(s, 0) | unk}, self.must)
            self._memo[memo] = opt.key > pess.key
        return self._memo[memo]

    def done(self) -> bool:
        """One server for every site (in a regular search one that was not in use: the change)."""
        p = self.best()
        return p.covered == self.full and len(p.servers) == 1 and (not self.regular or p.servers[0] not in self.before)

    def pickiest(self, m: int) -> list[str]:
        """The sites of `m`, the one that refused most first (in this search, then lately), a slow one (eBay's
        warm-up) last among equals: fail fast."""
        return sorted(self.names(m), key=lambda pid: (-self.refusals[pid], -len(self.marks[pid]), self.cost[pid],
                                                      self.pids.index(pid)))

    def assign(self, plan: Plan, current: dict[str, str]) -> dict[str, str]:
        """Each covered site on a server of `plan` that answers it: the one it uses when that is one, else the
        primary first."""
        out = {}
        for pid in self.pids:
            can = [s for s in plan.servers if (self.ok.get(s, 0) | self.odd) & self.bit[pid]]
            if can:
                out[pid] = current[pid] if current.get(pid) in can else can[0]
        return out


def _refresh(sh: Search) -> list[Target]:
    """The sites now: one switched on meanwhile joins the search, one switched off leaves it."""
    tgs = targets()
    sh.targets = {tg.pid: tg for tg in tgs}
    for tg in tgs:
        if all(s.pid != tg.pid for s in sh.sites):
            sh.sites.append(Site(tg.pid, tg.name))
    return tgs


def _note_best(sh: Search, b: Board) -> None:
    p = b.best()
    sh.best = {"servers": list(p.servers), "assign": b.assign(p, vpn.conn_state().get("assign") or {})}


# ------------------------------------------------------------------ the search
async def _main(sh: Search) -> None:
    global _run
    label = t("VPN test") if sh.profile else t("VPN server search")
    try:
        async with jobs.track("vpn-search", label, lane="vpn", link="/settings/vpn",
                              quiet=sh.trigger != "manual") as job:
            try:
                await (_test_profile(sh, job) if sh.profile else _search(sh, job))
            except asyncio.CancelledError:      # Stop, new configs, shutdown
                sh.status = "interrupted" if sh.reason == "interrupted" else "stopped"
            finally:
                with suppress(Exception):
                    await vpn.conn.stop_search()   # the server under test; the live ones stay
            job.result = _summary(sh)
            job.problems = len(sh.plan.get("uncovered") or []) + (sh.status == "failed")
            if job.problems:
                job.quiet = False
    except Exception as e:  # noqa: BLE001 - the state must be noted whatever happened
        log.warning("VPN search: %s", e)
        sh.status, sh.reason = "failed", _short(e)
        with suppress(Exception):
            await vpn.conn.stop_search()
    finally:
        sh.finished, sh.wait = time.time(), {}
        if sh.status == "running":
            sh.status = "interrupted"
        if not sh.discarded:
            if sh.status in ("done", "failed", "stopped"):
                vpn.update_conn(shuffled=sh.finished)
            elif sh.status == "interrupted":    # a restart cut it off: the first tick runs it again
                vpn.update_conn(pending={"at": sh.finished, "trigger": sh.trigger, "by": sh.by, "why": sh.why,
                                         "detail": sh.detail})
            _save(sh)
        if sh.discarded and _run is sh:
            _run = None


@dataclass(eq=False)
class _Slot:
    """A server the search asks through: one of its tunnels, or a live server (no process of its own)."""
    srv: Server
    route: vpn.Route | None = None
    asks: dict[str, asyncio.Task] = field(default_factory=dict)    # site -> its ask under way
    trials: dict[str, Trial] = field(default_factory=dict)        # site -> its Trial (what it sent so far)
    t0: float = field(default_factory=time.monotonic)


async def _search(sh: Search, job: jobs.Job) -> None:
    """Until one server serves every site or none is left that could help (the module docstring): servers start
    on the search's tunnels several at a time (no site request), sites are asked on several of them at once (each
    site one ask at a time, its limiter paces it), and each better result goes live at once."""
    order = vpn.order()
    sh.total = len(order)
    if not order:
        sh.status, sh.reason = "failed", "no_eu"
        vpn.update_conn(servers=[], assign={}, error=_failed_text(sh))
        await vpn.conn.disconnect()
        return
    slots: dict[str, _Slot] = {}                         # servers being asked through (or ready for it)
    starts: dict[asyncio.Task, _Slot] = {}               # tunnels starting
    asks: dict[asyncio.Task, tuple[_Slot, str]] = {}     # asks under way
    try:
        while True:
            tgs = _refresh(sh)
            if not tgs:
                break
            b = Board(sh, tgs)
            _note_best(sh, b)
            if b.done():
                break
            await _retire(sh, b, slots)
            _dispatch(sh, b, slots, asks, job)
            _start_more(sh, b, order, slots, starts, asks)
            if not starts and not asks:
                break
            job.step, job.done, job.total = _progress(sh), sh.checked(), sh.total
            done, _ = await asyncio.wait({*starts, *asks}, return_when=asyncio.FIRST_COMPLETED)
            for task in sorted(done, key=lambda x: x in asks):     # tunnels first: their slots in order
                if task in starts:
                    _started(sh, starts.pop(task), task, slots)
                else:
                    _asked(sh, *asks.pop(task), task)
            await _improve(sh)
    finally:                                             # done, stopped or cut off: nothing keeps running
        for task in [*starts, *asks]:
            task.cancel()
        if starts or asks:
            await asyncio.gather(*starts, *asks, return_exceptions=True)
        for task, (slot, pid) in asks.items():
            _asked(sh, slot, pid, task)
        for slot in [*slots.values(), *starts.values()]:
            if slot.srv.status != "done":
                slot.srv.status, slot.srv.secs = "done", round(time.monotonic() - slot.t0, 1)
        sh.servers = [s for s in sh.servers if s.answers or s.problem]   # up ahead but never needed: not checked
        await vpn.conn.stop_search()
    await _finish(sh)


def _wave(b: Board, s: str, inflight: set[str]) -> list[str]:
    """The sites to ask through `s` now (fail fast): the ones without a server, all at once; else, while nothing is
    known through `s`, the pickiest alone; once it answered, the rest at once (_retire() cancels a slow one when `s`
    can no longer help)."""
    unk = [pid for pid in b.pickiest(b.unknown(s)) if pid not in inflight]
    if not unk or not b.could(s):
        return []
    without = b.full & ~b.best().covered
    if first := [pid for pid in unk if without & b.bit[pid]]:
        return first
    if not b.asked.get(s, 0) and not b.ok.get(s, 0):
        return [] if inflight else unk[:1]
    return unk


def _dispatch(sh: Search, b: Board, slots: dict[str, _Slot], asks: dict[asyncio.Task, tuple[_Slot, str]],
              job: jobs.Job) -> None:
    """Start the asks that may run now: live servers first (no new process), then the search's tunnels in the order
    they started, ASK_PARALLEL of them at a time; a site asks through one server at a time."""
    busy = {pid for _s, pid in asks.values()}
    asking = sum(1 for n, s in slots.items() if s.asks and n not in b.live and not s.srv.live)
    rank = {s.name: i for i, s in enumerate(sh.servers)}       # the search's order, not the order tunnels came up
    names = [*b.live, *sorted((n for n, s in slots.items() if s.route is not None and n not in b.live),
                              key=lambda n: rank.get(n, 0))]
    for name in names:
        slot = slots.get(name)
        if name not in b.live and not slot.asks and asking >= ASK_PARALLEL:
            continue
        wave = [pid for pid in _wave(b, name, set(slot.asks) if slot else set()) if pid not in busy]
        if not wave:
            continue
        if slot is None:
            srv = Server(name, status="testing", live=True)
            sh.servers.append(srv)
            slot = slots[name] = _Slot(srv)
        elif name not in b.live and not slot.asks:
            asking += 1
        for pid in wave:
            task = asyncio.create_task(_ask_slot(sh, slot, sh.targets[pid], job))
            asks[task], slot.asks[pid] = (slot, pid), task
            busy.add(pid)


def _start_more(sh: Search, b: Board, order: list[str], slots: dict[str, _Slot],
                starts: dict[asyncio.Task, _Slot], asks: dict[asyncio.Task, tuple[_Slot, str]]) -> None:
    """More servers on the search's tunnels while there is room (vpn.SEARCH_TUNNELS) and few started ones wait to be
    asked: a dead server shows within seconds, and several at once hide that wait."""
    if any(s.srv.live for s, _p in asks.values()):
        return                               # a live server may settle it: no tunnel before its answer
    ready = sum(1 for n, s in slots.items() if s.route is not None and not s.asks and n not in b.live
                and not b.asked.get(n, 0))
    while len(vpn.conn.testing()) + len(starts) - _starting(starts) < vpn.SEARCH_TUNNELS \
            and ready + len(starts) < 2 * ASK_PARALLEL:
        name = _candidate(sh, b, order)
        if name is None:
            return
        pid = (_wave(b, name, set()) or b.pickiest(b.unknown(name)) or [None])[0]
        slot = _Slot(Server(name))
        sh.servers.append(slot.srv)
        starts[asyncio.create_task(_start_slot(slot, sh.targets[pid].host if pid else "www.willhaben.at",
                                               list(sh.targets)))] = slot


def _starting(starts: dict[asyncio.Task, _Slot]) -> int:
    """Starts already counted by vpn.conn.testing() (they reserved their server)."""
    testing = set(vpn.conn.testing())
    return sum(1 for s in starts.values() if s.srv.name in testing)


def _candidate(sh: Search, b: Board, order: list[str]) -> str | None:
    """The next server to start: in vpn.order(), the ones a site without a server answered through lately first;
    none already asked, live or whose answers could not help (sh.skipped counts those)."""
    seen = {s.name for s in sh.servers} | set(b.live)
    without = b.full & ~b.best().covered
    liked = {n for pid in b.names(without) for n in vpn.oks(pid)}
    pick, skipped = None, 0
    for name in sorted(order, key=lambda n: n not in liked):
        if name in seen:
            continue
        if not b.could(name):
            skipped += 1
        elif pick is None:
            pick = name
    sh.skipped = skipped
    return pick


async def _start_slot(slot: _Slot, host: str, pids: list[str]) -> None:
    """A tunnel of the search: started, the handshake there within seconds, `host` reachable through it. A dead one
    is marked for every site (a day: stale configs drop out)."""
    srv = slot.srv
    try:
        slot.route = await vpn.conn.start_search(srv.name, host)
    except (OSError, vpn.Unreachable) as e:
        srv.problem = "unreachable" if isinstance(e, vpn.Unreachable) else "broken"
        srv.detail = e.detail if isinstance(e, vpn.Unreachable) else _short(e)
        srv.status, srv.secs = "done", round(time.monotonic() - slot.t0, 1)
        log.info("VPN search: %s %s: %s", srv.name, srv.problem, srv.detail)
        for pid in pids:
            vpn.mark(pid, srv.name, srv.problem)
        return
    srv.status = "testing"


def _started(sh: Search, slot: _Slot, task: asyncio.Task, slots: dict[str, _Slot]) -> None:
    if not task.cancelled() and (e := task.exception()) is not None:
        slot.srv.problem, slot.srv.detail, slot.srv.status = "broken", _short(e), "done"
        log.warning("VPN search: %s did not start: %s", slot.srv.name, e)
    elif slot.route is not None:
        slots[slot.srv.name] = slot


def _asked(sh: Search, slot: _Slot, pid: str, task: asyncio.Task) -> None:
    """An ask ended: cancelled (its server could no longer help, or the search ends) leaves "skipped"."""
    slot.asks.pop(pid, None)
    tr = slot.trials.pop(pid, None)
    a = slot.srv.answers.get(pid)
    if task.cancelled() and (a is None or a.verdict in ("waiting", "asking")):
        slot.srv.answers[pid] = Answer("skipped", requests=tr.sent if tr else 0)
    elif not task.cancelled() and (e := task.exception()) is not None:
        slot.srv.answers[pid] = Answer("error", _short(e))
    if slot.srv.live and not slot.asks:
        slot.srv.status, slot.srv.secs = "done", round(time.monotonic() - slot.t0, 1)


async def _retire(sh: Search, b: Board, slots: dict[str, _Slot]) -> None:
    """A server that can no longer help (neither as the one server nor as the partner a site without a server needs:
    Board.could): its slow asks stop (not sent yet, or eBay's warm-up; a single request under way ends within
    TRIAL_TIMEOUT_S and is kept), and its tunnel stops once nothing runs there. One the best plan needs stays."""
    keep = set(b.best().servers)
    for name, slot in list(slots.items()):
        if slot.route is None and not slot.srv.live or name in keep or b.could(name):
            continue
        for pid, task in slot.asks.items():
            tr = slot.trials.get(pid)
            if tr is None or not tr.wire or tr.limit > 1:
                task.cancel()
        if slot.asks:
            continue
        del slots[name]
        if slot.srv.status != "done":
            slot.srv.status, slot.srv.secs = "done", round(time.monotonic() - slot.t0, 1)
        if not slot.srv.live:
            await vpn.conn.stop_search(name)       # no-op for one that went live meanwhile
            log.info("VPN search: %s (%s)", name,
                     ", ".join(f"{p}: {a.verdict}" for p, a in slot.srv.answers.items()) or "-")


async def _ask_slot(sh: Search, slot: _Slot, tg: Target, job: jobs.Job) -> None:
    """One site through the slot's server: its tunnel, or a live server's route (started again when it stopped)."""
    srv, route = slot.srv, slot.route
    if srv.live:
        route = vpn.conn.route(srv.name)
        if route is None:
            try:
                route = await vpn.conn.start_live(srv.name, tg.host)
            except (OSError, vpn.Unreachable) as e:
                reason = "unreachable" if isinstance(e, vpn.Unreachable) else "broken"
                log.warning("VPN search: %s in use %s: %s", srv.name, reason, e)
                vpn.mark(tg.pid, srv.name, reason)
                srv.answers[tg.pid] = Answer(reason, e.detail if isinstance(e, vpn.Unreachable) else _short(e))
                await vpn.drop(srv.name, t("{server} {reason}", server=srv.name, reason=vpn.reason_label(reason)))
                return
            if route is None:                 # no longer live: the next round sees it
                srv.answers[tg.pid] = Answer("skipped")
                return
    await _ask_one(sh, srv, route, tg, job, slot.trials)
    a = srv.answers.get(tg.pid)
    if a is not None and a.verdict == "moved" and not srv.live and not vpn.conn.alive(route):
        srv.problem, srv.detail = "broken", "wireproxy stopped"      # its tunnel died: not asked again


async def _ask_one(sh: Search, srv: Server, route: vpn.Route, tg: Target, job: jobs.Job,
                   trials: dict[str, Trial] | None = None) -> None:
    """One site through `route`, once its budget allows it; what it says is noted (marks) and, when the site's own
    server refused it, the site waits from now on."""
    while True:
        srv.answers[tg.pid] = Answer("waiting")
        if not await _pace(sh, tg, job):
            srv.answers[tg.pid] = Answer("off")
            return
        srv.answers[tg.pid] = Answer("asking")
        try:
            a = await _ask(sh, route, tg, trials)
        except Exception as e:  # noqa: BLE001 - one site's surprise must not end the search
            log.warning("VPN search: %s through %s: %s", tg.pid, srv.name, e)
            a = Answer("error", _short(e))
        if a.verdict != "paused":
            break
        sh.wait = {"pid": tg.pid, "kind": "pause", "until": time.time() + WAIT_STEP_S}   # its limiter said no
        await _sleep(WAIT_STEP_S)
    srv.answers[tg.pid] = a
    if a.verdict in REFUSED:
        vpn.mark(tg.pid, srv.name, a.verdict)
        if vpn.server_for(tg.pid) == srv.name and not vpn.paused(tg.pid):
            vpn.pause(tg.pid, a.verdict, a.detail)
    elif a.verdict in ("ok", "site_error"):
        vpn.mark(tg.pid, srv.name, "ok")


async def _ask(sh: Search, route: vpn.Route, tg: Target, trials: dict[str, Trial] | None = None) -> Answer:
    """The site's search through `route` under a Trial (its own visitor), and what it says about the server."""
    tr, t1, exc = Trial(route, limit=tg.cost), time.monotonic(), None
    if trials is not None:
        trials[tg.pid] = tr
    try:
        with trial(tr):
            try:
                await tg.ask()
            except Exception as e:  # noqa: BLE001 - PlatformError, GeizhalsError: judged below
                exc = e
    finally:
        tr.close()
        sh.site(tg.pid).requests += tr.sent
        _spend(tg.pid, tr.sent)
    secs = round(time.monotonic() - t1, 1)
    v = _verdict(tg, tr, exc)
    if v == "botcheck":
        n = sh.streak[tg.pid] = sh.streak.get(tg.pid, 0) + 1
        if n >= BOTCHECK_STREAK:              # through several servers: the site changed, not the servers
            if tg.pid not in sh.odd:
                sh.odd.append(tg.pid)
                vpn.update_conn(odd=sorted({*(vpn.conn_state().get("odd") or []), tg.pid}))
            return Answer("odd_page", _short(exc), tr.sent, secs, {"n": n})
    elif v != "paused":
        sh.streak[tg.pid] = 0
    if v in ("blocked", "rate", "unreachable"):
        detail = tr.detail
    elif v in ("site_error", "botcheck", "paused", "error"):
        detail = _short(exc) or (t("answered without a request (cache or replay)") if v == "error" else "")
    else:
        detail = ""
    return Answer(v, detail, tr.sent, secs, {"fix": "settings"} if isinstance(exc, NotTheExit) else {})


def _verdict(tg: Target, tr: Trial, exc: BaseException | None) -> str:
    """What one try says about the server, checked in this order."""
    if not tg.on():
        return "off"                    # also the Trial's "VPN is off": nothing went out directly
    if isinstance(exc, ExitMoved):
        return "moved"                  # its server stopped before the request went out: says nothing about it
    if tr.refused:
        return tr.refused               # blocked | rate | unreachable
    if isinstance(exc, Blocked) and not exc.sent:
        return "paused"                 # the site's own limiter: a pause after a block, or the hour's budget
    if not tr.sent:
        return "error"                  # nothing went out: proves nothing about the server
    if exc is None:
        return "ok" if vpn.conn.alive(tr.route) else "unreachable"
    if isinstance(exc, NotTheExit):
        return "site_error"             # e.g. rotated credentials: every server would get the same
    if tr.challenge or (tr.status == 200 and "json" not in tr.ctype):
        return "botcheck"               # Cloudflare's challenge (any status) or a page in place of the result list
    return "site_error"                 # the site answered through the server with an error (5xx, a JSON error)


async def _improve(sh: Search) -> None:
    """Switch over when what the search found beats the connections in use."""
    b = Board(sh, _refresh(sh))
    plan, now = b.best(), b.current()        # what is known against what is live (not what is known of it)
    if not plan.servers or plan.key <= now.key:
        return
    assign = b.assign(plan, vpn.conn_state().get("assign") or {})
    hosts = {s: next((sh.targets[p].host for p, x in assign.items() if x == s), "www.willhaben.at")
             for s in plan.servers}
    try:
        await vpn.switch(list(plan.servers), assign, hosts)
    except (OSError, vpn.Unreachable) as e:   # a server found earlier does not start again: not again in this one
        log.warning("VPN search: switching to %s failed: %s", " + ".join(plan.servers), e)
        running_now = {*b.live, *vpn.conn.testing()}
        for srv in sh.servers:
            if srv.name in plan.servers and srv.name not in running_now and not srv.live:
                srv.problem, srv.detail = "broken", _short(e)
        return
    for pid in assign:
        vpn.unpause(pid)
    sh.plan, sh.switched = {"servers": list(plan.servers), "assign": assign, "uncovered": []}, sh.switched + 1
    _save(sh)
    log.info("VPN search: live now %s (%s)", " + ".join(plan.servers),
             ", ".join(f"{p}: {s}" for p, s in assign.items()))


async def _finish(sh: Search) -> None:
    """The end: the sites no connection serves pause with a note until the next regular search; none served at all:
    no server, their requests fail closed."""
    b = Board(sh, _refresh(sh))
    plan = b.current()
    uncovered = b.names(b.full & ~plan.covered)
    for pid in uncovered:
        vpn.pause(pid, "none", f"{sh.checked()}/{sh.total}")
    st = vpn.conn_state()
    odd = sorted(set(sh.odd) | (set() if sh.trigger in REGULAR else set(st.get("odd") or [])))
    vpn.update_conn(odd=odd)
    sh.plan = {"servers": list(plan.servers), "uncovered": uncovered,
               "assign": {p: x for p, x in (st.get("assign") or {}).items() if x in plan.servers and p in b.bit}}
    if plan.servers:
        sh.status = "done"
        return
    sh.status, sh.reason = "failed", "none" if sh.servers else "no_eu"
    vpn.update_conn(servers=[], assign={}, error=_failed_text(sh))
    await vpn.conn.disconnect()


async def _test_profile(sh: Search, job: jobs.Job) -> None:
    """A dedicated profile: every site through it; the refusing ones pause until the next test, never another server."""
    tgs = _refresh(sh)
    name = vpn.source()[1][0]
    sh.total = 1
    srv = Server(name, live=True)
    sh.servers.append(srv)
    t0 = time.monotonic()
    try:
        if name not in vpn.live_servers():
            await vpn.switch([name], {}, {name: tgs[0].host})
        route = vpn.conn.route(name) or await vpn.conn.start_live(name, tgs[0].host)
        if route is None:
            raise OSError(t("wireproxy did not open its port"))
    except (OSError, vpn.Unreachable) as e:
        srv.problem = "unreachable" if isinstance(e, vpn.Unreachable) else "broken"
        srv.detail = f"no connection to {e.host}" if isinstance(e, vpn.Unreachable) else _short(e)
        srv.status, srv.secs = "done", round(time.monotonic() - t0, 1)
        for tg in tgs:
            vpn.mark(tg.pid, name, srv.problem)
            vpn.pause(tg.pid, srv.problem, srv.detail)
        sh.status, sh.reason = "failed", "profile"
        sh.plan = {"servers": [], "assign": {}, "uncovered": [tg.pid for tg in tgs]}
        vpn.update_conn(error=_failed_text(sh))
        return
    srv.status = "testing"
    await asyncio.gather(*(_ask_one(sh, srv, route, tg, job) for tg in tgs if tg.on()))   # each its own limiter
    assign, refused = {}, []
    for tg in tgs:
        a = srv.answers.get(tg.pid)
        if a is not None and a.verdict in ANSWERED:
            vpn.unpause(tg.pid)
            assign[tg.pid] = name
        elif a is not None and a.verdict in REFUSED:
            vpn.pause(tg.pid, a.verdict, a.detail)
            refused.append(tg.pid)
    vpn.update_conn(servers=[name], assign=assign, error="")
    srv.status, srv.secs = "done", round(time.monotonic() - t0, 1)
    sh.plan = {"servers": [name], "assign": assign, "uncovered": refused}
    sh.status = "done" if assign else "failed"
    sh.reason = "" if assign else "profile"


# ------------------------------------------------------------------ budgets and pauses
def _spend(pid: str, n: int) -> None:
    q = _spent.setdefault(pid, deque())
    q.extend([_clock()] * n)


def _spent_hour(pid: str) -> deque[float]:
    q = _spent.setdefault(pid, deque())
    while q and _clock() - q[0] >= 3600:
        q.popleft()
    return q


def _cap(lim: RateLimiter, cost: int) -> int:
    """Requests the search may send a site an hour: a quarter of its hourly budget (at least one test's worth);
    NO_BUDGET_PER_HOUR without a budget."""
    return max(cost, lim.per_hour // SHARE if lim.per_hour else NO_BUDGET_PER_HOUR)


def _budget(tg: Target) -> tuple[str, float]:
    """("", 0) when the search may ask the site now, else (why, seconds to wait): pause (after a block), quarter (its
    share of the hour is used), full (the agents' requests and the search's are near the budget)."""
    lim, cost = tg.limiter, tg.cost
    if lim.cooling_down_s > lim.max_wait_s:
        return "pause", lim.cooling_down_s - lim.max_wait_s
    spent, cap = _spent_hour(tg.pid), _cap(lim, cost)
    if len(spent) + cost > cap:
        return "quarter", spent[min(len(spent) + cost - cap, len(spent)) - 1] + 3600 - _clock()
    if lim.per_hour and lim.last_hour + cost > lim.per_hour * BUDGET_FULL:
        return "full", lim.free_in(lim.last_hour + cost - int(lim.per_hour * BUDGET_FULL))
    return "", 0.0


async def _pace(sh: Search, tg: Target, job: jobs.Job) -> bool:
    """Wait until the site may be asked (module docstring); False when it was switched off meanwhile."""
    while tg.on():
        why, secs = _budget(tg)
        if not why:
            sh.wait = {}
            return True
        lim = tg.limiter
        sh.wait = {"pid": tg.pid, "kind": why, "until": time.time() + secs, "per_hour": lim.per_hour,
                   "cap": _cap(lim, tg.cost), "used": lim.last_hour}
        job.step = _wait_text(sh)
        await _sleep(min(max(secs, 1.0), WAIT_STEP_S))
    sh.wait = {}
    return False


def _short(exc: BaseException | None) -> str:
    return (str(exc) or type(exc).__name__)[:200] if exc is not None else ""


# ------------------------------------------------------------------ persistence
def _save(sh: Search) -> None:
    if sh.discarded:
        return
    skip = ("task", "discarded", "streak", "targets", "wait")
    d = {f.name: getattr(sh, f.name) for f in fields(sh) if f.name not in skip}
    d.update(sites=[asdict(s) for s in sh.sites], servers=[asdict(s) for s in sh.servers])
    set_setting(STATE_KEY, json.dumps(d, separators=(",", ":")))


def _stored() -> Search | None:
    """The last search from the setting (after a restart); one of the shuffles before it reads as far as it can."""
    try:
        d = json.loads(get_setting(STATE_KEY) or "null")
        if not isinstance(d, dict) or "servers" not in d:
            return None
        kept = d.pop("kept", None)
        if not isinstance(d.get("before"), list):
            d["before"] = [d["before"]] if isinstance(d.get("before"), str) else []
        if kept and "plan" not in d:
            d["plan"] = {"servers": [kept], "assign": {}, "uncovered": []}
        d["sites"] = [Site(**x) for x in d.get("sites") or []]
        d["servers"] = [Server(**{**x, "answers": {p: Answer(**a) for p, a in (x.get("answers") or {}).items()}})
                        for x in d.get("servers") or []]
        sh = Search(**d)
    except (ValueError, TypeError):
        return None
    if sh.status == "running" and not (sh is _run and running()):
        sh.status, sh.reason = "interrupted", "interrupted"     # stored while it ran, then the app restarted
    return sh


# ------------------------------------------------------------------ the view (Jinja global vpn_test)
def view() -> dict[str, Any]:
    """For settings/_vpn_test.html, in the viewer's language: the live connections, the schedule and the last search
    (this process's, else the stored one): its progress while it runs, then its result per site and per server."""
    sh, live = _run or _stored(), running()
    st, mode = vpn.conn_state(), vpn.source()[0]
    pend, nxt = pending(st), next_at()
    names = {pid: name for pid, name in vpn.users()}
    if sh is not None:
        names = {**{s.pid: s.name for s in sh.sites}, **names}
    servers = vpn.live_servers(st)
    on = [pid for pid, _ in vpn.users()]
    conns = [{"name": s, "since": _dt(vpn.since(s, st)), "connected": vpn.conn.route(s) is not None,
              "sites": ", ".join(names.get(p, p) for p in on if vpn.server_for(p, st) == s and not vpn.paused(p))}
             for s in servers]
    waiting = [{"name": names.get(p, p), "note": vpn.pause_note(pz, vpn.server_for(p, st)),
                "tone": "bad" if pz[0] == "none" else "warn"}
               for p in on if (pz := vpn.paused(p)) is not None]
    out: dict[str, Any] = {
        "live": live, "blocked": None if live else blocker(), "profile": mode == "profile",
        "connections": conns, "two": len(conns) > 1, "waiting": waiting, "connected": bool(vpn.conn.routes()),
        "error": st.get("error") or "", "interval": interval_h(), "next_at": _dt(nxt),
        "next_due": nxt is not None and nxt <= time.time(), "pending": _pending_text(pend) if pend else "",
        "run": sh is not None, "progress": "", "now": "", "best": "", "wait": "", "clock": "", "summary": "",
        "tone": "", "trigger": "", "finished_at": None, "rows": [], "servers": [], "more": 0}
    if sh is None:
        return out
    testing = [s for s in sh.servers if s.status != "done"] if live else []
    current = next((s for s in reversed(testing) if s.answers), testing[-1] if testing else None)   # the newest asked
    listed = [s for s in sh.servers if s.answers or s.problem or s.status != "done"]    # not the unneeded ones
    shown = listed[-SHOW_SERVERS:] if live else listed
    out.update(progress=_progress(sh) if live else "", now=current.name if current else "",
               best=_best_text(sh, names) if live else "", wait=_wait_text(sh) if live else "",
               clock=_clock_text(int(time.time() - sh.started)), summary="" if live else _summary(sh), tone=_tone(sh),
               trigger=_trigger_text(sh, names), finished_at=_dt(sh.finished or sh.started),
               rows=[_row(sh, s, current, live) for s in sh.sites
                     if not live or s.pid in on and current is not None and s.pid in current.answers],
               servers=[_server_row(sh, srv, names, live) for srv in shown],
               more=len(listed) - len(shown))
    return out


def _row(sh: Search, s: Site, srv: Server | None, live: bool) -> dict[str, Any]:
    """One site: live, its answer on the server under test; after the search, where it ended up. Without a message
    the page shows it as a chip."""
    row = {"pid": s.pid, "name": s.name, "label": "", "tone": "", "message": "", "spinner": False, "action": None}
    if live:
        a = srv.answers.get(s.pid) if srv is not None else None
        if a is None:
            row["label"] = t("not asked") if srv is not None and srv.status == "testing" else ""
        elif a.verdict == "asking":
            row["spinner"] = True
        elif a.verdict == "waiting":
            row["label"] = t("waiting")
        else:
            row["label"], row["tone"] = _pill(a.verdict)
            row["message"] = _message(s, a, srv.name, live) if a.verdict not in REFUSED else ""
        return row
    plan = sh.plan or {}
    if sh.status in ("stopped", "interrupted") and not plan:
        return row                                           # the summary says it
    if s.pid in (plan.get("uncovered") or []):
        said = [srv.answers[s.pid] for srv in sh.servers if s.pid in srv.answers]
        refused = sum(a.verdict in REFUSED for a in said)
        row.update(label=t("No server"), tone="bad",
                   message=t("Refused every server it was asked through ({n}); its requests wait for the next "
                             "regular search.", n=refused) if refused else
                   t("No server got as far as asking {site}; its requests wait for the next regular search.",
                     site=s.name))
        return row
    server = (plan.get("assign") or {}).get(s.pid)
    a = next((x.answers[s.pid] for x in reversed(sh.servers) if x.name == server and s.pid in x.answers), None)
    if server is None:
        return row
    row["label"], row["tone"] = _pill(a.verdict) if a is not None and a.verdict in ANSWERED else (t("Works"), "ok")
    if a is not None and a.verdict in ("site_error", "odd_page", "error"):
        row["message"] = _message(s, a, server, live)
        if a.params.get("fix") == "settings" and s.pid != vpn.GEIZHALS:
            row["action"] = {"label": t("Open {site} settings", site=s.name), "href": f"/settings/{s.pid}"}
    return row


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
    if v in ("skipped", "moved"):
        return t("not needed"), ""
    return t("Not tested"), ""


def _message(s: Site, a: Answer, server: str, live: bool) -> str:
    site, v, p = s.name, a.verdict, a.params
    if v in REFUSED:
        msg = vpn.reason_label(v) + (f" ({vpn.describe(a.detail)})" if a.detail else "")
        return msg[:1].upper() + msg[1:]
    if v == "site_error":
        return t("{site} answers through {server}, but with an error: {error}. That is not the server's fault.",
                 site=site, server=server, error=a.detail)
    if v == "odd_page":
        return t("{site} answered through {n} servers in a row with a page the app does not know. That points to a "
                 "change on the site, not to the VPN.", site=site, n=p.get("n", BOTCHECK_STREAK))
    if v == "off":
        return t("The VPN or the site was switched off meanwhile.")
    if v == "error":
        return t("The test failed: {error}", error=a.detail)
    return ""


def _server_row(sh: Search, srv: Server, names: dict[str, str], live: bool) -> dict[str, Any]:
    ok, no, total = len(srv.answered()), len(srv.refused()), len(sh.targets) or len(sh.sites)
    if srv.problem:
        label, tone = vpn.reason_label(srv.problem), "bad"
    elif srv.status != "done":
        label, tone = (t("starting …") if srv.status == "starting" else t("asking the sites …")), "brand"
    elif not ok + no:
        label, tone = t("not tested"), ""
    elif no:
        label, tone = t("Refused"), "warn"
    else:
        label, tone = (t("all answer"), "ok") if ok >= total else (t("{ok} of {n} answer", ok=ok, n=total), "ok")
    parts = [vpn.describe(srv.detail)] if srv.problem and srv.detail else []
    parts += [t("{site}: {reason}", site=names.get(pid, pid), reason=vpn.reason_label(a.verdict))
              + (f" ({vpn.describe(a.detail)})" if a.detail else "") for pid, a in srv.answers.items()
              if a.verdict in REFUSED]
    if no and ok:                                       # a pair's partner: who answered through it
        parts.append(t("answered: {sites}", sites=", ".join(names.get(p, p) for p in srv.answered())))
    return {"name": srv.name, "label": label, "tone": tone, "live": srv.live, "parts": parts,
            "kept": srv.name in vpn.live_servers() and (not live or srv.live),
            "secs": t("{n} s", n=_num(srv.secs)) if srv.secs is not None else ""}


def _progress(sh: Search) -> str:
    """The header line while a search runs."""
    if sh.profile:
        return t("Testing the profile {server}", server=sh.servers[-1].name) if sh.servers else t("Starting …")
    if not sh.total:
        return t("Starting …")
    text = t("Checked {n} of {total} EU servers", n=sh.checked(), total=sh.total)
    if sh.skipped:
        text += " · " + t("{k} of them skipped: refused lately", k=sh.skipped)
    return text


def _plan_text(plan: dict[str, Any], names: dict[str, str], total: int) -> str:
    servers, assign = plan.get("servers") or [], plan.get("assign") or {}
    if not servers:
        return t("none yet")
    if len(servers) == 1:
        n = sum(1 for s in assign.values() if s == servers[0])
        return t("{server} for all {n} sites", server=servers[0], n=n) if n == total and n > 1 else \
            t("{server} for {sites}", server=servers[0], sites=", ".join(names.get(p, p) for p, s in assign.items()))
    return " · ".join(t("{server} for {sites}", server=s,
                        sites=", ".join(names.get(p, p) for p, x in assign.items() if x == s)) for s in servers)


def _best_text(sh: Search, names: dict[str, str]) -> str:
    return _plan_text(sh.best, names, len(sh.targets) or len(sh.sites))


def _wait_text(sh: Search) -> str:
    w = sh.wait
    if not w:
        return ""
    site = sh.targets[w["pid"]].name if w.get("pid") in sh.targets else w.get("pid", "?")
    m = max(1, round((float(w.get("until") or 0) - time.time()) / 60))
    if w.get("kind") == "pause":
        return t("Waits for {site}: it pauses after a block, about {m} min left.", site=site, m=m)
    if w.get("kind") == "full":
        return t("Waits for {site}: {used} of its {per_hour} requests this hour are used; the last tenth stays for "
                 "the search agents. Free again in about {m} min.", site=site, used=w.get("used", 0),
                 per_hour=w.get("per_hour", 0), m=m)
    if w.get("per_hour"):
        return t("Waits for {site}: the search sends it at most a quarter of its {per_hour} requests an hour "
                 "({cap}). Free again in about {m} min.", site=site, per_hour=w["per_hour"], cap=w.get("cap", 0), m=m)
    return t("Waits for {site}: the search sends it at most {cap} requests an hour. Free again in about {m} min.",
             site=site, cap=w.get("cap", 0), m=m)


def _summary(sh: Search) -> str:
    if sh.status in ("stopped", "interrupted"):
        return _reason_text(sh.reason)
    if sh.status == "failed":
        return _failed_text(sh)
    plan = sh.plan or {}
    servers, uncovered = plan.get("servers") or [], plan.get("uncovered") or []
    names = {s.pid: s.name for s in sh.sites}
    if sh.profile and servers:
        n = len(plan.get("assign") or {}) + len(uncovered)
        return t("{server}: {ok} of {n} sites answer", server=servers[0], ok=n - len(uncovered), n=n)
    if not servers:
        return ""
    text = _plan_text(plan, names, len(plan.get("assign") or {}) + len(uncovered))
    if len(servers) == 2:
        text = t("Two servers: {plan}", plan=text)
    elif sh.trigger in REGULAR and servers[0] in sh.before and not uncovered:
        text = t("No other server suits every site; {server} stays", server=servers[0])
    if uncovered:
        text = t("No server for {sites} after {n} of {total} EU servers", sites=", ".join(names.get(p, p)
                                                                                           for p in uncovered),
                 n=sh.checked(), total=sh.total) + " · " + text
    return text


def _tone(sh: Search) -> str:
    if sh.status == "failed":
        return "bad"
    return "warn" if (sh.plan or {}).get("uncovered") else ""


def _failed_text(sh: Search) -> str:
    if sh.reason == "no_eu":
        return t("No server works: the collection has no EU server left to try")
    if sh.reason == "profile":
        return t("The profile does not work for any site")
    if sh.reason == "none":
        return t("No server works: none of the {n} checked answers any site", n=sh.checked())
    return t("The search failed: {error}", error=sh.reason)


def _trigger_text(sh: Search, names: dict[str, str]) -> str:
    if sh.trigger == "schedule":
        return t("Scheduled")
    if sh.trigger == "manual":
        return t("Started by you")
    if sh.trigger == "new":
        return t("New configs")
    site = names.get(sh.by) or ("Geizhals" if sh.by == vpn.GEIZHALS else sh.by)
    if sh.trigger == "switch":
        return t("{site} switched on", site=site)
    if sh.why == "none":
        return t("no server yet ({site} needed one)", site=site)
    return t("{site}: {server} {reason}", site=site, server=" + ".join(sh.before) or "?",
             reason=vpn.reason_label(sh.why)) + (f" ({vpn.describe(sh.detail)})" if sh.detail else "")


def _pending_text(p: dict[str, Any]) -> str:
    """Why the next search comes early: an automatic one waits for its turn, or one a restart cut off."""
    if p["trigger"] != "auto":
        return t("A search a restart cut off runs again.")
    site = dict(vpn.users()).get(p["by"], p["by"])
    if p["why"] == "none":
        return t("Brought forward: {site} found no server.", site=site)
    return t("Brought forward: {site} refused the server ({reason}).", site=site, reason=vpn.reason_label(p["why"]))


def _reason_text(reason: str) -> str:
    return {"uploaded": t("Stopped: new configs were uploaded"), "removed": t("Stopped: the configs were removed"),
            "interrupted": t("Interrupted by a restart of the app"),
            "stopped": t("Stopped by you; the servers found so far stay")}.get(reason, t("Stopped"))


def _clock_text(secs: int) -> str:
    """How long the search runs: "42 s", then "2:41 min"."""
    return t("{n} s", n=secs) if secs < 60 else t("{t} min", t=f"{secs // 60}:{secs % 60:02d}")


def _num(x: float) -> str:
    """2.9 → "2.9", in German "2,9"."""
    s = f"{x:g}"
    return s.replace(".", ",") if lang() == "de" else s


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None
