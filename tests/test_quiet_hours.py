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
