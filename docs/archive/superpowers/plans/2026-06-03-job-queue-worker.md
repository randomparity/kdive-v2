# Job Queue & Worker Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M0 durable-job execution layer — a Postgres-backed queue with admission idempotency, lease/heartbeat, bounded retries, and a worker that dispatches claimed jobs to per-kind handlers.

**Architecture:** Three modules under `src/kdive/jobs/`. `models.py` holds the `JobHandler` callable type and a `HandlerRegistry`. `queue.py` provides five connection-scoped async functions (`enqueue`/`dequeue`/`heartbeat`/`complete`/`fail`) over the existing `jobs` table; each wraps its statements in `conn.transaction()` so it self-commits on any connection, and every post-claim write is fenced on `(worker_id, state='running')`. `worker.py`'s `Worker` owns an `AsyncConnectionPool`, claims one job per `run_once`, runs a background heartbeat on a second connection, dispatches the handler, and finalizes via `complete`/`fail` on fresh connections. No schema change — the `jobs` table, `Job` model, and `JobState` lifecycle are already merged (#5, #6).

**Tech Stack:** Python 3.13 · `psycopg` 3.3 async (`AsyncConnection`, `dict_row`, `Jsonb`, `conn.transaction()`) · `psycopg-pool` 3.3 (`AsyncConnectionPool`) · `pydantic` 2 · Postgres 17 · `testcontainers` · `pytest` (driven with `asyncio.run`, no `pytest-asyncio`).

**Design doc:** [`docs/superpowers/specs/2026-06-03-job-queue-worker-design.md`](../specs/2026-06-03-job-queue-worker-design.md) · **ADR:** [`docs/adr/0018-job-queue-worker-execution.md`](../../adr/0018-job-queue-worker-execution.md)

---

## File structure

- Create `src/kdive/jobs/models.py` — `JobHandler` type alias, `DuplicateHandler`, `HandlerRegistry`.
- Create `src/kdive/jobs/queue.py` — `DEFAULT_MAX_ATTEMPTS`, `DEFAULT_LEASE`, `enqueue`, `dequeue`, `heartbeat`, `complete`, `fail`.
- Create `src/kdive/jobs/worker.py` — `Worker` (`__init__`, `run_once`, `run`, private `_dispatch`/`_heartbeat_loop`).
- Create `tests/jobs/__init__.py`, `tests/jobs/conftest.py` (re-export the db fixtures), `tests/jobs/test_registry.py`, `tests/jobs/test_queue.py`, `tests/jobs/test_worker.py`.

`src/kdive/jobs/__init__.py` already exists (empty) and stays empty. The `jobs` table and the `JobState` `running → queued` requeue edge already exist; no migration.

**Test command (every task):** the jobs DB tests need Docker. Run with `KDIVE_REQUIRE_DOCKER=1` so a missing daemon fails loudly instead of skipping:

```bash
KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs -q
```

The registry tests (Task 1) need no Docker.

---

## Task 1: Handler registry (`models.py`)

Pure-Python, no DB — fastest feedback first.

**Files:**
- Create: `src/kdive/jobs/models.py`
- Create: `tests/jobs/__init__.py`, `tests/jobs/test_registry.py`

- [ ] **Step 1: Create the empty test package marker**

Create `tests/jobs/__init__.py` with no content (an empty file).

- [ ] **Step 2: Write the failing test**

Create `tests/jobs/test_registry.py`:

```python
"""Tests for the job-handler registry (ADR-0018)."""

from __future__ import annotations

import pytest

from kdive.domain.models import JobKind
from kdive.jobs.models import DuplicateHandler, HandlerRegistry


async def _noop(conn: object, job: object) -> str | None:
    return None


def test_get_returns_registered_handler() -> None:
    reg = HandlerRegistry()
    reg.register(JobKind.BUILD, _noop)
    assert reg.get(JobKind.BUILD) is _noop


def test_get_unregistered_returns_none() -> None:
    assert HandlerRegistry().get(JobKind.PROVISION) is None


def test_register_duplicate_raises() -> None:
    reg = HandlerRegistry()
    reg.register(JobKind.BUILD, _noop)
    with pytest.raises(DuplicateHandler):
        reg.register(JobKind.BUILD, _noop)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run python -m pytest tests/jobs/test_registry.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.jobs.models'`.

