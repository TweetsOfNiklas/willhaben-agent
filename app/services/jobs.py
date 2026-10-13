"""Background jobs on the user's behalf (ad actions, publishing, chat messages), one after another per lane.

The page that starts a job returns at once; the header shows what is running right now, what is queued and the
last results (GET /jobs). Work that is not queued here shows up too: track() wraps a Claude call a page waits for
(price verdict, Geizhals search) or a task of its own (ad batch), and add_source() lists work that keeps its state
elsewhere (the product search, from its DB row). Lanes: "ads" (own ads: update, delete, publish, ...), "chat" (messages, sync) and "claude"
(answers and revisions Claude writes). Within a lane
jobs run strictly in order (an update must not overlap a delete run); the lanes run side by side, so a chat answer
never waits behind twenty deletes. The pacing towards willhaben stays the shared limiter's: requests of both lanes
still go out one by one. Jobs live in memory only: a restart drops the queue (callers persist their own markers and
clean them up at startup, see my_ads.recover, chats.recover_sending, ad_creator.recover_publishing).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db import utcnow
from app.i18n import t

log = logging.getLogger(__name__)

KEEP_FINISHED = 8
SHOW_FINISHED_S = 15 * 60      # finished jobs stay visible in the header this long


@dataclass
class Job:
    id: int
    kind: str                  # e.g. "myads-update": a still queued job of the same kind is reused, not doubled
    label: str
    lane: str = "ads"
    status: str = "queued"     # queued | running | done | failed
    step: str = ""             # what happens right now, e.g. "Delete „Fahrrad“"
    current: str | None = None  # the item being worked on right now, as a page marker key ("ad:123", "draft:5")
    done: int = 0
    total: int = 0
    result: str = ""
    problems: int = 0          # items that did not work out (job itself finished)
    link: str | None = None    # the page with the result (the finished-job notice opens it)
    quiet: bool = False        # no notice when it finishes: the page that started it shows the result itself
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def progress(self, done: int, step: str = "", current: str | None = None) -> None:
        self.done, self.step, self.current = done, step, current
        if current:     # what the job records from here on belongs to this item (timeline)
            from app.services import timeline
            timeline.update_context(**timeline.marker_ids(current))

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "lane": self.lane, "status": self.status, "step": self.step, "current": self.current,
                "done": self.done, "total": self.total, "result": self.result, "problems": self.problems,
                "link": self.link, "quiet": self.quiet, "finished_at": self.finished_at.isoformat() if self.finished_at else None}


LANES = ("ads", "chat", "claude")   # claude: drafts and revisions (no willhaben account request, can take a minute)

_ids = itertools.count(1)
_locks: dict[str, asyncio.Lock] = {}
_active: list[Job] = []                                  # running first, then queued, in order
_finished: deque[Job] = deque(maxlen=KEEP_FINISHED)      # newest first


_sources: list[Callable[[], dict[str, list[dict[str, Any]]]]] = []


def reset() -> None:
    """Forget everything (tests: every test has its own event loop)."""
    _locks.clear()
    _active.clear()
    _finished.clear()


def add_source(fn: Callable[[], dict[str, list[dict[str, Any]]]]) -> None:
    """Work that keeps its own state (the product search): `fn()` returns {"active": [view], "recent": [view]} in
    the shape of Job.view(), with string ids ("ps-12") so they never clash with job numbers."""
    if fn not in _sources:
        _sources.append(fn)


@asynccontextmanager
async def track(kind: str, label: str, *, lane: str = "claude", link: str | None = None, quiet: bool = False,
                current: str | None = None) -> AsyncIterator[Job]:
    """Show work that runs right here in the header like a job: running while the block runs, then among the
    finished ones. Not queued: it starts at once and holds no lane. The block may set job.result, or job.status =
    "failed" for a failure it handled itself; an exception marks it failed and goes on."""
    job = Job(next(_ids), kind, label, lane=lane, status="running", current=current, link=link, quiet=quiet)
    _active.append(job)
    try:
        yield job
        if job.status == "running":
            job.status = "done"
    except BaseException as e:
        job.status, job.result = "failed", str(e) or type(e).__name__
        raise
    finally:
        job.finished_at, job.step, job.current = utcnow(), "", None
        if job in _active:
            _active.remove(job)
        _finished.appendleft(job)


def submit(kind: str, label: str, run: Callable[[Job], Awaitable[str | None]], *, lane: str = "ads", total: int = 0,
           current: str | None = None, reuse_queued: bool = False) -> Job:
    """Queue `run(job)` in `lane`; it returns the result line shown in the header. Needs a running event loop."""
    if lane not in LANES:
        raise ValueError(lane)
    if reuse_queued:
        for j in _active:
            if j.kind == kind and j.status == "queued":
                return j
    job = Job(next(_ids), kind, label, lane=lane, total=total, current=current)
    _active.append(job)
    job.task = asyncio.create_task(_run(job, run))
    return job


JOB_TIMEOUT_S = 15 * 60     # longest a job may run (a big clean-up or update fits); the lane is freed afterwards


async def _run(job: Job, run: Callable[[Job], Awaitable[str | None]]) -> None:
    from app.services import timeline

    try:
        async with _locks.setdefault(job.lane, asyncio.Lock()):
            job.status = "running"
            # one timeline entry per job; Claude calls and willhaben requests inside it are its steps
            async with timeline.run("job", job.label, name=job.kind, **timeline.marker_ids(job.current)) as tl:
                try:
                    job.result = await asyncio.wait_for(run(job), JOB_TIMEOUT_S) or ""
                    job.status = "done"
                except TimeoutError:          # a stuck request (e.g. a willhaben page that never answers) frees the lane
                    log.warning("job %r timed out after %d s", job.label, JOB_TIMEOUT_S)
                    job.status, job.result = "failed", t("Took longer than {n} min – stopped", n=JOB_TIMEOUT_S // 60)
                except Exception as e:  # noqa: BLE001 - nobody awaits the task: the error goes to the header
                    log.warning("job %r failed: %s", job.label, e)
                    job.status, job.result = "failed", str(e)
                tl.finish(job.status == "done", job.result if job.status == "failed" else None,
                          payload={k: v for k, v in {"result": job.result, "problems": job.problems, "done": job.done,
                                                     "total": job.total, "lane": job.lane}.items() if v})
    finally:
        job.finished_at, job.step, job.current = utcnow(), "", None
        if job in _active:
            _active.remove(job)
        _finished.appendleft(job)


def working() -> set[str]:
    """Marker keys of the items running jobs work on right now (pages show a spinner instead of an hourglass)."""
    return {j.current for j in _active if j.status == "running" and j.current}


def busy(kind_prefix: str = "") -> bool:
    return any(j.kind.startswith(kind_prefix) for j in _active)


def snapshot() -> dict[str, Any]:
    now = utcnow()
    recent = [j for j in _finished if j.finished_at and (now - j.finished_at).total_seconds() < SHOW_FINISHED_S]
    active, done = [j.view() for j in _active], [j.view() for j in recent]
    for fn in _sources:
        try:
            more = fn()
        except Exception as e:  # noqa: BLE001 - a broken source must not take the header down
            log.warning("job source %r failed: %s", getattr(fn, "__name__", fn), e)
            continue
        active += more.get("active") or []
        done += more.get("recent") or []
    done.sort(key=lambda v: v.get("finished_at") or "", reverse=True)
    return {"active": active, "recent": done[:KEEP_FINISHED]}
