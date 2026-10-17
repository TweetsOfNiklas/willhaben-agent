import random
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import Hit, SearchAgent, session, utcnow
from app.services import adaptive_pace as ap


def _at(hour):
    """Today (or yesterday, if still to come) at `hour` Vienna time, in UTC."""
    now = utcnow().astimezone(ap.VIENNA)
    day = now.date() if hour <= now.hour else now.date() - timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour, 30, tzinfo=ap.VIENNA).astimezone(timezone.utc)


def _agent(**kw):
    with session() as db:
        a = SearchAgent(name="Rad", source_url="x", webapi_url="x", interval_s=600, **kw)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a


def _hits(agent_id, hours):
    with session() as db:
        for i, h in enumerate(hours):
            ts = _at(h)
            db.add(Hit(agent_id=agent_id, ad_id=f"{agent_id}-{i}", heading="Ad", url="https://x", published_at=ts,
                       first_seen_at=ts + timedelta(minutes=5)))
        db.commit()


def test_slow_hours_get_a_factor_busy_ones_keep_the_interval():
    a = _agent()
    _hits(a.id, [9] * 60 + [10] * 60 + [20] * 4)
    p = ap.profile(a.id)
    assert not p.pooled and p.samples == 124
    assert p.factors[9] == p.factors[10] == 1.0                  # busy: the agent's own interval
    assert p.factors[3] == ap.max_factor() == 4                  # nothing at all: the slowest pace
    assert 1.0 < p.factors[20] <= 4


def test_too_few_hits_use_all_agents_then_nothing():
    a, b = _agent(), _agent()
    assert ap.profile(a.id) is ap.FLAT or ap.profile(a.id).factors == [1.0] * 24
    ap.reset()
    _hits(b.id, [8] * 80)
    p = ap.profile(a.id)
    assert p.pooled and p.samples == 80 and p.factors[8] == 1.0 and p.factors[20] == 4


def test_an_old_ad_counts_when_it_was_first_seen():
    a = _agent()
    with session() as db:
        for i in range(60):
            db.add(Hit(agent_id=a.id, ad_id=str(i), heading="Ad", url="https://x",
                       published_at=_at(3) - timedelta(days=5), first_seen_at=_at(14)))
        db.commit()
    assert ap.profile(a.id).factors[14] == 1.0


def test_skips_at_random_in_slow_hours_and_runs_after_a_long_wait():
    a = _agent()
    _hits(a.id, [9] * 100)
    slow = _at(21)
    a.last_run_at = slow - timedelta(minutes=10)
    rng = random.Random(1)
    skipped = sum(ap.should_skip(a, slow, rng) for _ in range(1000))
    assert 700 < skipped < 800                                    # factor 4: three in four left out
    a.last_run_at = slow - timedelta(seconds=600 * 4 * ap.FORCE_AFTER)
    assert not ap.should_skip(a, slow, rng)                       # waited long enough
    a.last_run_at = _at(9) - timedelta(minutes=10)
    assert not any(ap.should_skip(a, _at(9), rng) for _ in range(50))   # busy hour
    ap.save(False, 4)
    a.last_run_at = slow - timedelta(minutes=10)
    assert not any(ap.should_skip(a, slow, rng) for _ in range(50))


def test_settings_show_the_profile_and_save():
    from app.main import app

    a = _agent()
    _hits(a.id, [9] * 60)
    with TestClient(app) as c:
        page = c.get("/settings/quiet").text
        assert "Adaptive polling" in page and "data-adaptive-profile" in page and "from 60 hits" in page
        c.post("/settings/quiet", data={"pace": "1", "start": "01:00", "end": "07:00", "mode": "slow",
                                        "interval_min": "60", "adaptive_max": "8"})
    assert not ap.enabled() and ap.max_factor() == 8