- [ ] **Step 4: Write the implementation**

Create `src/kdive/jobs/models.py`:

```python
"""Job-handler type and registry for the durable queue (ADR-0018, issue #9).

A :data:`JobHandler` is the async callable a worker invokes for one claimed
:class:`~kdive.domain.models.Job`; it runs the op and returns a ``result_ref``
(object-store key) or ``None``, or raises to fail the job. :class:`HandlerRegistry`
binds exactly one handler per :class:`~kdive.domain.models.JobKind`; the plane issues
(#11+) populate it at worker startup and the worker dispatches by ``Job.kind``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection

from kdive.domain.models import Job, JobKind

type JobHandler = Callable[[AsyncConnection, Job], Awaitable[str | None]]


class DuplicateHandler(RuntimeError):
    """A second handler was registered for a kind that already has one."""


class HandlerRegistry:
    """A one-handler-per-kind registry the worker dispatches through."""

    def __init__(self) -> None:
        self._handlers: dict[JobKind, JobHandler] = {}

    def register(self, kind: JobKind, handler: JobHandler) -> None:
        """Bind ``handler`` to ``kind``.

        Raises:
            DuplicateHandler: A handler is already registered for ``kind`` — two
                issues must not silently both claim a kind.
        """
        if kind in self._handlers:
            raise DuplicateHandler(f"a handler is already registered for {kind}")
        self._handlers[kind] = handler

    def get(self, kind: JobKind) -> JobHandler | None:
        """Return the handler for ``kind``, or ``None`` if none is registered."""
        return self._handlers.get(kind)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run python -m pytest tests/jobs/test_registry.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/jobs/models.py tests/jobs && uv run ruff format src/kdive/jobs/models.py tests/jobs
uv run ty check src
git add src/kdive/jobs/models.py tests/jobs/__init__.py tests/jobs/test_registry.py
git commit -m "feat(jobs): handler registry keyed by JobKind"
```

Expected: ruff clean, `ty` clean.

---

## Task 2: Shared test fixtures (`conftest.py`)

**Files:**
- Create: `tests/jobs/conftest.py`

- [ ] **Step 1: Re-export the disposable-Postgres fixtures**

Create `tests/jobs/conftest.py`. The db tests already define `postgres_url` (session), `pg_conn`, and `migrated_url`; importing them into this conftest makes them available to the jobs tests. Listing them in `__all__` marks them re-exported so ruff does not flag the imports:

```python
"""Shared fixtures for the jobs tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so the jobs
suite runs against the same per-test migrated schema (testcontainers Postgres).
"""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]
```

- [ ] **Step 2: Verify fixtures resolve with a throwaway test**

Temporarily append to `tests/jobs/test_registry.py`:

