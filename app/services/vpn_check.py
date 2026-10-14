"""Settings → VPN → "Test the sites": every site with its VPN on searches once through its exit; a site that does
not answer properly moves on to the next exit until one works, and that one stays the site's exit for every request
after.

Sites run side by side, one task each; within a site one exit at a time, through the site's own adapter and client
(its limiter, hourly budget, checks and visitor) and its own Tunnel (order, marks, shared processes). Each try is a
Trial (httpclient): its requests go out through that one exit only, never on to another and never directly, and it
notes what they met. A refused or unreachable exit is marked as any request marks it; a page in place of the result
list (a bot check) as `botcheck`, for an hour. Caps per site and run: MAX_EXITS exits, SITE_S seconds, a quarter of its
hourly budget and half of what the hour has left. Stop is honoured between two exits (one probe or one try); new
configs and app shutdown cancel. One test at a time; a test of one site (a row's button) replaces that site's row of
the last result and keeps the others. State in memory while it runs (the page polls it) and in the setting `vpn_test`
(start, each site's end, the end) for reloads and restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from app import platforms
from app.db import get_setting, set_setting
from app.i18n import lang, t
from app.platforms import vpn
from app.platforms.base import Blocked, NotTheExit, Platform, PlatformError
from app.platforms.httpclient import ExitMoved, Trial, trial
from app.platforms.ratelimit import RateLimiter
from app.services import jobs
from app.willhaben import replay

log = logging.getLogger(__name__)

KEYWORD = "fahrrad"       # every site answers it; an empty result list counts as an answer too
MAX_EXITS = 10            # exits one site tries per run ("Test 10 more" goes on)
SITE_S = 300              # longest one site tests (checked between two exits)
BUDGET_FULL = 0.9         # no exit is started that could take the site past this share of its hourly budget
BOTCHECK_STREAK = 3       # bot-check pages through this many exits in a row: the site changed, not the exits
MOVED_STREAK = 3          # rounds in a row whose exit was gone before anything was sent: wireproxy keeps stopping
STOP_WAIT_S = 10          # cancel(): longest wait for the sites to leave their exits
KEEP_ATTEMPTS = 40        # per site in the setting
STATE_KEY = "vpn_test"


@dataclass
class Attempt:
    exit: str
    verdict: str                     # ok | blocked | rate | unreachable | broken | botcheck | site_error
    detail: str = ""                 # "HTTP 403", the network error, the site's message
    requests: int | None = None      # sent through it by the test (0: probe only; None: another request tried it)
    secs: float | None = None


@dataclass
class SiteRun:
    pid: str
    name: str
    gap: float = 0.0                 # the limiter's minimum gap (the "slow site" hint)
    status: str = "waiting"          # waiting | testing | done
    verdict: str = ""                # set by end(): see _pill()
    params: dict[str, Any] = field(default_factory=dict)   # the message's values; texts are rendered when viewed
    step: str = ""                   # starting | asking
    exit: str | None = None          # tried now
    before: str | None = None        # the exit in use when the test began
    attempts: list[Attempt] = field(default_factory=list)
    requests: int = 0
    started: float | None = None
    finished: float | None = None
    probe_t0: float = 0.0            # monotonic start of the probe heard last

    @property
    def tried(self) -> int:
        return len(self.attempts)

    def heard(self, exit_: str, event: str, detail: str) -> None:
        """Tunnel listener: every exit this site tries meanwhile, the test's own and its other requests'."""
        now = time.monotonic()
        if event == "trying":
            self.exit, self.step, self.probe_t0 = exit_, "starting", now
        elif event == "using":
            self.exit, self.step = exit_, "asking"
        else:
            probe = event in ("unreachable", "broken") and self.step == "starting" and self.exit == exit_
            self.attempts.append(Attempt(exit_, event, detail, 0 if probe else None,
                                         round(now - self.probe_t0, 1) if probe else None))

    def settle(self, exit_: str, requests: int, secs: float) -> None:
        """The test's own try of `exit_` ended in the attempt the listener just noted: add its cost."""
        if self.attempts and self.attempts[-1].exit == exit_ and self.attempts[-1].requests is None:
            self.attempts[-1].requests, self.attempts[-1].secs = requests, secs

    def end(self, verdict: str, **params: Any) -> None:
        self.status, self.verdict, self.params, self.step = "done", verdict, params, ""
        self.finished = time.time()


