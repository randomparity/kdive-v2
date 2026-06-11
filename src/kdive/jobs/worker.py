"""The worker tier: claim, heartbeat, dispatch, finalize (ADR-0018).

A :class:`Worker` owns an ``AsyncConnectionPool`` and processes one job per
:meth:`Worker.run_once`: ``dequeue`` claims and charges an attempt, a background
heartbeat renews the lease on a second connection, the registered handler runs on a
dispatch connection, and ``complete``/``fail`` finalize on fresh connections (so a
handler that poisoned its connection cannot block finalization). The worker holds no
transaction across the handler — a handler runs 30+ minutes and commits its own steps
(ADR-0018 decision 7).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry, JobHandler
from kdive.jobs.payloads import PayloadValidationError
from kdive.jobs.worker_telemetry import JobSpan, WorkerTelemetry
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

if TYPE_CHECKING:
    from kdive.health import Heartbeat

_log = logging.getLogger(__name__)
_CONTEXT_VALUE_MAX = 1000
_CONTEXT_KEY = re.compile(r"[^a-zA-Z0-9_.-]+")


class Worker:
    """Claims and dispatches durable jobs from the Postgres queue."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        registry: HandlerRegistry,
        *,
        worker_id: str,
        lease: timedelta = queue.DEFAULT_LEASE,
        heartbeat_interval: timedelta = timedelta(seconds=30),
        poll_interval: timedelta = timedelta(seconds=1),
        secret_registry: SecretRegistry,
        heartbeat: Heartbeat | None = None,
        heartbeat_tick: timedelta = timedelta(seconds=1),
        readiness: Callable[[], Awaitable[bool]] | None = None,
        telemetry: WorkerTelemetry | None = None,
    ) -> None:
        """Build a worker.

        Args:
            heartbeat: The ``/livez`` loop heartbeat bumped by a background ticker at
                ``heartbeat_tick`` cadence (ADR-0090 §5), so a long-running job never makes
                the worker read not-live. ``None`` in tests / a process without the aux
                listener.
            heartbeat_tick: The interval between background heartbeat bumps; must be well
                under the heartbeat's ``stale_after`` so a healthy loop never reads stale.
            readiness: A coroutine returning whether this process's backends are reachable
                (the worker/reconciler set: PG + MinIO, no OIDC). When it returns ``False``
                the worker pauses dequeuing new jobs while the backend is down rather than
                failing them (ADR-0090 §5). ``None`` disables the gate (always dequeues).
            telemetry: Per-job span + duration/queue-depth metrics; defaults to a no-op.

        Raises:
            ValueError: ``heartbeat_interval > lease / 3`` — too coarse to keep the
                lease alive across a missed beat, which would let the job be reclaimed
                and double-run; or ``pool.max_size < 2`` — a job in flight holds two
                connections at once (its handler's dispatch connection and the
                background heartbeat's), so a smaller pool would stall every dispatch
                until the heartbeat acquisition timed out.
        """
        if heartbeat_interval > lease / 3:
            raise ValueError(
                f"heartbeat_interval ({heartbeat_interval}) must be <= lease/3 "
                f"({lease / 3}); a coarser interval risks mid-job reclaim and double-run"
            )
        if pool.max_size < 2:
            raise ValueError(
                f"pool.max_size ({pool.max_size}) must be >= 2: a dispatched job holds "
                "its handler connection and a concurrent heartbeat connection at once"
            )
        self._pool = pool
        self._registry = registry
        self._worker_id = worker_id
        self._lease = lease
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        self._secret_registry = secret_registry
        self._heartbeat = heartbeat
        self._heartbeat_tick = heartbeat_tick.total_seconds()
        self._readiness = readiness
        self._telemetry = telemetry or WorkerTelemetry.disabled()

    async def run_once(self) -> Job | None:
        """Claim and dispatch one job; return it, or ``None`` if idle.

        Skips ``dequeue`` and returns ``None`` (idle) when this process's backends are
        not reachable (ADR-0090 §5): a not-ready worker pauses dequeuing new jobs while a
        needed backend is down rather than failing them. It also reads ``queue_paused`` at
        the top of the claim loop (ADR-0062): while the queue is paused the worker skips
        ``dequeue`` too. In both cases a job already in flight in :meth:`_dispatch` is
        untouched and keeps heart-beating — the freeze applies only to the claim of *new*
        work; the reconciler keeps enqueuing, and those jobs simply wait for resume.
        """
        if not await self._is_ready():
            return None
        async with self._pool.connection() as conn:
            if await queue.is_queue_paused(conn):
                return None
            job = await queue.dequeue(conn, self._worker_id, lease=self._lease)
            self._telemetry.observe_queue_depth(await queue.count_claimable(conn))
        if job is None:
            return None
        handler = self._registry.get(job.kind)
        if handler is None:
            async with self._pool.connection() as conn:
                await queue.fail(conn, job, ErrorCategory.NOT_IMPLEMENTED, terminal=True)
            _log.warning("no handler for job %s kind %s; dead-lettered", job.id, job.kind)
            return job
        await self._dispatch(job, handler)
        return job

    async def _is_ready(self) -> bool:
        """Return readiness via the injected gate; always ready when no gate is wired."""
        if self._readiness is None:
            return True
        return await self._readiness()

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once`, sleeping ``poll_interval`` when idle or after an error.

        The ``/livez`` heartbeat is bumped by a **background ticker task** at
        :attr:`_heartbeat_tick` cadence (ADR-0090 §5), *not* per job — so a single
        long-running job (a kernel build runs for minutes, far past the stale bound) never
        starves the heartbeat and makes the worker read not-live. Liveness tracks the
        event loop, not the work unit: while the loop is scheduling the ticker keeps it
        live; a genuinely wedged event loop stops the ticker too and ``/livez`` goes stale.
        A stuck *job* (vs a stuck loop) is caught by job-duration metrics and the lease
        fence, not by liveness.

        A transient per-iteration error (e.g. a brief database outage in ``dequeue``)
        is logged and the loop continues — a durable worker must not die on one bad
        iteration. The sleep after an error avoids a hot error-loop while the
        dependency recovers.
        """
        ticker = self._start_heartbeat_ticker(stop)
        try:
            await self._claim_loop(stop)
        finally:
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

    def _start_heartbeat_ticker(self, stop: asyncio.Event) -> asyncio.Task[None] | None:
        if self._heartbeat is None:
            return None
        return asyncio.create_task(_tick_until_stop(self._heartbeat, stop, self._heartbeat_tick))

    async def _claim_loop(self, stop: asyncio.Event) -> None:
        poll = self._poll_interval.total_seconds()
        while not stop.is_set():
            try:
                job = await self.run_once()
            except Exception:  # noqa: BLE001 - a durable worker survives a transient per-iteration error
                _log.exception("run_once failed; continuing after %ss", poll)
                await _sleep_until_stop(stop, poll)
                continue
            if job is None:
                await _sleep_until_stop(stop, poll)

    async def _dispatch(self, job: Job, handler: JobHandler) -> None:
        with self._telemetry.job_span(job.kind.value) as span:
            heartbeat = asyncio.create_task(self._heartbeat_loop(job.id))
            try:
                await self._run_handler(job, handler, span)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _run_handler(self, job: Job, handler: JobHandler, span: JobSpan) -> None:
        try:
            async with self._pool.connection() as conn:
                result_ref = await handler(conn, job)
        except Exception as exc:  # noqa: BLE001 - the worker turns any handler failure into a dead-letter/requeue
            span.set_outcome("error")
            category = _failure_category(exc)
            async with self._pool.connection() as conn:
                await queue.fail(
                    conn,
                    job,
                    category,
                    failure_context=_failure_context(exc, self._secret_registry),
                )
            _log.warning("job %s failed: %s", job.id, category, exc_info=True)
            return
        async with self._pool.connection() as conn:
            completed = await queue.complete(conn, job.id, self._worker_id, result_ref)
        if completed is None:
            _log.warning("job %s completed but was reclaimed; result dropped", job.id)

    async def _heartbeat_loop(self, job_id: UUID) -> None:
        """Renew the lease until cancelled, the fence misses, or a heartbeat errors.

        A failed heartbeat (DB blip, lost connection) is logged and ends the loop
        rather than escaping the task — the lease then lapses and the reconciler/
        next ``dequeue`` reclaims the job (the designed fallback). Letting it escape
        would re-raise out of ``_dispatch``'s ``finally`` and crash the worker.
        ``asyncio.CancelledError`` is a ``BaseException`` and so is not caught here,
        so normal cancellation still stops the loop.
        """
        interval = self._heartbeat_interval.total_seconds()
        try:
            async with self._pool.connection() as conn:
                while True:
                    await asyncio.sleep(interval)
                    if not await queue.heartbeat(conn, job_id, self._worker_id, lease=self._lease):
                        return
        except Exception:  # noqa: BLE001 - a failing heartbeat must not crash the worker; stop beating and let the lease lapse
            _log.warning(
                "heartbeat for job %s failed; stopping (lease will lapse)",
                job_id,
                exc_info=True,
            )


def _failure_category(exc: Exception) -> ErrorCategory:
    if isinstance(exc, CategorizedError):
        return exc.category
    if isinstance(exc, PayloadValidationError):
        return ErrorCategory.CONFIGURATION_ERROR
    return ErrorCategory.INFRASTRUCTURE_FAILURE


def _failure_context(exc: Exception, registry: SecretRegistry) -> dict[str, str]:
    redactor = Redactor(registry=registry)
    context = {"failure_message": _redacted(redactor, str(exc))}
    if isinstance(exc, CategorizedError):
        for key, value in exc.details.items():
            if _safe_detail(value):
                context[f"failure_detail_{_context_key(str(key))}"] = _redacted(
                    redactor, "" if value is None else str(value)
                )
    return context


def _safe_detail(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool | UUID)


def _context_key(key: str) -> str:
    cleaned = _CONTEXT_KEY.sub("_", key).strip("_.-")
    return cleaned or "value"


def _redacted(redactor: Redactor, value: str) -> str:
    return redactor.redact_text(value)[:_CONTEXT_VALUE_MAX]


async def _sleep_until_stop(stop: asyncio.Event, timeout: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout)


async def _tick_until_stop(heartbeat: Heartbeat, stop: asyncio.Event, interval: float) -> None:
    """Bump ``heartbeat`` every ``interval`` seconds until ``stop`` is set or cancelled.

    Runs concurrently with the claim loop so a long-running job never starves the
    ``/livez`` signal (ADR-0090 §5); a wedged event loop stops this ticker too, so a truly
    stuck worker still reads not-live.
    """
    heartbeat.tick()
    while not stop.is_set():
        await _sleep_until_stop(stop, interval)
        heartbeat.tick()