```python
def test_migrated_url_fixture_resolves(migrated_url: str) -> None:
    assert isinstance(migrated_url, str) and migrated_url
```

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_registry.py -q`
Expected: PASS (4 passed) — proves the cross-package fixture import works before any queue code depends on it.

- [ ] **Step 3: Remove the throwaway test and commit**

Delete `test_migrated_url_fixture_resolves` from `test_registry.py` (the real queue tests exercise the fixture from here on).

```bash
uv run ruff check tests/jobs && uv run ruff format tests/jobs
git add tests/jobs/conftest.py tests/jobs/test_registry.py
git commit -m "test(jobs): reuse disposable-Postgres fixtures for the jobs suite"
```

---

## Task 3: `queue.enqueue` — admission idempotency

**Files:**
- Create: `src/kdive/jobs/queue.py`
- Create: `tests/jobs/test_queue.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/jobs/test_queue.py`:

```python
"""Tests for the connection-scoped queue operations (ADR-0018)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _count_jobs(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("SELECT count(*) FROM jobs")
    row = await cur.fetchone()
    return row[0]


def test_enqueue_inserts_queued_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            job = await queue.enqueue(
                conn, JobKind.BUILD, {"x": 1}, {"principal": "alice"}, "dk-1"
            )
            assert isinstance(job, Job)
            assert job.state is JobState.QUEUED
            assert job.attempt == 0
            assert job.payload == {"x": 1}
            assert job.authorizing == {"principal": "alice"}
            assert job.dedup_key == "dk-1"
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_same_dedup_key_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-dup")
            second = await queue.enqueue(conn, JobKind.PROVISION, {"y": 2}, {"p": "b"}, "dk-dup")
            assert second.id == first.id
            assert second.kind is JobKind.BUILD  # the existing row, unchanged
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_distinct_dedup_keys_make_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            a = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-a")
            b = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-b")
            assert a.id != b.id
            assert await _count_jobs(conn) == 2

    asyncio.run(_run())


def test_enqueue_rejects_max_attempts_below_one(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="max_attempts"):
                await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-0", max_attempts=0)

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_queue.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.jobs.queue'`.

- [ ] **Step 3: Write `queue.py` with `enqueue`**

Create `src/kdive/jobs/queue.py`:

```python
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

from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job, JobKind

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE = timedelta(minutes=5)


async def enqueue(
    conn: AsyncConnection,
    kind: JobKind,
    payload: dict[str, Any],
    authorizing: dict[str, Any],
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
```

- [ ] **Step 4: Run to verify the enqueue tests pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_queue.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/jobs/queue.py tests/jobs/test_queue.py && uv run ruff format src/kdive/jobs/queue.py tests/jobs/test_queue.py
uv run ty check src
git add src/kdive/jobs/queue.py tests/jobs/test_queue.py
git commit -m "feat(jobs): enqueue with admission idempotency on dedup_key"
```

---

## Task 4: `queue.dequeue` — claim, charge attempt, reclaim

**Files:**
- Modify: `src/kdive/jobs/queue.py`
- Modify: `tests/jobs/test_queue.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/jobs/test_queue.py`:

Add `from psycopg.rows import dict_row` to the imports at the top of
`tests/jobs/test_queue.py` (matching the codebase convention), then append:

```python
async def _insert_running_job(
    conn: psycopg.AsyncConnection,
    dedup_key: str,
    *,
    worker_id: str = "dead",
    lease_seconds: int,
    attempt: int = 0,
    max_attempts: int = 3,
) -> Job:
    """Insert a job already in ``running`` with a lease ``lease_seconds`` from now.

    Negative ``lease_seconds`` makes the lease already lapsed. The timestamp is
    computed in SQL (``now() + make_interval(...)``) — a relative interval cannot be
    passed as a bound parameter to a ``timestamptz`` column.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
            "    lease_expires_at, authorizing, dedup_key) "
            "VALUES ('build', '{}', 'running', %s, %s, %s, now() + make_interval(secs => %s), "
            "    '{}', %s) RETURNING *",
            (attempt, max_attempts, worker_id, lease_seconds, dedup_key),
        )
        row = await cur.fetchone()
    return Job.model_validate(row)


