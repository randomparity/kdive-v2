"""Per-job worker telemetry: a span + job-duration / queue-depth metrics (ADR-0090 §5).

Mirrors the server's :class:`~kdive.mcp.middleware.TelemetryMiddleware` for the worker's
dispatch boundary: one span per job carrying only allowlisted labels (``job_kind`` and
``outcome`` — never a tenant/principal identifier, ADR-0090 §4), a job-duration
histogram, and a queue-depth gauge sampled at each poll. Secret values that reach a span
attribute are scrubbed by the redacting span exporter on export (the facade, §4).

The instruments are built from the process meter/tracer (the facade's providers). When no
telemetry is wired (unit tests, a process without OTel), :meth:`WorkerTelemetry.disabled`
yields a no-op so the worker code path is unconditional.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from opentelemetry.trace import SpanKind, Status, StatusCode

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, Meter, UpDownCounter
    from opentelemetry.trace import Span, Tracer

#: Histogram bucket bounds (seconds) for per-job duration — kdive jobs run from
#: sub-second (teardown) to many minutes (kernel build), so the upper buckets are coarse.
_DURATION_BUCKETS = (0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0)


class WorkerTelemetry:
    """Emit a span + job-duration / queue-depth metrics for the worker (ADR-0090 §5).

    Args:
        tracer: The tracer (from the facade's ``TracerProvider``) job spans open on.
        meter: The meter (from the facade's ``MeterProvider``) instruments are made on.
    """

    def __init__(self, *, tracer: Tracer, meter: Meter) -> None:
        self._tracer = tracer
        self._enabled = True
        self._duration: Histogram = meter.create_histogram(
            "kdive.job.duration",
            unit="s",
            description="Worker job-handler wall-clock duration.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )
        self._queue_depth: UpDownCounter = meter.create_up_down_counter(
            "kdive.job.queue.depth",
            unit="1",
            description="Jobs claimable from the queue at the last poll.",
        )

    @classmethod
    def disabled(cls) -> WorkerTelemetry:
        """Return a no-op telemetry (no tracer/meter) for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    @contextlib.contextmanager
    def job_span(self, job_kind: str) -> Iterator[JobSpan]:
        """Open a span over one job dispatch, recording duration on exit.

        Yields a :class:`JobSpan` whose :meth:`JobSpan.set_outcome` stamps the terminal
        outcome label; the duration histogram is recorded with that outcome on close.
        """
        if not self._enabled:
            yield JobSpan(None, job_kind)
            return
        started = time.perf_counter()
        with self._tracer.start_as_current_span(
            f"job/{job_kind}", kind=SpanKind.CONSUMER, attributes={"job_kind": job_kind}
        ) as span:
            handle = JobSpan(span, job_kind)
            try:
                yield handle
            finally:
                self._record(handle, time.perf_counter() - started)

    def _record(self, handle: JobSpan, elapsed: float) -> None:
        labels = {"job_kind": handle.job_kind, "outcome": handle.outcome}
        if handle.span is not None:
            handle.span.set_attribute("outcome", handle.outcome)
            if handle.outcome == "error":
                handle.span.set_status(Status(StatusCode.ERROR))
        self._duration.record(elapsed, labels)

    def observe_queue_depth(self, claimable: int) -> None:
        """Record the queue depth observed at a poll (no-op when disabled)."""
        if self._enabled:
            self._queue_depth.add(claimable)


class JobSpan:
    """A per-job span handle carrying the terminal outcome label."""

    def __init__(self, span: Span | None, job_kind: str) -> None:
        self.span = span
        self.job_kind = job_kind
        self.outcome = "ok"

    def set_outcome(self, outcome: str) -> None:
        """Stamp the job's terminal outcome (``ok``/``error``) for the duration label."""
        self.outcome = outcome
