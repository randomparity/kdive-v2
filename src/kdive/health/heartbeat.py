"""The affirmative ``/livez`` loop heartbeat (ADR-0090 §5).

``/livez`` is an *affirmative* liveness signal, not liveness-by-timeout, and it tracks
the **loop, not the work unit**. The owning loop bumps :meth:`Heartbeat.tick` at its
scheduling/poll granularity (it woke, is dequeuing, has not deadlocked) — *not* at job
completion, since kdive jobs legitimately run for minutes. ``/livez`` is live while the
last tick is within :attr:`stale_after` seconds; a genuinely stuck job is caught by
job-duration metrics and per-job timeouts, not by liveness.
"""

from __future__ import annotations

import time


class Heartbeat:
    """A monotonic last-tick timestamp the aux ``/livez`` handler reads.

    Args:
        stale_after: Seconds after the last :meth:`tick` at which liveness goes stale.
        now: Monotonic clock (injected for tests); defaults to :func:`time.monotonic`.
    """

    def __init__(self, *, stale_after: float, now: object = time.monotonic) -> None:
        self._stale_after = stale_after
        self._now = now
        self._last_tick = self._read_now()

    def _read_now(self) -> float:
        return float(self._now())  # ty: ignore[call-non-callable]

    def tick(self) -> None:
        """Record that the owning loop made a scheduling pass (woke and is progressing)."""
        self._last_tick = self._read_now()

    def is_live(self) -> bool:
        """Return whether the last tick is within :attr:`stale_after` seconds."""
        return (self._read_now() - self._last_tick) < self._stale_after
