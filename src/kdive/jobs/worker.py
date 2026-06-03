"""The worker tier: claim, heartbeat, dispatch, finalize (ADR-0018, issue #9).

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
from datetime import timedelta
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry, JobHandler

_log = logging.getLogger(__name__)


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
    ) -> None:
        """Build a worker.

        Raises:
            ValueError: ``heartbeat_interval > lease / 3`` — too coarse to keep the
                lease alive across a missed beat, which would let the job be reclaimed
                and double-run.
        """
        if heartbeat_interval > lease / 3:
            raise ValueError(
                f"heartbeat_interval ({heartbeat_interval}) must be <= lease/3 "
                f"({lease / 3}); a coarser interval risks mid-job reclaim and double-run"
            )
        self._pool = pool
        self._registry = registry
        self._worker_id = worker_id
        self._lease = lease
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval

    async def run_once(self) -> Job | None:
        """Claim and dispatch one job; return it, or ``None`` if the queue is empty."""
        async with self._pool.connection() as conn:
            job = await queue.dequeue(conn, self._worker_id, lease=self._lease)
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

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once`, sleeping ``poll_interval`` when idle or after an error.

        A transient per-iteration error (e.g. a brief database outage in ``dequeue``)
        is logged and the loop continues — a durable worker must not die on one bad
        iteration. The sleep after an error avoids a hot error-loop while the
        dependency recovers.
        """
        poll = self._poll_interval.total_seconds()
        while not stop.is_set():
            try:
                job = await self.run_once()
            except Exception:  # noqa: BLE001 - a durable worker survives a transient per-iteration error
                _log.exception("run_once failed; continuing after %ss", poll)
                await asyncio.sleep(poll)
                continue
            if job is None:
                await asyncio.sleep(poll)

    async def _dispatch(self, job: Job, handler: JobHandler) -> None:
        heartbeat = asyncio.create_task(self._heartbeat_loop(job.id))
        try:
            try:
                async with self._pool.connection() as conn:
                    result_ref = await handler(conn, job)
            except Exception as exc:  # noqa: BLE001 - the worker turns any handler failure into a dead-letter/requeue
                category = (
                    exc.category
                    if isinstance(exc, CategorizedError)
                    else ErrorCategory.INFRASTRUCTURE_FAILURE
                )
                async with self._pool.connection() as conn:
                    await queue.fail(conn, job, category)
                _log.warning("job %s failed: %s", job.id, category)
                return
            async with self._pool.connection() as conn:
                completed = await queue.complete(conn, job.id, self._worker_id, result_ref)
            if completed is None:
                _log.warning("job %s completed but was reclaimed; result dropped", job.id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

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
            _log.warning("heartbeat for job %s failed; stopping (lease will lapse)", job_id)