@dataclass
class Run:
    sites: list[SiteRun]             # one row per site: the ones tested now and, after `only`, the others' last result
    only: str | None = None          # one site tested again; the other rows are kept from before
    status: str = "running"          # running | done | stopped | interrupted
    reason: str = ""                 # why it ends early: you | forgotten | uploaded | removed | interrupted
    started: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    discarded: bool = field(default=False, repr=False, compare=False)    # its configs are gone: never saved again

    @property
    def tested(self) -> list[SiteRun]:
        return [s for s in self.sites if self.only in (None, s.pid)]


_run: Run | None = None


# ------------------------------------------------------------------ starting and stopping
def targets(only: str | None = None) -> list[Platform]:
    """The sites a test asks: switched on under Platforms (willhaben always is) with their VPN switch on."""
    return [p for p in platforms.all() if vpn.enabled(p.id) and only in (None, p.id)]


def blocker() -> str | None:
    """Why no test can start now; None when one can."""
    if replay.enabled():
        return t("Not available in the offline replay: the test needs real requests")
    if not vpn.source()[1]:
        return t("Upload WireGuard configs first")
    if vpn.runner.binary() is None:
        return t("wireproxy is not installed")
    if not targets():
        return t("Switch the VPN on for a site first")
    return None


def running() -> bool:
    return _run is not None and _run.task is not None and not _run.task.done()


def start(only: str | None = None, *, forget: bool = False) -> str | None:
    """Test every site with the VPN on, or `only`, in the background (the header lists the job "VPN test"); `forget`:
    drop that site's marks first. `only` replaces that site's row of the last result and keeps the others. None when
    it started, else why not. No await before _run is set: two clicks or two tabs cannot start two tests."""
    global _run
    if running():
        return t("A VPN test is running")
    if why := blocker():
        return why
    sites = targets(only)
    if not sites:
        return t("Switch the VPN on for a site first")
    if forget and only:
        vpn.forget(only)
    fresh = {p.id: SiteRun(p.id, p.name, gap=p.limiter.min_gap_s) for p in sites}
    rows = list(fresh.values())
    if only and (last := _run or _stored()) is not None:
        _settle(last)
        kept = {s.pid: s for s in last.sites}
        rows = [fresh.get(p.id) or kept[p.id] for p in targets() if p.id in fresh or p.id in kept]
    _run = Run(rows, only=only)
    _save(_run)
    _run.task = asyncio.create_task(_main(_run, sites))
    return None


def stop(reason: str = "you") -> None:
    """Stop after the exit each site is trying (one probe, or one try: eBay 3 requests at most)."""
    if running() and not _run.reason:
        _run.reason = reason


async def cancel(reason: str) -> None:
    """New or removed configs, app shutdown: cancel now and wait until the sites left their exits (≤ STOP_WAIT_S). The
    exit in progress gets no mark; a request cut off mid-way leaves its client a new transport (_drop_transport)."""
    if not running():
        return
    run = _run
    run.reason = reason
    run.task.cancel()
    await asyncio.wait({run.task}, timeout=STOP_WAIT_S)
    if run.task.done() and run.status == "running":      # cancelled before it began: _main never ran
        for s in run.sites:
            if s.status != "done":
                s.end(_stop_verdict(reason), reason=reason)
        run.status, run.finished = _stop_verdict(reason), time.time()
        _save(run)


async def source_changed(reason: str) -> None:
    """Before vpn.source_changed(): cancel a running test (its exits are about to stop) and forget the last result
    (it names exits of the old configs), also when the cancel fails."""
    try:
        await cancel(reason)
    finally:
        clear()


async def shutdown() -> None:
    await cancel("interrupted")


def clear() -> None:
    """Forget the last result. A test still winding down after cancel() saves nothing when it ends."""
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


def _settle(run: Run) -> None:
    """A run stored while it ran whose task is gone (a restart): it and its open sites show as interrupted."""
    if run.status != "running" or (run is _run and running()):
        return
    run.status, run.reason = "interrupted", "interrupted"
    for s in run.sites:
        if s.status != "done":
            s.status, s.verdict, s.params, s.step = "done", "interrupted", {"reason": "interrupted"}, ""


