"""Spec for the Q9 rate limiter: never exceed N requests in any rolling 60s window.

WHY this exists at all. The RAGAS judge is an LLM: scoring 48 golden questions on three
metrics costs ~240 Gemini calls, against a free-tier quota of **15 requests/minute and
500/day**. Low concurrency is not enough to stay under it — a single serial worker at
~1.5s per call issues ~40 calls/minute, well over the cap. So the pacing has to be
explicit, and it has to be *its own* testable unit rather than a `sleep()` sprinkled
through the scoring loop.

The chosen shape is a SLIDING WINDOW: remember the timestamps of the last `rpm` grants,
and if the window is full, wait until the oldest one falls out of it. That maps 1:1 onto
how the quota is actually phrased ("15 per minute"), unlike a leaky bucket whose
steady-state rate only *approximates* the same thing. It also allows a legitimate burst:
the first 15 calls go out immediately, which is exactly what the quota permits.

These specs drive the clock. `AsyncRateLimiter` takes injectable `clock`/`sleep`
callables so the test advances virtual time instead of really sleeping — a real-time
test of a 60-second window would either take minutes or be flaky, and neither is
acceptable in an offline suite that must stay fast and 0-skip.
"""

from __future__ import annotations

import pytest
from eval.throttle import AsyncRateLimiter


class FakeClock:
    """A virtual monotonic clock: `sleep` advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limiter(rpm: int) -> tuple[AsyncRateLimiter, FakeClock]:
    clock = FakeClock()
    return AsyncRateLimiter(rpm, clock=clock.time, sleep=clock.sleep), clock


async def test_burst_up_to_rpm_is_not_delayed() -> None:
    """The first `rpm` acquires are free — the quota allows a full-rate burst."""
    limiter, clock = _limiter(3)

    for _ in range(3):
        await limiter.acquire()

    assert clock.now == 0.0
    assert clock.slept == []


async def test_acquire_past_rpm_waits_for_the_window_to_slide() -> None:
    """The (rpm+1)-th call waits until the OLDEST grant is 60s old, then proceeds."""
    limiter, clock = _limiter(3)

    for _ in range(3):
        await limiter.acquire()
    await limiter.acquire()

    # Oldest grant was at t=0, so the 4th may go at t=60 — not a moment earlier.
    assert clock.now == pytest.approx(60.0)


async def test_window_slides_rather_than_resetting() -> None:
    """Once the old grants expire the limiter refills — it is a window, not a batch.

    After the 4th grant at t=60, the grants at t=0 are exactly 60s old and have left the
    window, so the 5th acquire proceeds immediately instead of waiting another minute.
    """
    limiter, clock = _limiter(3)

    for _ in range(4):
        await limiter.acquire()
    assert clock.now == pytest.approx(60.0)

    await limiter.acquire()
    assert clock.now == pytest.approx(60.0)


async def test_counts_every_granted_request() -> None:
    """`requests` counts grants — this is what the budget guard reports as spend."""
    limiter, _ = _limiter(5)
    assert limiter.requests == 0

    for _ in range(4):
        await limiter.acquire()

    assert limiter.requests == 4


async def test_rpm_zero_means_unlimited() -> None:
    """rpm <= 0 disables pacing, so offline tests can run the loop with no waiting."""
    limiter, clock = _limiter(0)

    for _ in range(50):
        await limiter.acquire()

    assert clock.now == 0.0
    assert limiter.requests == 50
