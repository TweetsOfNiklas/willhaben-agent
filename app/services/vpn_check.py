"""Settings → VPN → "Test the sites": every site with its VPN on searches once through its exit; a site that does
not answer properly moves on to the next exit until one works, and that one stays the site's exit for every request
after.

Sites run side by side, one task each; within a site one exit at a time, through the site's own adapter and client
(its limiter, hourly budget, checks and visitor) and its own Tunnel (order, marks, shared processes). Each try is a
Trial (httpclient): its requests go out through that one exit only, never on to another and never directly, and it
notes what they met. A refused or unreachable exit is marked as any request marks it; a page in place of the result
list (a bot check) as `botcheck`, for an hour. Caps per site and run: MAX_EXITS exits, SITE_S seconds, a quarter of its
hourly budget and half of what the hour has left. Stop is honoured between two exits (one probe or one try); new
configs and app shutdown cancel. One test at a time; state in memory while it runs (the page polls it) and in the
setting `vpn_test` (start, each site's end, the end) for reloads and restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from app import platforms
from app.db import get_setting, set_setting
from app.i18n import t
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
    sites: list[SiteRun]
    only: str | None = None
    status: str = "running"          # running | done | stopped | interrupted
    reason: str = ""                 # why it ends early: you | forgotten | uploaded | removed | interrupted
    started: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)


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
    drop that site's marks first. None when it started, else why not. No await before _run is set: two clicks or two
    tabs cannot start two tests."""
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
    _run = Run([SiteRun(p.id, p.name, gap=p.limiter.min_gap_s) for p in sites], only=only)
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
    (it names exits of the old configs)."""
    await cancel(reason)
    clear()


async def shutdown() -> None:
    await cancel("interrupted")


def clear() -> None:
    global _run
    if not running():
        _run = None
    set_setting(STATE_KEY, "")


def reset() -> None:
    """Tests: every test has its own event loop and database."""
    global _run
    _run = None


def _stop_verdict(reason: str) -> str:
    return "interrupted" if reason == "interrupted" else "stopped"


# ------------------------------------------------------------------ the run
async def _main(run: Run, sites: list[Platform]) -> None:
    by_id = {p.id: p for p in sites}
    try:
        async with jobs.track("vpn-test", t("VPN test"), lane="vpn", link="/settings/vpn") as job:
            job.total = len(run.sites)
            tasks = [asyncio.create_task(_guarded(run, s, by_id[s.pid], job)) for s in run.sites]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:      # cancel(): gather cancelled the sites, each ended as stopped
                await asyncio.wait(tasks)
            run.status = "done" if not run.reason else _stop_verdict(run.reason)
            ok = sum(s.verdict == "ok" for s in run.sites)
            job.result = _reason_text(run.reason) if run.reason else t("{ok} of {n} sites work through the VPN",
                                                                         ok=ok, n=len(run.sites))
            job.problems = 0 if run.reason else len(run.sites) - ok
    finally:
        run.finished = time.time()
        if run.status == "running":
            run.status = "interrupted"
        _save(run)


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
    cap = _cap(lim, cost)
    if cap < cost or _budget_full(lim, cost):
        return s.end("no_budget", used=lim.last_hour, per_hour=lim.per_hour)
    mode, names = vpn.source()
    if mode == "profile":
        vpn.unmark(p.id, names[0])              # the only exit: a test always asks it again
    tn.reset_failure()                          # the EXHAUSTED_S wait after a failed pass is not for a test
    s.status, s.started = "testing", time.time()
    s.before = tn.config if tn.current() else None
    spec, streak, t0 = p.keyword_search(KEYWORD), 0, time.monotonic()
    with tn.listen(s.heard):
        while True:
            if run.reason:
                return s.end("stopped", reason=run.reason)
            if vpn.tunnel_for(p.id) is None or not platforms.enabled(p.id):
                return s.end("off")
            if s.tried >= MAX_EXITS:
                return s.end("more", n=s.tried, left=_left(tn))
            if s.requests + cost > cap or _budget_full(lim, cost):
                return s.end("budget", n=s.requests, used=lim.last_hour, per_hour=lim.per_hour, left=_left(tn))
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
            tr, t1, exc = Trial(route), time.monotonic(), None
            with trial(tr):
                try:
                    await p.search(spec, rows=1)
                except PlatformError as e:
                    exc = e
            s.requests += tr.sent
            secs = round(time.monotonic() - t1, 1)
            v = _verdict(p, tr, exc, tn)
            if v == "ok":
                s.attempts.append(Attempt(route.name, "ok", "", tr.sent, secs))
                return s.end("ok", exit=route.name, current=route.name == s.before)
            if v in ("off", "paused"):
                return s.end(v, error=_short(exc))
            if v == "error":
                return s.end("error", error=_short(exc) or t("answered without a request (cache or replay)"))
            if v == "site_error":
                s.attempts.append(Attempt(route.name, "site_error", _short(exc), tr.sent, secs))
                return s.end("site_error", exit=route.name, error=_short(exc))
            if v == "botcheck":
                if streak + 1 >= BOTCHECK_STREAK and tn.current() == route:
                    s.attempts.append(Attempt(route.name, "botcheck", _short(exc), tr.sent, secs))
                    return s.end("odd_page", n=streak + 1)          # not marked: probably not the exits
                if await tn.failed(route, "botcheck", detail=_short(exc)):
                    streak += 1
                    s.settle(route.name, tr.sent, secs)
                continue                            # not marked: another request moved the tunnel meanwhile
            if v in ("blocked", "rate", "unreachable"):
                streak = 0
                s.settle(route.name, tr.sent, secs) # _send_trial marked it; the listener noted it
            # moved: another request left this exit; the next round asks the tunnel's new one


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


def _cap(lim: RateLimiter, cost: int) -> int:
    """Requests one site's run may send: a quarter of its hourly budget (at least one exit's worth), never more than
    half of what the hour has left; without a budget MAX_EXITS exits' worth."""
    if not lim.per_hour:
        return MAX_EXITS * cost
    return min(max(cost, lim.per_hour // 4), (lim.per_hour - lim.last_hour) // 2)


def _budget_full(lim: RateLimiter, cost: int) -> bool:
    """One more exit could take the site past BUDGET_FULL of its hour (the agents' requests count too)."""
    return bool(lim.per_hour) and lim.last_hour + cost > lim.per_hour * BUDGET_FULL


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


# ------------------------------------------------------------------ persistence
def _save(run: Run) -> None:
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
    """For settings/_vpn_test.html, in the viewer's language: the last test (this process's, else the stored one),
    whether it runs, why none can start, one row per site."""
    run, live = _run or _stored(), running()
    if run is not None and not live and run.status == "running":     # its task ended without saying so (a restart)
        run.status, run.reason = "interrupted", "interrupted"
        for s in run.sites:
            if s.status != "done":
                s.status, s.verdict, s.params = "done", "interrupted", {"reason": "interrupted"}
    eligible = {p.id: p for p in targets()}
    rows = [_row(s, live, s.pid in eligible) for s in run.sites] if run else []
    if not live:
        listed = {r["pid"] for r in rows}
        rows += [_idle_row(p, run is not None) for pid, p in eligible.items() if pid not in listed]
    return {"live": live, "stopping": live and bool(run.reason), "blocked": None if live else blocker(),
            "run": run is not None, "n": len(run.sites) if run else 0, "summary": _summary(run) if run else "",
            "finished_at": _dt(run.finished or run.started) if run else None,
            "elapsed": int(time.time() - run.started) if run else 0,
            "rows": rows, "max_exits": MAX_EXITS, "site_min": SITE_S // 60}


def _row(s: SiteRun, live: bool, eligible: bool) -> dict[str, Any]:
    v, p = s.verdict, s.params
    label, tone = ((t("waiting"), "") if s.status == "waiting" else ("", "") if s.status == "testing" else _pill(v))
    action = action_label = None
    if eligible and not live and s.status == "done":
        if v in ("more", "time") and p.get("left"):
            action, action_label = "more", t("Test {n} more", n=min(MAX_EXITS, p["left"]))
        elif v in ("none", "none_left"):
            action, action_label = "forget", t("Forget the blocks of {site} and test again", site=s.name)
        else:
            action, action_label = "test", t("Test now")
    hint = None
    if v in ("more", "time", "budget", "none") and s.tried >= 3 and all(a.verdict == "blocked" for a in s.attempts):
        hint = t("{site} refused every one of them (HTTP 403): the site probably blocks most servers of this VPN. "
                 "A dedicated IP as a profile can help.", site=s.name)
    end = s.finished or (time.time() if s.status == "testing" else s.started) or 0
    return {"pid": s.pid, "name": s.name, "status": s.status, "verdict": v, "label": label, "tone": tone,
            "message": _message(s), "hint": hint, "tried": s.tried, "requests": s.requests,
            "elapsed": int(end - s.started) if s.started else 0,
            "attempts": [{"exit": a.exit, "label": _attempt_label(a.verdict), "tone": _attempt_tone(a.verdict),
                          "detail": a.detail, "requests": a.requests, "secs": a.secs} for a in s.attempts],
            "action": action, "action_label": action_label}


def _idle_row(p: Platform, after_run: bool) -> dict[str, Any]:
    return {"pid": p.id, "name": p.name, "status": "idle", "verdict": "", "label": "", "tone": "",
            "message": t("Not part of the last test.") if after_run else t("Not tested yet."), "hint": None,
            "tried": 0, "requests": 0, "elapsed": 0, "attempts": [], "action": "test", "action_label": t("Test now")}


def _message(s: SiteRun) -> str:
    p, site, v = s.params, s.name, s.verdict
    if s.status == "waiting":
        return ""
    if s.status == "testing":
        if s.step == "asking" and s.exit:
            msg = t("Asking {site} through {exit} …", site=site, exit=s.exit)
            if s.gap >= 5:
                msg += " " + t("{site} allows one request every {s} s.", site=site, s=f"{s.gap:g}")
            return msg
        if s.step == "starting" and s.exit:
            return t("Starting exit {exit} …", exit=s.exit)
        return t("Testing …")
    if v == "ok":
        if p.get("current"):
            return t("The current exit {exit} works.", exit=p.get("exit"))
        return t("Works through {exit}. {site} uses this exit from now on.", exit=p.get("exit"), site=site)
    if v == "none":
        return t("No exit works for {site}: {n} tried, {k} skipped because they failed in the last hours. Its requests "
                 "pause; they never go out directly.", site=site, n=p.get("n", 0), k=p.get("k", 0))
    if v == "none_left":
        return t("No exit left to test: all {k} failed for {site} in the last hours. Its requests pause until a block "
                 "expires.", k=p.get("k", 0), site=site)
    if v == "profile":
        return t("{site} does not answer through the profile ({reason}). Its requests pause until you upload another "
                 "profile or a collection.", site=site, reason=vpn.reason_label(p.get("reason", "")))
    if v == "more":
        return t("No working exit among the {n} tried; {left} not tried yet.", n=p.get("n", 0), left=p.get("left", 0))
    if v == "time":
        return t("Stopped after {m} minutes; {left} exits not tried yet.", m=p.get("m", 0), left=p.get("left", 0))
    if v == "budget":
        return t("Stopped after {n} requests so that the search agents keep enough of the hourly budget of {site} "
                 "({used} of {per_hour} used); {left} exits not tried yet.", n=p.get("n", 0), site=site,
                 used=p.get("used", 0), per_hour=p.get("per_hour", 0), left=p.get("left", 0))
    if v == "no_budget":
        return t("Not tested: {site} has used most of its requests for this hour ({used} of {per_hour}).",
                 site=site, used=p.get("used", 0), per_hour=p.get("per_hour", 0))
    if v == "paused":
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
    return _reason_text(p.get("reason", ""))              # stopped | interrupted


def _pill(v: str) -> tuple[str, str]:
    if v == "ok":
        return t("Works"), "ok"
    if v in ("none", "none_left", "profile"):
        return t("No working exit"), "bad"
    if v in ("more", "time", "budget"):
        return t("None found yet"), "warn"
    if v in ("no_budget", "paused"):
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


def _summary(run: Run) -> str:
    if run.reason:
        return _reason_text(run.reason)
    return t("{ok} of {n} sites work through the VPN", ok=sum(s.verdict == "ok" for s in run.sites), n=len(run.sites))


def _reason_text(reason: str) -> str:
    return {"you": t("Stopped by you"), "forgotten": t("Stopped: the blocks were forgotten"),
            "uploaded": t("Stopped: new configs were uploaded"), "removed": t("Stopped: the configs were removed"),
            "interrupted": t("Interrupted by a restart of the app")}.get(reason, t("Stopped"))


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None
