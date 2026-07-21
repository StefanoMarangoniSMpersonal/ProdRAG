"""A sliding-window async rate limiter — keeps the Q9 judge inside the free-tier quota.

The RAGAS judge (`eval/ragas_eval.py`) is itself an LLM, so scoring the golden set costs
hundreds of Gemini calls against a quota of **15 requests/minute, 500/day**. Pacing has
to be deliberate: a serial scoring loop at ~1.5s per call would issue ~40 calls/minute
and start collecting 429s a third of the way in, after the budget was already spent.

Why a SLIDING WINDOW rather than the more common token bucket: the quota is literally
phrased "15 requests per minute", and a window enforces exactly that sentence — at most
`rpm` grants in any rolling 60 seconds. A token bucket refilling at rpm/60 tokens per
second only approximates the same limit, and reasoning about whether its burst capacity
can breach the real cap is exactly the kind of subtlety you don't want between you and a
day's worth of API budget. The window also permits the burst the quota genuinely allows:
the first `rpm` calls go out with no delay.

Implementation: a deque of the timestamps of the last `rpm` grants. If it is full, sleep
until the oldest timestamp is 60s old, then drop it. O(1) per acquire, no background task
to supervise.

`clock`/`sleep` are injectable so tests can drive virtual time (see test_throttle.py) —
a real-time test of a 60-second window would be slow, flaky, or both. Production uses
`time.monotonic` (immune to wall-clock jumps) and `asyncio.sleep` (yields the loop, so
waiting here never blocks other coroutines).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

WINDOW_SECONDS = 60.0


class AsyncRateLimiter:
    """Grants at most `rpm` acquires per rolling 60s window.

    `rpm <= 0` disables pacing entirely (used by offline tests, which must never wait).
    `requests` counts every grant, which is what the budget guard reports as spend.
    """

    def __init__(
        self,
        rpm: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rpm = rpm
        self._clock = clock
        self._sleep = sleep
        self._grants: deque[float] = deque()
        self._requests = 0
        # Serializes acquire() so concurrent callers can't each read a non-full window
        # and both slip through — without it the limiter would be advisory, not binding.
        self._lock = asyncio.Lock()

    @property
    def requests(self) -> int:
        """How many requests have been granted so far."""
        return self._requests

    async def acquire(self) -> None:
        """Wait (if needed) until issuing one more request stays within the quota."""
        if self._rpm <= 0:
            self._requests += 1
            return

        async with self._lock:
            if len(self._grants) >= self._rpm:
                # The window is full: the next grant may not happen until the oldest one
                # has aged out of it. Sleeping past that point would waste quota.
                wait = self._grants[0] + WINDOW_SECONDS - self._clock()
                if wait > 0:
                    await self._sleep(wait)
                self._grants.popleft()

            self._grants.append(self._clock())
            self._requests += 1