# ------------------------------------------------------------------ the run
async def _main(run: Run, sites: list[Platform]) -> None:
    global _run
    by_id = {p.id: p for p in sites}
    tested = [s for s in run.sites if s.pid in by_id]
    try:
        async with jobs.track("vpn-test", t("VPN test"), lane="vpn", link="/settings/vpn") as job:
            job.total = len(tested)
            tasks = [asyncio.create_task(_guarded(run, s, by_id[s.pid], job)) for s in tested]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:      # cancel(): gather cancelled the sites, each ended as stopped
                await asyncio.wait(tasks)
            run.status = "done" if not run.reason else _stop_verdict(run.reason)
            job.result = _reason_text(run.reason) if run.reason else _count_text(tested)
            job.problems = 0 if run.reason else sum(s.verdict != "ok" for s in tested)
    finally:
        run.finished = time.time()
        if run.status == "running":
            run.status = "interrupted"
        _save(run)
        if run.discarded and _run is run:
            _run = None


async def _guarded(run: Run, s: SiteRun, p: Platform, job: jobs.Job) -> None:
    try:
        await _site(run, s, p, job)
    except asyncio.CancelledError:
        s.end(_stop_verdict(run.reason), reason=run.reason or "you")
        raise
    except Exception as e:  # noqa: BLE001 - one site's surprise must not end the other sites' tests
        log.warning("VPN test of %s: %s", s.pid, e)
        s.end("error", error=_short(e))
    finally:
        job.done += 1
        _save(run)


