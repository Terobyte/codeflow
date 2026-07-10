"""Clock — everything in synapse that needs "now" goes through this, never `time.time()` or
`asyncio.sleep()` directly. That lets tests and the console runner move time deterministically
instead of relying on real sleeps (R10 — flaky e2e tests without a shared virtual clock)."""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Anything with a `now() -> float` (unix-epoch-like seconds) is a Clock."""

    def now(self) -> float:
        ...


class SystemClock:
    """Real wall-clock time — used by the live voice pipeline (app.py)."""

    def now(self) -> float:
        return time.time()


class FakeClock:
    """Virtual clock — used by tests and the console runner. Time only moves when
    `advance()` is called; there are no real sleeps anywhere in the offline test suite."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move a clock backwards")
        self._now += seconds