def test_dequeue_claims_oldest_and_charges_attempt(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-old")
            await asyncio.sleep(0.01)
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-new")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            assert claimed.dedup_key == "dk-old"  # FIFO by created_at
            assert claimed.state is JobState.RUNNING
            assert claimed.worker_id == "w1"
            assert claimed.attempt == 1
            assert claimed.lease_expires_at is not None

    asyncio.run(_run())


def test_dequeue_empty_returns_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await queue.dequeue(conn, "w1") is None

    asyncio.run(_run())


def test_dequeue_concurrent_claims_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as setup:
            await queue.enqueue(setup, JobKind.BUILD, {}, {"p": "a"}, "dk-1")
            await queue.enqueue(setup, JobKind.BUILD, {}, {"p": "a"}, "dk-2")
        async with await _connect(migrated_url) as a, await _connect(migrated_url) as b:
            ja, jb = await asyncio.gather(queue.dequeue(a, "wa"), queue.dequeue(b, "wb"))
        assert ja is not None and jb is not None
        assert ja.id != jb.id  # SKIP LOCKED: no double-claim

    asyncio.run(_run())


def test_dequeue_skips_future_lease_reclaims_past_lease(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _insert_running_job(conn, "dk-future", lease_seconds=300)
            assert await queue.dequeue(conn, "w1") is None  # live lease: not reclaimed

            await _insert_running_job(conn, "dk-past", lease_seconds=-60)
            reclaimed = await queue.dequeue(conn, "w1")
            assert reclaimed is not None
            assert reclaimed.dedup_key == "dk-past"
            assert reclaimed.worker_id == "w1"
            assert reclaimed.attempt == 1  # 0 -> 1 on reclaim

    asyncio.run(_run())


def test_dequeue_skips_exhausted_attempts(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _insert_running_job(conn, "dk-done", lease_seconds=-60, attempt=3, max_attempts=3)
            assert await queue.dequeue(conn, "w1") is None  # attempt == max: left for reconciler

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_queue.py -k dequeue -q`
Expected: FAIL with `AttributeError: module 'kdive.jobs.queue' has no attribute 'dequeue'`.

- [ ] **Step 3: Add `dequeue` to `queue.py`**

Append to `src/kdive/jobs/queue.py`:

```python
async def dequeue(conn: AsyncConnection, worker_id: str, *, lease: timedelta = DEFAULT_LEASE) -> Job | None:
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
```

- [ ] **Step 4: Run to verify the dequeue tests pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_queue.py -q`
Expected: PASS (all enqueue + dequeue tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/jobs/queue.py tests/jobs/test_queue.py && uv run ruff format src/kdive/jobs/queue.py tests/jobs/test_queue.py
uv run ty check src
git add src/kdive/jobs/queue.py tests/jobs/test_queue.py
git commit -m "feat(jobs): dequeue with SKIP LOCKED claim, attempt charge, lease reclaim"
```

---

## Task 5: `queue.heartbeat`, `queue.complete`, `queue.fail` — fenced finalizers

**Files:**
- Modify: `src/kdive/jobs/queue.py`
- Modify: `tests/jobs/test_queue.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/jobs/test_queue.py`:

```python
from kdive.domain.errors import ErrorCategory


def test_heartbeat_renews_for_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-hb")
            claimed = await queue.dequeue(conn, "w1", lease=timedelta(seconds=10))
            assert claimed is not None
            assert await queue.heartbeat(conn, claimed.id, "w1", lease=timedelta(minutes=5)) is True
            cur = await conn.execute("SELECT lease_expires_at FROM jobs WHERE id = %s", (claimed.id,))
            renewed = (await cur.fetchone())[0]
            assert renewed > claimed.lease_expires_at

    asyncio.run(_run())


def test_heartbeat_false_for_non_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-hb2")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            assert await queue.heartbeat(conn, claimed.id, "intruder") is False

    asyncio.run(_run())


def test_complete_for_owner_and_none_for_non_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-c1")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            done = await queue.complete(conn, claimed.id, "w1", "s3://result")
            assert done is not None
            assert done.state is JobState.SUCCEEDED
            assert done.result_ref == "s3://result"

            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-c2")
            other = await queue.dequeue(conn, "w1")
            assert other is not None
            assert await queue.complete(conn, other.id, "intruder", "s3://x") is None

    asyncio.run(_run())


def test_fail_requeues_below_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-f1", max_attempts=3)
            claimed = await queue.dequeue(conn, "w1")  # attempt -> 1
            assert claimed is not None
            out = await queue.fail(conn, claimed, ErrorCategory.INFRASTRUCTURE_FAILURE)
            assert out.state is JobState.QUEUED
            assert out.worker_id is None
            assert out.lease_expires_at is None

    asyncio.run(_run())


def test_fail_dead_letters_at_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            claimed = await _insert_running_job(
                conn, "dk-f2", worker_id="w1", lease_seconds=300, attempt=3, max_attempts=3
            )
            out = await queue.fail(conn, claimed, ErrorCategory.BUILD_FAILURE)
            assert out.state is JobState.FAILED
            assert out.error_category is ErrorCategory.BUILD_FAILURE

    asyncio.run(_run())


def test_fail_terminal_dead_letters_below_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-f3", max_attempts=3)
            claimed = await queue.dequeue(conn, "w1")  # attempt -> 1, below max
            assert claimed is not None
            out = await queue.fail(conn, claimed, ErrorCategory.NOT_IMPLEMENTED, terminal=True)
            assert out.state is JobState.FAILED
            assert out.error_category is ErrorCategory.NOT_IMPLEMENTED

    asyncio.run(_run())


def test_fail_fence_miss_returns_input(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-f4")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            # Simulate a reclaim by another worker: change worker_id out from under it.
            await conn.execute("UPDATE jobs SET worker_id = 'w2' WHERE id = %s", (claimed.id,))
            out = await queue.fail(conn, claimed, ErrorCategory.INFRASTRUCTURE_FAILURE)
            assert out is claimed  # fence missed: unchanged input returned

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_queue.py -k "heartbeat or complete or fail" -q`
Expected: FAIL — `heartbeat`/`complete`/`fail` not defined.

- [ ] **Step 3: Add the three finalizers to `queue.py`**

Append to `src/kdive/jobs/queue.py`:

```python
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
    conn: AsyncConnection, job: Job, error_category: ErrorCategory, *, terminal: bool = False
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
            "UPDATE jobs SET state = 'failed', error_category = %s "
            "WHERE id = %s AND worker_id = %s AND state = 'running' RETURNING *"
        )
        params: tuple[object, ...] = (error_category, job.id, job.worker_id)
    else:
        query = (
            "UPDATE jobs SET state = 'queued', worker_id = NULL, "
            "    lease_expires_at = NULL, heartbeat_at = NULL "
            "WHERE id = %s AND worker_id = %s AND state = 'running' RETURNING *"
        )
        params = (job.id, job.worker_id)
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    return job if row is None else Job.model_validate(row)
```

- [ ] **Step 4: Run to verify all queue tests pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_queue.py -q`
Expected: PASS (all enqueue/dequeue/heartbeat/complete/fail tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/jobs/queue.py tests/jobs/test_queue.py && uv run ruff format src/kdive/jobs/queue.py tests/jobs/test_queue.py
uv run ty check src
git add src/kdive/jobs/queue.py tests/jobs/test_queue.py
git commit -m "feat(jobs): fenced heartbeat, complete, and requeue-or-dead-letter fail"
```

---

## Task 6: `Worker` — construct guard + `run_once` dispatch loop

**Files:**
- Create: `src/kdive/jobs/worker.py`
- Create: `tests/jobs/test_worker.py`

- [ ] **Step 1: Write the failing tests (construct guard + happy path + unknown kind)**

Create `tests/jobs/test_worker.py`:

```python
"""Tests for the worker claim/dispatch loop (ADR-0018)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker


async def _final_state(url: str, job_id: UUID) -> Job:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        job = await JOBS.get(conn, job_id)
    assert job is not None
    return job


def test_init_rejects_interval_above_third_of_lease() -> None:
    reg = HandlerRegistry()
    with pytest.raises(ValueError, match="heartbeat_interval"):
        Worker(
            object(), reg, worker_id="w1",
            lease=timedelta(seconds=3), heartbeat_interval=timedelta(seconds=2),
        )


def test_init_accepts_interval_at_third_of_lease() -> None:
    Worker(
        object(), HandlerRegistry(), worker_id="w1",
        lease=timedelta(seconds=3), heartbeat_interval=timedelta(seconds=1),
    )


def test_run_once_happy_path(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls: list[Job] = []

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                calls.append(job)
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-happy")

            processed = await worker.run_once()
            assert processed is not None and processed.id == job.id
            assert len(calls) == 1
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED
            assert final.result_ref == "s3://out"

            assert await worker.run_once() is None  # queue now empty

    asyncio.run(_run())


def test_run_once_unknown_kind_dead_letters(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            worker = Worker(pool, HandlerRegistry(), worker_id="w1")  # no handlers
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-unk")
            await worker.run_once()
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.FAILED
            assert final.error_category is ErrorCategory.NOT_IMPLEMENTED
            assert final.attempt == 1  # claimed once, dead-lettered at once (terminal)

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_worker.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.jobs.worker'`.

- [ ] **Step 3: Write `worker.py`**

Create `src/kdive/jobs/worker.py`:

```python
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
        """Loop :meth:`run_once`, sleeping ``poll_interval`` when the queue is empty."""
        poll = self._poll_interval.total_seconds()
        while not stop.is_set():
            job = await self.run_once()
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
        interval = self._heartbeat_interval.total_seconds()
        async with self._pool.connection() as conn:
            while True:
                await asyncio.sleep(interval)
                if not await queue.heartbeat(conn, job_id, self._worker_id, lease=self._lease):
                    return
```

The `JobHandler` alias (Task 1) types `handler` precisely, so `await handler(conn,
job)` type-checks with no ignore. The broad `except Exception` is the worker's job —
it turns any handler failure into a dead-letter/requeue — so it is annotated
`# noqa: BLE001` with that justification; `BaseException` (e.g. `CancelledError` on
shutdown) is deliberately not caught and propagates.

- [ ] **Step 4: Run the tests and the type checker**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_worker.py -q`
Expected: PASS (4 passed: two `__init__` guard tests, happy path, unknown kind).

Run: `uv run ty check` (whole project — matches the pre-commit hook, which checks
`tests/` too).
Expected: clean.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff check src/kdive/jobs/worker.py tests/jobs/test_worker.py && uv run ruff format src/kdive/jobs/worker.py tests/jobs/test_worker.py
git add src/kdive/jobs/worker.py tests/jobs/test_worker.py
git commit -m "feat(jobs): worker run_once dispatch with heartbeat and finalize"
```

---

## Task 7: Worker acceptance — dedup, dead-letter, lapsed-lease reclaim, live heartbeat

**Files:**
- Modify: `tests/jobs/test_worker.py`

- [ ] **Step 1: Write the acceptance tests**

Append to `tests/jobs/test_worker.py`:

```python
def test_run_once_dedup_runs_handler_once(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls = 0

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                nonlocal calls
                calls += 1
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                first = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-dedup")
                second = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-dedup")
            assert second.id == first.id

            await worker.run_once()
            assert await worker.run_once() is None  # only one job ever existed
            assert calls == 1

    asyncio.run(_run())


def test_run_once_dead_letters_after_max_attempts(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls = 0

            async def always_raises(conn: psycopg.AsyncConnection, job: Job) -> str:
                nonlocal calls
                calls += 1
                raise CategorizedError("boom", category=ErrorCategory.BUILD_FAILURE)

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, always_raises)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-poison", max_attempts=3)

            for _ in range(3):
                await worker.run_once()
            assert calls == 3
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.FAILED
            assert final.error_category is ErrorCategory.BUILD_FAILURE
            assert await worker.run_once() is None  # dead-lettered: not re-dequeued

    asyncio.run(_run())


def test_run_once_reclaims_lapsed_lease(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls = 0

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                nonlocal calls
                calls += 1
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-lapse")
                # Simulate a dead worker holding a now-lapsed lease.
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'dead', "
                    "lease_expires_at = now() - interval '1 min' WHERE id = %s",
                    (job.id,),
                )
            processed = await worker.run_once()  # reclaims and runs it
            assert processed is not None and processed.id == job.id
            assert calls == 1
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED
            assert final.attempt == 1  # 0 -> 1 on reclaim (the dead worker never charged it)

    asyncio.run(_run())


def test_heartbeat_renews_live_lease(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=10) as pool:
            started = asyncio.Event()

            async def slow(conn: psycopg.AsyncConnection, job: Job) -> str:
                started.set()
                await asyncio.sleep(2.0)  # outlives the 1 s lease
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, slow)
            worker = Worker(
                pool, reg, worker_id="w1",
                lease=timedelta(seconds=1), heartbeat_interval=timedelta(milliseconds=250),
            )
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-hb-live")

            task = asyncio.create_task(worker.run_once())
            await started.wait()

            await asyncio.sleep(0.5)
            async with pool.connection() as c:
                cur = await c.execute("SELECT lease_expires_at FROM jobs WHERE id = %s", (job.id,))
                t1 = (await cur.fetchone())[0]
            await asyncio.sleep(0.6)
            async with pool.connection() as c:
                cur = await c.execute(
                    "SELECT lease_expires_at, worker_id FROM jobs WHERE id = %s", (job.id,)
                )
                t2, owner = await cur.fetchone()

            assert t2 > t1            # the heartbeat advanced the lease mid-run
            assert owner == "w1"      # never reclaimed
            await task
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure / progress**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs/test_worker.py -q`
Expected: the four new tests pass with the Task 6 worker (no new production code needed — these are acceptance tests over existing behavior). If `test_heartbeat_renews_live_lease` fails because the heartbeat never advanced the lease, the heartbeat task is broken — fix `worker._heartbeat_loop` until it passes (this is the one test that catches a missing/incorrect heartbeat).

- [ ] **Step 3: Run the whole jobs suite**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/jobs -q`
Expected: PASS (registry + queue + worker).

- [ ] **Step 4: Full guardrails and commit**

```bash
uv run ruff check . && uv run ruff format --check .
uv run ty check          # whole project (src + tests), matching the pre-commit hook
KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest -m "not live_vm" -q
git add tests/jobs/test_worker.py
git commit -m "test(jobs): worker acceptance — dedup, dead-letter, reclaim, live heartbeat"
```

Expected: ruff clean, `ty` clean over src and tests, full suite green (the gdbstub/libvirt/drgn integration tests stay gated/skipped). CI itself runs `ty check src`, but checking tests locally catches type errors in the suite before they reach the commit hook.

---

## Self-review notes

- **Spec coverage:** `models.py`/registry → Task 1; fixtures → Task 2; `enqueue` (idempotency, `max_attempts` guard) → Task 3; `dequeue` (claim, attempt charge, reclaim, exhausted-skip, SKIP LOCKED) → Task 4; `heartbeat`/`complete`/`fail` (fencing, terminal) → Task 5; `Worker` (`__init__` guard, `run_once`, `run`, dispatch, heartbeat task, fresh-connection finalize) → Task 6; the five acceptance scenarios + the live-heartbeat test → Task 7. The reconciler sweep, `jobs.*` tools, provider handlers, per-pool scheduling, and retry backoff are spec non-goals — no task, intentionally.
- **Type consistency:** `JobHandler = Callable[[AsyncConnection, Job], Awaitable[str | None]]` is used identically in `models.py` and `worker._dispatch`; `queue` functions return `Job`/`Job | None`/`bool` as the tests assert; `ErrorCategory`/`JobState`/`JobKind` are imported from their existing modules (`kdive.domain.errors`/`kdive.domain.state`/`kdive.domain.models`).
- **Guardrail note:** every queue function wraps its statements in `conn.transaction()` so it self-commits on the autocommit test connections *and* on the worker's non-autocommit pool connections.
