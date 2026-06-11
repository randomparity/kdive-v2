"""Per-pass reconciler telemetry: a span + pass-duration / reconcile-lag (ADR-0090 §5).

Mirrors the worker's per-job telemetry for the reconciler's per-pass boundary: one span
per reconcile pass (``kind=INTERNAL``), a pass-duration histogram, and a reconcile-lag
gauge — the wall-clock gap between the *scheduled* pass start and the *actual* start,
which grows when a pass overruns its interval (a backlogged or wedged reconciler). Labels
are restricted to the allowlist (``outcome``); no tenant/principal identifier travels as a
label (ADR-0090 §4).

When no telemetry is wired (unit tests, a process without OTel),
:meth:`ReconcilerTelemetry.disabled` yields a no-op so the loop code path is unconditional.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from opentelemetry.trace import SpanKind, Status, StatusCode

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, Meter
    from opentelemetry.trace import Span, Tracer

#: Histogram bucket bounds (seconds) for one reconcile pass (DB sweeps, usually fast).
_DURATION_BUCKETS = (0.05, 0.25, 1.0, 5.0, 15.0, 30.0, 60.0)


class ReconcilerTelemetry:
    """Emit a span + pass-duration / reconcile-lag metrics for the reconciler (§5).

    Args:
        tracer: The tracer (from the facade's ``TracerProvider``) pass spans open on.
        meter: The meter (from the facade's ``MeterProvider``) instruments are made on.
    """

    def __init__(self, *, tracer: Tracer, meter: Meter) -> None:
        self._tracer = tracer
        self._enabled = True
        self._duration: Histogram = meter.create_histogram(
            "kdive.reconcile.duration",
            unit="s",
            description="Reconciler pass wall-clock duration.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )
        self._lag: Histogram = meter.create_histogram(
            "kdive.reconcile.lag",
            unit="s",
            description="Gap between the scheduled and actual reconcile-pass start.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )

    @classmethod
    def disabled(cls) -> ReconcilerTelemetry:
        """Return a no-op telemetry (no tracer/meter) for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    def observe_lag(self, lag_seconds: float) -> None:
        """Record the gap between the scheduled and actual pass start (no-op when disabled)."""
        if self._enabled and lag_seconds >= 0.0:
            self._lag.record(lag_seconds)

    @contextlib.contextmanager
    def pass_span(self) -> Iterator[_PassSpan]:
        """Open a span over one reconcile pass, recording duration on exit."""
        if not self._enabled:
            yield _PassSpan(None)
            return
        started = time.perf_counter()
        with self._tracer.start_as_current_span("reconcile/pass", kind=SpanKind.INTERNAL) as span:
            handle = _PassSpan(span)
            try:
                yield handle
            finally:
                self._record(handle, time.perf_counter() - started)

    def _record(self, handle: _PassSpan, elapsed: float) -> None:
        if handle.span is not None:
            handle.span.set_attribute("outcome", handle.outcome)
            if handle.outcome == "error":
                handle.span.set_status(Status(StatusCode.ERROR))
        self._duration.record(elapsed, {"outcome": handle.outcome})


class _PassSpan:
    """A per-pass span handle carrying the terminal outcome label."""

    def __init__(self, span: Span | None) -> None:
        self.span = span
        self.outcome = "ok"

    def set_outcome(self, outcome: str) -> None:
        """Stamp the pass's terminal outcome (``ok``/``error``)."""
        self.outcome = outcome
