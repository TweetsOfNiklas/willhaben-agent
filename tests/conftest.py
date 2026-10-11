import json
import os
import tempfile
from pathlib import Path

os.environ.update({
    "DATA_DIR": tempfile.mkdtemp(prefix="wh-test-"),
    "BROWSER_ENABLED": "0",
    "HA_LISTENER": "0",
    "TELEGRAM_LISTENER": "0",
    "HUMANIZE_CHAT": "0",       # auto replies answer immediately in tests (test_humanize covers the timing)
    "TELEGRAM_BOT_TOKEN": "",
    "REQUEST_MIN_GAP_S": "0",
    "PLATFORM_KLEINANZEIGEN_MIN_GAP_S": "0",
    "HA_URL": "",
    "HA_TOKEN": "",
    "HA_NOTIFY_SERVICE": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "DASHBOARD_PASSWORD": "",
})

import socket  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import create_engine  # noqa: E402

from app import db as dbmod  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Tests never reach the internet: a forgotten mock fails at once instead of waiting on a real site.
_LOCAL = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver")
_real_getaddrinfo, _real_connect, _real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex


def _local(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    return not isinstance(host, str) or host in _LOCAL or host.startswith("127.") or "/" in host   # "/": unix socket


def _getaddrinfo(host, *args, **kwargs):
    if not _local((host,)):
        raise socket.gaierror(f"tests must not reach the network ({host})")
    return _real_getaddrinfo(host, *args, **kwargs)


def _connect(self, address):
    if not _local(address):
        raise ConnectionRefusedError(f"tests must not reach the network ({address})")
    return _real_connect(self, address)


def _connect_ex(self, address):
    return _real_connect_ex(self, address) if _local(address) else 111     # ECONNREFUSED


socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = _getaddrinfo, _connect, _connect_ex


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def wait_jobs(timeout: float = 5.0) -> None:
    """TestClient runs the app's event loop in another thread: wait there until the background jobs are done."""
    import time

    from app.services import jobs
    end = time.monotonic() + timeout
    while jobs.busy() and time.monotonic() < end:
        time.sleep(0.02)
    assert not jobs.busy(), "background jobs still running"


async def finish_jobs() -> None:
    """In an async test: run the queued background jobs to the end."""
    import asyncio

    from app.services import jobs
    while jobs._active:
        await asyncio.gather(*(j.task for j in list(jobs._active)))


@pytest.fixture(autouse=True)
def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    dbmod.set_engine(engine)
    dbmod.init_db()
    from app.services import jobs
    from app.willhaben import recorder
    jobs.reset()                # background jobs are per event loop
    recorder.sync_writes = True  # one in-memory connection: willhaben recordings are written right away
    yield
    recorder.reset()
    recorder.sync_writes = False
    dbmod.set_engine(None)
