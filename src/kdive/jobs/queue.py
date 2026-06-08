"""Connection-scoped operations over the durable ``jobs`` queue (ADR-0018, issue #9).

``enqueue`` admits a job idempotently on ``dedup_key``; ``dequeue`` claims the oldest
eligible job with ``FOR UPDATE SKIP LOCKED``, charging an attempt and reclaiming a
lapsed lease; ``heartbeat`` renews a lease; ``complete`` and ``fail`` finalize a
claimed job. Every post-claim write is fenced on ``worker_id`` + ``state = 'running'``
so a worker that lost its lease cannot mutate a job another worker now owns. Each
function wraps its statements in ``conn.transaction()`` so it self-commits on any
connection, and all assume READ COMMITTED (psycopg's default).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job, JobAuthorizing, JobKind
from kdive.jobs.payloads import (
    Authorizing,
    PayloadModel,
    dump_authorizing,
    dump_payload,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE = timedelta(minutes=5)


async def enqueue(
    conn: AsyncConnection,
    kind: JobKind,
    payload: PayloadModel | dict[str, Any],
    authorizing: Authorizing | JobAuthorizing | dict[str, Any],
    dedup_key: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Job:
    """Admit a job, returning the existing one on a ``dedup_key`` conflict.

    Upsert-then-fetch: ``INSERT … ON CONFLICT (dedup_key) DO NOTHING`` then
    ``SELECT … WHERE dedup_key = …`` in one transaction, so a re-issue returns the
    **same** job (in whatever state it has since reached) and never enqueues a
    duplicate. ``DO NOTHING RETURNING`` is avoided — it returns no row on conflict.

    Raises:
        ValueError: ``max_attempts < 1`` (a job that ``dequeue`` could never claim).
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    payload = dump_payload(kind, payload)
    authorizing = dump_authorizing(authorizing)
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
            "VALUES (%s, %s, 'queued', %s, %s, %s) "
            "ON CONFLICT (dedup_key) DO NOTHING",
            (kind, Jsonb(payload), max_attempts, Jsonb(authorizing), dedup_key),
        )
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    if row is None:  # Invariant: we just inserted the row, or it already existed.
        raise RuntimeError(f"enqueue found no job for dedup_key {dedup_key!r}")
    return Job.model_validate(row)


async def dequeue(
    conn: AsyncConnection, worker_id: str, *, lease: timedelta = DEFAULT_LEASE
) -> Job | None:
    """Claim the oldest eligible job for ``worker_id``, charging an attempt.

    Eligible: ``queued``, or ``running`` with a lapsed lease (an abandoned job), and
    ``attempt < max_attempts``. The single ``UPDATE`` sets ``running``/``worker_id``/
    lease/``heartbeat_at`` and ``attempt = attempt + 1`` (charging the claim bounds
    retries across worker death). ``FOR UPDATE SKIP LOCKED`` lets parallel workers
    claim disjoint rows without blocking. ``now()`` is the database clock, so no
    worker clocks need to agree.

    Returns:
        The claimed :class:`Job`, or ``None`` when nothing is eligible.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE jobs SET "
            "    state = 'running', worker_id = %s, attempt = attempt + 1, "
            "    lease_expires_at = now() + %s, heartbeat_at = now() "
            "WHERE id = ( "
            "    SELECT id FROM jobs "
            "    WHERE (state = 'queued' "
            "           OR (state = 'running' AND lease_expires_at < now())) "
            "      AND attempt < max_attempts "
            "    ORDER BY created_at "
            "    FOR UPDATE SKIP LOCKED "
            "    LIMIT 1 "
            ") "
            "RETURNING *",
            (worker_id, lease),
        )
        row = await cur.fetchone()
    return None if row is None else Job.model_validate(row)


async def heartbeat(
    conn: AsyncConnection, job_id: UUID, worker_id: str, *, lease: timedelta = DEFAULT_LEASE
) -> bool:
    """Renew the lease for ``job_id`` if ``worker_id`` still owns the running job.

    Returns:
        ``True`` when a row matched; ``False`` when the job is no longer this worker's
        running job (reclaimed, completed, failed, or canceled).
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE jobs SET heartbeat_at = now(), lease_expires_at = now() + %s "
            "WHERE id = %s AND worker_id = %s AND state = 'running' "
            "RETURNING id",
            (lease, job_id, worker_id),
        )
        row = await cur.fetchone()
    return row is not None


