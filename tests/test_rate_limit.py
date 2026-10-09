import pytest

from app.willhaben.public import Blocked, RateLimiter


async def test_hourly_budget_fails_fast():
    lim = RateLimiter(0, per_hour=3, max_wait_s=1)
    for _ in range(3):
        async with lim:
            pass
    assert lim.last_hour == 3
    with pytest.raises(Blocked):
        async with lim:
            pass
    # the lock was released despite the error
    assert not lim._lock.locked()


async def test_cooldown_fails_fast():
    lim = RateLimiter(0, max_wait_s=1)
    lim.penalize()
    with pytest.raises(Blocked):
        async with lim:
            pass
    assert lim.cooling_down_s > 50
