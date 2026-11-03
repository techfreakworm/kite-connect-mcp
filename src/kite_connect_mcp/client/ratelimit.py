"""Client-side rate limits, kept just under Kite's published limits so the API never has to say 429.

Kite (https://kite.trade/docs/connect/v3/exceptions/, "API rate limit"): quote 1 request/s, historical candles 3/s,
order placement 10/s, everything else 10/s; and at most 400 orders a minute. The limiter is a sliding window: at most
`limit` calls in any `period` seconds, which (unlike a token bucket) can never burst past the limit at a window edge.
The periods are 10% longer than Kite's to leave a margin for clock and network jitter.

The limits are per API key at Kite, so another program using the same key shares them: a 429 can still happen, and the
HTTP client backs off on it for reads (never for writes).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

MARGIN = 1.1  # every window is this much longer than Kite's


@dataclass(frozen=True)
class Window:
    limit: int
    period_s: float


GROUPS: dict[str, tuple[Window, ...]] = {
    "quote": (Window(1, 1.0 * MARGIN),),
    "historical": (Window(3, 1.0 * MARGIN),),
    "order": (Window(10, 1.0 * MARGIN), Window(400, 60.0 * MARGIN)),
    "other": (Window(10, 1.0 * MARGIN),),
}


class SlidingWindowLimiter:
    """At most `window.limit` acquisitions in any `window.period_s` seconds, for every window given."""

    def __init__(self, windows: tuple[Window, ...], *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep):
        self._windows = windows
        self._stamps: list[deque[float]] = [deque() for _ in windows]
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    def _wait_needed(self, now: float) -> float:
        wait = 0.0
        for window, stamps in zip(self._windows, self._stamps, strict=True):
            while stamps and now - stamps[0] >= window.period_s:
                stamps.popleft()
            if len(stamps) >= window.limit:
                wait = max(wait, window.period_s - (now - stamps[0]))
        return wait

    async def acquire(self) -> float:
        """Wait until a call is allowed and record it. Returns how long it waited (seconds)."""
        waited = 0.0
        async with self._lock:
            while True:
                now = self._clock()
                wait = self._wait_needed(now)
                if wait <= 0:
                    for stamps in self._stamps:
                        stamps.append(now)
                    return waited
                await self._sleep(wait)
                waited += wait


class RateLimiter:
    """One sliding-window limiter per Kite rate-limit group."""

    def __init__(self, *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep, groups=GROUPS):
        self._limiters = {
            name: SlidingWindowLimiter(windows, clock=clock, sleep=sleep) for name, windows in groups.items()
        }

    async def acquire(self, group: str) -> float:
        return await self._limiters.get(group, self._limiters["other"]).acquire()