async def complete(
    conn: AsyncConnection, job_id: UUID, worker_id: str, result_ref: str | None
) -> Job | None:
    """Mark ``job_id`` succeeded with ``result_ref`` if ``worker_id`` still owns it.

    Returns:
        The updated :class:`Job`, or ``None`` if the fence did not match (the worker
        lost the job to a reclaim; the caller logs and drops the result).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE jobs SET state = 'succeeded', result_ref = %s "
            "WHERE id = %s AND worker_id = %s AND state = 'running' "
            "RETURNING *",
            (result_ref, job_id, worker_id),
        )
        row = await cur.fetchone()
    return None if row is None else Job.model_validate(row)


async def fail(
    conn: AsyncConnection,
    job: Job,
    error_category: ErrorCategory,
    *,
    terminal: bool = False,
    failure_context: Mapping[str, str] | None = None,
) -> Job:
    """Dead-letter or requeue a claimed ``job``, fenced on its ``worker_id``.

    Dead-letters (``running → failed`` with ``error_category``) when ``terminal`` is
    set (a non-retryable failure, e.g. no handler for the kind) or the already-charged
    ``job.attempt`` has reached ``job.max_attempts``; otherwise requeues
    (``running → queued``, clearing the lease) for another attempt.

    Returns:
        The job's post-write state, or the unchanged ``job`` when the fence missed
        (another worker reclaimed it).
    """
    if terminal or job.attempt >= job.max_attempts:
        query = (
            "UPDATE jobs SET state = 'failed', error_category = %s, failure_context = %s "
            "WHERE id = %s AND worker_id = %s AND state = 'running' RETURNING *"
        )
        params: tuple[object, ...] = (
            error_category,
            Jsonb(dict(failure_context or {})),
            job.id,
            job.worker_id,
        )
    else:
        query = (
            "UPDATE jobs SET state = 'queued', worker_id = NULL, "
            "    lease_expires_at = NULL, heartbeat_at = NULL, failure_context = '{}'::jsonb "
            "WHERE id = %s AND worker_id = %s AND state = 'running' RETURNING *"
        )
        params = (job.id, job.worker_id)
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    return job if row is None else Job.model_validate(row)


async def is_queue_paused(conn: AsyncConnection) -> bool:
    """Return the worker's ``queue_paused`` flag from the single-row ``ops_control``.

    Read before each ``dequeue`` (``Worker.run_once``): while paused the worker claims
    no new job but keeps heart-beating any job already in flight. ``ops_control`` is
    seeded with one row at migration time, so the read always finds it; a missing row is
    an unexpected schema state and **fails closed** (treated as paused) rather than
    silently claiming while the control row is absent.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT queue_paused FROM ops_control WHERE singleton = true")
        row = await cur.fetchone()
    return True if row is None else bool(row[0])


async def set_queue_paused(conn: AsyncConnection, paused: bool) -> None:
    """Set the worker's ``queue_paused`` flag on the single-row ``ops_control``.

    ``ops.queue_pause``/``ops.queue_resume`` call this. Wraps the ``UPDATE`` in
    ``conn.transaction()`` so it self-commits on any connection.
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE ops_control SET queue_paused = %s WHERE singleton = true",
            (paused,),
        )


async def all_recent_jobs(
    conn: AsyncConnection, limit: int, *, states: Sequence[str] | None = None
) -> list[Job]:
    """Return the most recent jobs across **every** project, newest first, capped.

    The platform view (``ops.jobs_list``, ADR-0062): unlike :func:`recent_jobs` this is
    **not** project-scoped — it spans all tenants for an operator's cross-project queue
    inspection, so its only caller must already hold ``platform_operator``. ``states``,
    when given, filters to those job states (e.g. ``["queued", "running"]``); an empty
    sequence yields no rows. The ``id`` tiebreaker totals the order on a shared
    ``created_at`` so the cap never drops an arbitrary one of a tied pair.
    """
    where = "" if states is None else " WHERE state = ANY(%(states)s::text[])"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs" + where + " ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
            {"limit": limit, "states": list(states) if states is not None else None},
        )
        rows = await cur.fetchall()
    return [Job.model_validate(row) for row in rows]


async def queue_depth(conn: AsyncConnection) -> dict[str, int]:
    """Return the cross-project job count per state (the platform queue depth).

    Spans every project (the platform view); states with no jobs are omitted. Used by
    ``ops.jobs_list`` to report queue depth alongside the per-job rows.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT state, count(*) FROM jobs GROUP BY state")
        rows = await cur.fetchall()
    return {str(state): int(count) for state, count in rows}


async def recent_jobs(conn: AsyncConnection, limit: int, projects: Sequence[str]) -> list[Job]:
    """Return the caller's most recent jobs, newest first, capped at ``limit``.

    Scoped to ``projects``: only jobs whose ``authorizing->>'project'`` is one of the
    caller's granted projects are returned (#11). An empty ``projects`` yields no rows,
    and a job whose ``authorizing`` carries no ``project`` belongs to no one (fail
    closed). The cap applies after the project filter, so the caller gets up to ``limit``
    of *their* jobs. The ``id`` tiebreaker makes the order total when two jobs share a
    ``created_at`` microsecond, so the cap never drops an arbitrary one of a tied pair.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs WHERE authorizing->>'project' = ANY(%s::text[]) "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (list(projects), limit),
        )
        rows = await cur.fetchall()
    return [Job.model_validate(row) for row in rows]
