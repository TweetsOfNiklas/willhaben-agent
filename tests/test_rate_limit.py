import asyncio

import pytest

from app.platforms import ratelimit
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


async def test_reset_forgets_the_pause_and_the_hour():
    lim = RateLimiter(0, per_hour=5)
    async with lim:
        pass
    lim.penalize()
    assert lim.cooling_down_s > 0 and lim.last_hour == 1
    lim.reset()                                   # tests: a block in one test must not pause the next
    assert lim.cooling_down_s == 0 and lim.last_hour == 0 and lim._strikes == 0
    async with lim:                               # usable right away
        pass


# ------------------------------------------------------------------ a used-up budget: background work waits
@pytest.fixture
def quick_wake(monkeypatch):
    monkeypatch.setattr(ratelimit, "WAKE_S", 0.01)
    monkeypatch.setattr(ratelimit.random, "uniform", lambda a, b: 0.0)


async def _until(cond, timeout=2.0):
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never came true")


async def test_budget_full_says_when_it_is_free_again():
    lim = RateLimiter(0, per_hour=1, max_wait_s=1)
    async with lim:
        pass
    with pytest.raises(ratelimit.BudgetFull) as e:
        async with lim:
            pass
    assert isinstance(e.value, Blocked) and not e.value.sent and 3500 < e.value.free_in_s <= 3600


async def test_background_work_waits_for_the_budget_instead_of_failing(quick_wake):
    lim = RateLimiter(0, per_hour=1, max_wait_s=1)
    async with lim:
        pass
    seen = []
    bw = ratelimit.BudgetWait(lambda b: seen.append(dict(b.free_at())))

    async def background():
        with ratelimit.waiting(bw):
            async with lim:
                return "sent"

    task = asyncio.create_task(background())
    await _until(lambda: bw.sites)
    assert not task.done() and "willhaben" in bw.free_at()
    # a page request meanwhile is not stuck behind the waiting job: it fails fast, the lock is free
    assert not lim._lock.locked()
    with pytest.raises(ratelimit.BudgetFull):
        async with lim:
            pass
    lim._recent[0] -= 3600                         # the hour has passed: the request goes out by itself
    assert await asyncio.wait_for(task, 2) == "sent"
    assert lim.last_hour == 1 and not bw.sites and seen[-1] == {}


async def test_waiting_ends_with_its_block():
    assert not ratelimit.waits()
    with ratelimit.waiting():
        assert ratelimit.waits()
    assert not ratelimit.waits()


async def test_a_block_still_fails_while_waiting_for_the_budget():
    lim = RateLimiter(0, max_wait_s=1)
    lim.penalize()
    with ratelimit.waiting(), pytest.raises(Blocked) as e:   # 403/429: no waiting, as before
        async with lim:
            pass
    assert not isinstance(e.value, ratelimit.BudgetFull)