async def _site(run: Run, s: SiteRun, p: Platform, job: jobs.Job) -> None:
    """One site: its search through one exit after another until the site answers (module docstring)."""
    tn, lim, cost = vpn.tunnel_for(p.id), p.limiter, p.vpn_test_requests
    if tn is None or not platforms.enabled(p.id):
        return s.end("off")
    if why := _paused(lim):                     # before anything changes: an untested site keeps its marks and wait
        return s.end("paused", error=why)
    cap, cap_why = _cap(lim, cost)
    if cap < cost or _budget_full(lim, cost):
        return s.end("no_budget", used=lim.last_hour, per_hour=lim.per_hour)
    mode, names = vpn.source()
    if mode == "profile":
        vpn.unmark(p.id, names[0])              # the only exit: a test always asks it again
    tn.reset_failure()                          # the EXHAUSTED_S wait after a failed pass is not for a test
    s.status, s.started = "testing", time.time()
    s.before = tn.config if tn.current() else None
    spec, streak, moved, t0 = p.keyword_search(KEYWORD), 0, 0, time.monotonic()
    with tn.listen(s.heard):
        while True:
            if run.reason:
                return s.end("stopped", reason=run.reason)
            if vpn.tunnel_for(p.id) is None or not platforms.enabled(p.id):
                return s.end("off")
            if s.tried >= MAX_EXITS:
                return s.end("more", n=s.tried, left=_left(tn))
            if s.requests + cost > cap or _budget_full(lim, cost):
                return s.end("budget", n=s.requests, used=lim.last_hour, per_hour=lim.per_hour, left=_left(tn),
                             why=cap_why if s.requests + cost > cap else "full")
            if why := _paused(lim):
                return s.end("paused", error=why)
            if time.monotonic() - t0 >= SITE_S:
                return s.end("time", m=SITE_S // 60, left=_left(tn))
            tried = s.tried
            try:
                route = await tn.ensure(probes=1)   # one probe at a time: Stop and the site's own requests wait ≤ one
            except PlatformError as e:
                if tn.failing:
                    return _no_exit(s, mode, str(e))
                if s.tried > tried:
                    continue                        # that exit failed (the listener noted it); more are left
                return s.end("error", error=_short(e))   # no exit tried and none failing: never loop on it
            if run.reason or not vpn.enabled(p.id):
                continue                            # decided at the top, before anything is sent
            s.exit, s.step = route.name, "asking"
            job.step = t("{site}: trying {exit}", site=s.name, exit=route.name)
            tr, t1, exc = Trial(route, limit=cost), time.monotonic(), None
            with trial(tr):
                try:
                    await p.search(spec, rows=1)
                except PlatformError as e:
                    exc = e
            s.requests += tr.sent
            secs = round(time.monotonic() - t1, 1)
            v = _verdict(p, tr, exc, tn)
            if v == "moved":                        # another request left this exit, or its wireproxy stopped
                moved = moved + 1 if not tr.sent else 0
                if moved >= MOVED_STREAK:           # never restart the same exit without end
                    return s.end("error", error=t("the exit stopped or changed {n} times in a row before a request "
                                                  "went out", n=moved))
                continue                            # the next round asks the tunnel's exit now
            moved = 0
            if v == "ok":
                s.attempts.append(Attempt(route.name, "ok", "", tr.sent, secs))
                return s.end("ok", exit=route.name, current=route.name == s.before)
            if v in ("off", "paused"):
                return s.end(v, error=_short(exc))
            if v == "error":
                return s.end("error", error=_short(exc) or t("answered without a request (cache or replay)"))
            if v == "site_error":                   # the row's message has the error; the attempt its status only
                s.attempts.append(Attempt(route.name, "site_error", _status(tr), tr.sent, secs))
                return s.end("site_error", exit=route.name, error=_short(exc),
                             fix="settings" if isinstance(exc, NotTheExit) else "")
            if v == "botcheck":
                if streak + 1 >= BOTCHECK_STREAK and tn.current() == route:
                    s.attempts.append(Attempt(route.name, "botcheck", _short(exc), tr.sent, secs))
                    return s.end("odd_page", n=streak + 1)          # not marked: probably not the exits
                if await tn.failed(route, "botcheck", detail=_short(exc)):
                    streak += 1
                    s.settle(route.name, tr.sent, secs)
                continue                            # not marked: another request moved the tunnel meanwhile
            streak = 0                              # blocked | rate | unreachable: _send_trial marked it
            s.settle(route.name, tr.sent, secs)


def _verdict(p: Platform, tr: Trial, exc: PlatformError | None, tn: vpn.Tunnel) -> str:
    """What one try says about its exit, checked in this order."""
    if not vpn.enabled(p.id) or not platforms.enabled(p.id):
        return "off"                    # also the Trial's "VPN is off": nothing went out directly
    if isinstance(exc, ExitMoved):
        return "moved"                  # another request left this exit before ours went out
    if tr.refused:
        return tr.refused               # blocked | rate | unreachable: marked by _send_trial
    if isinstance(exc, Blocked) and not exc.sent:
        return "paused"                 # the site's own limiter: a pause after a block, or the hour's budget
    if not tr.sent:
        return "error"                  # nothing went out: proves nothing about the exit
    if exc is None:
        return "ok" if tn.current() == tr.route else "moved"
    if isinstance(exc, NotTheExit):
        return "site_error"             # e.g. rotated credentials: every exit would get the same
    if tr.challenge or (tr.status == 200 and "json" not in tr.ctype):
        return "botcheck"               # Cloudflare's challenge (any status) or a page in place of the result list
    return "site_error"                 # the site answered through the exit with an error (5xx, a JSON error)


def _paused(lim: RateLimiter) -> str:
    """The limiter would refuse before sending: a pause after a block longer than it waits."""
    if lim.cooling_down_s > lim.max_wait_s:
        return t("{name} pause after a block, {s} s left", name=lim.name, s=f"{lim.cooling_down_s:.0f}")
    return ""


def _cap(lim: RateLimiter, cost: int) -> tuple[int, str]:
    """Requests one site's run may send, and which rule sets it: a quarter of its hourly budget (at least one exit's
    worth), never more than half of what the hour has left; without a budget MAX_EXITS exits' worth."""
    if not lim.per_hour:
        return MAX_EXITS * cost, "exits"
    quarter, half_left = max(cost, lim.per_hour // 4), (lim.per_hour - lim.last_hour) // 2
    return (quarter, "quarter") if quarter <= half_left else (half_left, "left")


def _budget_full(lim: RateLimiter, cost: int) -> bool:
    """One more exit could take the site past BUDGET_FULL of its hour (the agents' requests count too)."""
    return bool(lim.per_hour) and lim.last_hour + cost > lim.per_hour * BUDGET_FULL


def _wait_s(p: Platform) -> float:
    """Seconds until a test of this site could send again (its limiter's pause, its hourly budget); 0: now."""
    lim, cost = p.limiter, p.vpn_test_requests
    wait = lim.cooling_down_s if lim.cooling_down_s > lim.max_wait_s else 0.0
    if lim.per_hour:
        most = min(lim.per_hour - 2 * cost, int(lim.per_hour * BUDGET_FULL + 1e-9) - cost)  # in the hour, for a start
        if most < 0:
            return float("inf")         # a budget too small for one exit's try
        wait = max(wait, lim.free_in(lim.last_hour - most))
    return wait


def _left(tn: vpn.Tunnel) -> int:
    """Exits of a collection this site has neither tried nor marked (a profile has none to go on with)."""
    mode, names = vpn.source()
    return len([n for n in tn.candidates(mode, names) if n != tn.config]) if mode == "collection" else 0


def _no_exit(s: SiteRun, mode: str | None, error: str) -> None:
    """ensure() found no exit left and fails closed now: why, in the site's words."""
    if mode is None or not vpn.source()[1] or vpn.runner.binary() is None:
        return s.end("error", error=error)
    marks = vpn.marks(s.pid)
    if mode == "profile":
        return s.end("profile", reason=next(iter(marks.values()), (0, "unreachable"))[1])
    tried = {a.exit for a in s.attempts}
    if tried:
        return s.end("none", n=len(tried), k=len(set(marks) - tried))
    return s.end("none_left", k=len(marks))


def _short(exc: BaseException | None) -> str:
    return (str(exc) or type(exc).__name__)[:200] if exc is not None else ""


def _status(tr: Trial) -> str:
    """The answer's status as an attempt's detail ("HTTP 401"); nothing for a 200 (the message says what was wrong)."""
    return f"HTTP {tr.status}" if tr.status and tr.status != 200 else ""


# ------------------------------------------------------------------ persistence
def _save(run: Run) -> None:
    if run.discarded:
        return
    d = {"only": run.only, "status": run.status, "reason": run.reason, "started": run.started, "finished": run.finished,
         "sites": [{**asdict(s), "attempts": [asdict(a) for a in s.attempts[-KEEP_ATTEMPTS:]]} for s in run.sites]}
    set_setting(STATE_KEY, json.dumps(d, separators=(",", ":")))


def _stored() -> Run | None:
    """The last test from the setting (after a restart)."""
    try:
        d = json.loads(get_setting(STATE_KEY) or "null")
        if not isinstance(d, dict):
            return None
        sites = [SiteRun(**{**x, "attempts": [Attempt(**a) for a in x.get("attempts") or []]})
                 for x in d.get("sites") or []]
        return Run(sites, only=d.get("only"), status=d.get("status") or "done", reason=d.get("reason") or "",
                   started=float(d.get("started") or time.time()), finished=d.get("finished"))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ the view (Jinja global vpn_test)
def view() -> dict[str, Any]:
    """For settings/_vpn_test.html, in the viewer's language: the last result (this process's, else the stored one),
    whether a test runs, why none can start, one row per site. Before the first test there are no rows."""
    run, live = _run or _stored(), running()
    if run is None:
        return {"live": False, "stopping": False, "blocked": blocker(), "run": False, "progress": "", "summary": "",
                "finished_at": None, "elapsed": 0, "rows": []}
    _settle(run)
    eligible = {p.id: p for p in targets()}
    rows = [_row(run, s, live, eligible.get(s.pid)) for s in run.sites]
    if not live:
        listed = {r["pid"] for r in rows}
        rows += [_idle_row(p) for pid, p in eligible.items() if pid not in listed]
    return {"live": live, "stopping": live and bool(run.reason), "blocked": None if live else blocker(), "run": True,
            "progress": _progress(run) if live else "", "summary": _summary(run),
            "finished_at": _dt(run.finished or run.started), "elapsed": int(time.time() - run.started), "rows": rows}


def _row(run: Run, s: SiteRun, live: bool, p: Platform | None) -> dict[str, Any]:
    label, tone = (t("waiting"), "") if s.status == "waiting" else ("", "") if s.status == "testing" else _pill(s)
    action, note = (None, "") if live or s.status != "done" or p is None else _next_step(s, p)
    hint = None
    if (s.verdict in ("more", "time", "budget", "none") and s.tried >= 3
            and all(a.verdict == "blocked" for a in s.attempts)):
        hint = t("{site} refused every one of them (HTTP 403): the site probably blocks most servers of this VPN. "
                 "A dedicated IP as a profile can help.", site=s.name)
    end = s.finished or (time.time() if s.status == "testing" else s.started) or 0
    elapsed = t("{n} s", n=int(end - s.started)) if s.started else ""
    if s.status == "testing":
        counters = [t("exit {n}", n=s.tried + 1)] + ([_requests(s.requests)] if s.requests else []) + [elapsed]
    elif s.status == "done" and (s.tried or s.requests):
        counters = [t("{n} tried", n=s.tried), _requests(s.requests), elapsed]
    else:
        counters = []
    earlier = run.only not in (None, s.pid) and s.finished     # kept from an earlier test: when it was
    return {"pid": s.pid, "name": s.name, "status": s.status, "verdict": s.verdict, "label": label, "tone": tone,
            "message": _message(s), "hint": hint, "counters": " · ".join(c for c in counters if c),
            "kept": bool(earlier), "ago": _dt(s.finished) if earlier else None,
            "attempts": [{"exit": a.exit, "label": _attempt_label(a.verdict), "tone": _attempt_tone(a.verdict),
                          "detail": a.detail, "cost": _attempt_cost(a)} for a in s.attempts],
            "action": action, "note": note}


def _idle_row(p: Platform) -> dict[str, Any]:
    """A site with the VPN on that the last result does not list (switched on since)."""
    return {"pid": p.id, "name": p.name, "status": "idle", "verdict": "", "label": "", "tone": "",
            "message": t("Not tested yet."), "hint": None, "counters": "", "kept": False, "ago": None, "attempts": [],
            "action": {"kind": "test", "label": t("Test now")}, "note": ""}


def _next_step(s: SiteRun, p: Platform) -> tuple[dict[str, str] | None, str]:
    """The row's button (kind more | forget | test | settings, label, href) where it is a real next step, or a note
    on when a test is possible again."""
    v, prm = s.verdict, s.params
    if v == "ok":
        return None, ""
    if v == "site_error" and prm.get("fix") == "settings":
        return {"kind": "settings", "label": t("Open {site} settings", site=s.name), "href": f"/settings/{s.pid}"}, ""
    if wait := _wait_s(p):
        return None, ("" if wait == float("inf") else
                      t("A test is possible again in about {m} min.", m=max(1, math.ceil(wait / 60))))
    if v in ("none", "none_left"):
        return {"kind": "forget", "label": t("Forget its blocks and test again")}, ""
    if v in ("more", "time", "budget") and prm.get("left"):
        cost = p.vpn_test_requests
        n = max(1, min(MAX_EXITS, prm["left"], _cap(p.limiter, cost)[0] // cost))
        return {"kind": "more", "label": t("Test {n} more", n=n)}, ""
    return {"kind": "test", "label": t("Test now")}, ""


def _message(s: SiteRun) -> str:
    p, site, v = s.params, s.name, s.verdict
    if s.status == "waiting":
        return ""
    if s.status == "testing":
        if s.step == "asking" and s.exit:
            msg = t("Asking {site} through {exit} …", site=site, exit=s.exit)
            if s.gap >= 5:
                msg += " " + t("{site} allows one request every {s} s.", site=site, s=_num(s.gap))
            return msg
        if s.step == "starting" and s.exit:
            return t("Starting exit {exit} …", exit=s.exit)
        return t("Testing …")
    left = _left_text(p.get("left", 0))
    if v == "ok":
        if p.get("current"):
            return t("The current exit {exit} works.", exit=p.get("exit"))
        return t("Works through {exit}. {site} uses this exit from now on.", exit=p.get("exit"), site=site)
    if v == "none":                   # the sites list above says that its requests pause
        if p.get("k"):
            return t("No exit works: {n} tried, {k} skipped because they failed recently.", n=p.get("n", 0),
                     k=p["k"])
        return t("No exit works: all {n} tried.", n=p.get("n", 0))
    if v == "none_left":
        return t("No exit left to test: all {k} failed recently.", k=p.get("k", 0))
    if v == "profile":
        return t("{site} does not answer through the profile ({reason}). Upload another profile or a collection.",
                 site=site, reason=vpn.reason_label(p.get("reason", "")))
    if v == "more":
        return _join(t("No working exit among the {n} tried; one test tries at most {max}.", n=p.get("n", 0),
                       max=MAX_EXITS), left)
    if v == "time":
        return _join(t("Stopped after {m} minutes, the longest one site is tested.", m=p.get("m", 0)), left)
    if v == "budget":
        n, used, per_hour = _requests(p.get("n", 0)), p.get("used", 0), p.get("per_hour", 0)
        why = p.get("why")
        if why == "quarter":
            msg = t("Stopped after {requests}: one test uses at most a quarter of the {per_hour} requests {site} "
                    "allows per hour.", requests=n, per_hour=per_hour, site=site)
        elif why == "left":
            msg = t("Stopped after {requests}: one test uses at most half of what is left of the hour's {per_hour} "
                    "requests ({used} used).", requests=n, per_hour=per_hour, used=used)
        else:
            msg = t("Stopped after {requests}: {site} has used {used} of its {per_hour} requests this hour; the rest "
                    "stays for the search agents.", requests=n, site=site, used=used, per_hour=per_hour)
        return _join(msg, left)
    if v == "no_budget":
        return t("Not tested: {site} has used most of its requests for this hour ({used} of {per_hour}).",
                 site=site, used=p.get("used", 0), per_hour=p.get("per_hour", 0))
    if v == "paused":
        if not s.tried:
            return t("Not tested: {error}", error=p.get("error", ""))
        return t("Not tested further: {error}", error=p.get("error", ""))
    if v == "site_error":
        return t("{site} answers through {exit}, but with an error: {error}. That is not the exit's fault; it stays in "
                 "use.", site=site, exit=p.get("exit", ""), error=p.get("error", ""))
    if v == "odd_page":
        return t("{site} answered through {n} exits in a row with a page the app does not know. That points to a "
                 "change on the site, not to the VPN; the test stopped.", site=site, n=p.get("n", 0))
    if v == "off":
        return t("The VPN or the site was switched off; the test stopped.")
    if v == "error":
        return t("The test failed: {error}", error=p.get("error", ""))
    return ""                         # stopped | interrupted: the pill and the header say it


def _pill(s: SiteRun) -> tuple[str, str]:
    v = s.verdict
    if v == "ok":
        return t("Works"), "ok"
    if v in ("none", "none_left", "profile"):
        return t("No working exit"), "bad"
    if v in ("more", "time", "budget"):
        return t("No exit found yet"), "warn"
    if v == "no_budget" or (v == "paused" and not s.tried):
        return t("Not tested"), ""
    if v == "paused":
        return t("Paused"), "warn"
    if v in ("site_error", "odd_page"):
        return t("Site error"), "warn"
    if v == "off":
        return t("Off"), ""
    if v == "error":
        return t("Error"), "bad"
    return t("Stopped"), ""                                  # stopped | interrupted


def _attempt_label(v: str) -> str:
    return t("answers") if v == "ok" else t("answers with an error") if v == "site_error" else vpn.reason_label(v)


def _attempt_tone(v: str) -> str:
    return {"ok": "ok", "blocked": "bad", "botcheck": "bad", "broken": "bad"}.get(v, "warn")


def _attempt_cost(a: Attempt) -> list[str]:
    """Requests and seconds of one exit tried, for the test's own tries only (another request's has none)."""
    if a.requests is None:
        return []
    return [_requests(a.requests)] + ([t("{n} s", n=_num(a.secs))] if a.secs is not None else [])


def _progress(run: Run) -> str:
    """The header while a test runs: what changes when a site is done (the seconds tick apart from it)."""
    if run.reason:
        return t("Stopping after the exit each site is trying …")
    tested = run.tested
    if len(tested) == 1:
        return t("Testing {site}", site=tested[0].name)
    return t("{done} of {n} sites done", done=sum(s.status == "done" for s in tested), n=len(tested))


def _summary(run: Run) -> str:
    return _reason_text(run.reason) if run.reason else _count_text(run.sites)


def _count_text(sites: list[SiteRun]) -> str:
    if len(sites) == 1:
        return f"{sites[0].name}: {_pill(sites[0])[0]}"
    return t("{ok} of {n} sites work through the VPN", ok=sum(s.verdict == "ok" for s in sites), n=len(sites))


def _reason_text(reason: str) -> str:
    return {"you": t("Stopped by you"), "forgotten": t("Stopped: the blocks were forgotten"),
            "uploaded": t("Stopped: new configs were uploaded"), "removed": t("Stopped: the configs were removed"),
            "interrupted": t("Interrupted by a restart of the app")}.get(reason, t("Stopped"))


def _requests(n: int) -> str:
    return t("1 request") if n == 1 else t("{n} requests", n=n)


def _left_text(n: int) -> str:
    return "" if n <= 0 else t("1 exit not tried yet.") if n == 1 else t("{n} exits not tried yet.", n=n)


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _num(x: float) -> str:
    """2.9 → "2.9", in German "2,9"."""
    s = f"{x:g}"
    return s.replace(".", ",") if lang() == "de" else s


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None
