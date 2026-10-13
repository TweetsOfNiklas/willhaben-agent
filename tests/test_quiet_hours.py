from datetime import datetime, timedelta, timezone

from app.db import SearchAgent
from app.services import quiet_hours as qh


def _vienna(h, m=0):
    return datetime(2026, 10, 9, h, m, tzinfo=qh.VIENNA).astimezone(timezone.utc)


def test_window_over_midnight_with_jitter():
    qh.save(True, "23:00", "06:00", "slow", 60)
    assert qh.state(now=_vienna(2, 0)).active
    assert not qh.state(now=_vienna(12, 0)).active
    st = qh.state(now=_vienna(2, 0))
    assert abs((st.end - _vienna(6, 0)).total_seconds()) <= qh.JITTER_MIN * 60


def test_slow_and_pause_modes():
    qh.save(True, "01:00", "07:00", "slow", 60)
    now = _vienna(4, 0)
    recent = SearchAgent(name="a", source_url="x", webapi_url="x", last_run_at=now - timedelta(minutes=10))
    old = SearchAgent(name="b", source_url="x", webapi_url="x", last_run_at=now - timedelta(minutes=61))
    assert qh.should_skip(recent, now) and not qh.should_skip(old, now)
    assert not qh.should_skip(recent, _vienna(12, 0))
    qh.save(True, "01:00", "07:00", "pause", 60)
    assert qh.should_skip(old, now)
    qh.save(False, "01:00", "07:00", "pause", 60)
    assert not qh.should_skip(old, now)


def test_last_end():
    qh.save(True, "01:00", "07:00", "pause", 60)
    assert abs((qh.last_end(_vienna(12, 0)) - _vienna(7, 0)).total_seconds()) <= qh.JITTER_MIN * 60
    assert qh.last_end(_vienna(4, 0)) < _vienna(0, 0)       # tonight's window still runs: the last one was yesterday
    qh.save(False, "01:00", "07:00", "pause", 60)
    assert qh.last_end(_vienna(12, 0)) is None


async def test_account_polling_waits_in_quiet_hours(monkeypatch):
    from app import scheduler

    calls = []

    class FakeSess:
        started, last_check = True, None

        async def check_login(self):
            calls.append("login")

        def chat_live_ok(self):
            return False

    async def fake_sync():
        calls.append("sync")

    monkeypatch.setattr(scheduler, "get_session", lambda: FakeSess())
    monkeypatch.setattr(scheduler.chats, "sync", fake_sync)
    monkeypatch.setattr(scheduler.chats, "last_sync_at", [None])
    qh.save(True, "01:00", "07:00", "pause", 60)
    monkeypatch.setattr(qh, "utcnow", lambda: _vienna(4, 0))
    await scheduler._session_check()
    await scheduler._chat_sync()
    assert calls == []
    monkeypatch.setattr(qh, "utcnow", lambda: _vienna(12, 0))
    await scheduler._session_check()
    await scheduler._chat_sync()
    assert calls == ["login", "sync"]
