# Reconciler Loop (M0 subset) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M0 reconciler — a periodic core loop that repairs four drift cases between Postgres and libvirt (orphaned System, abandoned/zombie job, dead DebugSession, leaked libvirt domain) plus the lease-expiry compensation, behind a narrow `InfraReaper` provider port.

**Architecture:** One module `src/kdive/reconciler/loop.py` holds the `InfraReaper`/`OwnedDomain` Protocols, a `NullReaper` M0 default, a `ReconcileReport` dataclass, four module-level `_repair_*(conn, …)` functions, a `reconcile_once` that runs them with per-repair isolation, and a `Reconciler` loop. Each repair fences its writes (single `UPDATE … WHERE <precondition> RETURNING`, or a per-System advisory lock with a re-read) and frames every statement — *including candidate reads* — inside `conn.transaction()`, because on the non-autocommit pool a bare read leaves the connection `INTRANS` and turns later `conn.transaction()` blocks into savepoints that never commit. A `reconciler` subcommand in `__main__.py` runs it in production with the `NullReaper` until the libvirt provider (#15) injects a real one.

**Tech Stack:** Python 3.13 · `psycopg` 3.3 async (`AsyncConnection`, `dict_row`, `Jsonb`, `conn.transaction()`) · `psycopg-pool` 3.3 (`AsyncConnectionPool`) · `pydantic` 2 · Postgres 17 · `testcontainers` · `pytest` (driven with `asyncio.run`, no `pytest-asyncio`).

**Design doc:** [`docs/superpowers/specs/2026-06-03-reconciler-loop-design.md`](../specs/2026-06-03-reconciler-loop-design.md) · **ADR:** [`docs/adr/0021-reconciler-loop-drift-repair.md`](../../adr/0021-reconciler-loop-drift-repair.md)

---

## File structure

- Create `src/kdive/reconciler/loop.py` — `OwnedDomain`/`InfraReaper` Protocols, `NullReaper`, `ReconcileReport`, `_repair_orphaned_systems`, `_repair_abandoned_jobs`, `_repair_dead_sessions`, `_repair_leaked_domains`, `reconcile_once`, `Reconciler`.
- Modify `src/kdive/__main__.py` — add the `reconciler` subcommand and `_run_reconciler`.
- Create `tests/reconciler/__init__.py`, `tests/reconciler/conftest.py` (re-export the db fixtures + `FakeReaper`/`_FakeDomain` + seeding helpers), `tests/reconciler/test_loop.py`, `tests/reconciler/test_main.py`.

`src/kdive/reconciler/__init__.py` already exists (empty) and stays empty. No migration — every column and state edge the reconciler touches already exists (#6, merged).

**Test command (DB tasks 3–7):** the reconciler DB tests need Docker. Run with `KDIVE_REQUIRE_DOCKER=1` so a missing daemon fails loudly instead of skipping:

```bash
KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler -q
```

Task 1 (pure-Python skeleton) and Task 8's parser test need no Docker.

**Import hygiene (ruff `F` + `I` are enabled).** Each task adds only the imports its own code uses — an import added before it is used fails that commit's `ruff check` (F401), and same-module imports must be merged and sorted (I001). When a task says "add imports," merge new names into the existing `from <module> import …` line and run `uv run ruff check --fix tests/reconciler src/kdive/reconciler` before the final `ruff check` in each task's commit step to auto-sort/merge.

**Why the tests run repairs through a real pool.** The repairs must execute on a **non-autocommit** connection so a regression of the transaction-nesting hazard (a bare candidate read leaving the connection `INTRANS`, making per-unit `conn.transaction()` blocks savepoints that `RELEASE` but never `COMMIT`) is actually caught. The tests therefore **seed and assert via a separate autocommit connection** (`_connect`, which auto-commits each insert) but **run the repair through an `AsyncConnectionPool`** (`_run_repair`). Running a repair on an autocommit connection instead would mask that whole class of bug.

---

## Task 1: `loop.py` skeleton — port, `NullReaper`, `ReconcileReport`

Pure-Python, no DB — fastest feedback first.

**Files:**
- Create: `src/kdive/reconciler/loop.py`
- Create: `tests/reconciler/__init__.py`, `tests/reconciler/test_loop.py`

- [ ] **Step 1: Create the empty test package marker**

Create `tests/reconciler/__init__.py` with no content (an empty file).

- [ ] **Step 2: Write the failing test**

Create `tests/reconciler/test_loop.py`:

```python
"""Tests for the reconciler loop (ADR-0021, issue #12)."""

from __future__ import annotations

import asyncio

from kdive.reconciler.loop import InfraReaper, NullReaper, ReconcileReport


def test_null_reaper_is_an_infra_reaper() -> None:
    assert isinstance(NullReaper(), InfraReaper)


def test_null_reaper_lists_nothing_and_destroy_is_noop() -> None:
    async def _run() -> None:
        reaper = NullReaper()
        assert await reaper.list_owned() == []
        assert await reaper.destroy("anything") is None

    asyncio.run(_run())


def test_reconcile_report_holds_counts_and_failures() -> None:
    report = ReconcileReport(
        orphaned_systems=1,
        abandoned_jobs=2,
        dead_sessions=3,
        leaked_domains=4,
        failures=("abandoned_jobs",),
    )
    assert report.orphaned_systems == 1
    assert report.failures == ("abandoned_jobs",)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run python -m pytest tests/reconciler/test_loop.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.reconciler.loop'`.

- [ ] **Step 4: Write the `loop.py` skeleton**

Create `src/kdive/reconciler/loop.py`:

```python
"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs four repairs — orphaned
System, abandoned (zombie) job, dead DebugSession, leaked libvirt domain — each on a
fresh pooled connection, each fencing its writes, each isolated so one failing repair
does not starve the others. Time predicates use Postgres ``now()`` (never a Python
clock). The local-libvirt :class:`InfraReaper` implementation lands with the provider
(#15); M0 ships :class:`NullReaper` so the three Postgres-only repairs run today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)

# Reserved principal for system-initiated GC teardowns (ADR-0021): a reconciler
# teardown bypasses the interactive destructive-op gate by design, made auditable
# by this attribution rather than the owning user's.
SYSTEM_RECONCILER_PRINCIPAL = "system:reconciler"


@runtime_checkable
class OwnedDomain(Protocol):
    """A libvirt domain the provider owns; ``system_id`` is its metadata tag."""

    name: str
    system_id: UUID | None


@runtime_checkable
class InfraReaper(Protocol):
    """The narrow provider port the reconciler consumes (a subset of DiscoveryPlane)."""

    async def list_owned(self) -> list[OwnedDomain]: ...
    async def destroy(self, name: str) -> None: ...


class NullReaper:
    """The M0 default reaper: owns nothing, destroys nothing.

    Until the libvirt provider (#15) ships a real :class:`InfraReaper`, this lets the
    three Postgres-only repairs run in production; leaked-domain reaping activates when
    #15 injects the real reaper. It is the honest "no provider yet" default, not a stub.
    """

    async def list_owned(self) -> list[OwnedDomain]:
        return []

    async def destroy(self, name: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Per-category counts of one pass, plus the names of repairs that raised."""

    orphaned_systems: int
    abandoned_jobs: int
    dead_sessions: int
    leaked_domains: int
    failures: tuple[str, ...]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run python -m pytest tests/reconciler/test_loop.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/reconciler/loop.py tests/reconciler && uv run ruff format src/kdive/reconciler/loop.py tests/reconciler
uv run ty check src
git add src/kdive/reconciler/loop.py tests/reconciler/__init__.py tests/reconciler/test_loop.py
git commit -m "feat(reconciler): InfraReaper port, NullReaper, ReconcileReport"
```

Expected: ruff clean, `ty` clean. The skeleton imports only what it uses; later tasks add imports (`AsyncConnection`, `dict_row`, `LockScope`/`advisory_xact_lock`, `JobKind`, `queue` in Task 3; `asyncio`, `Awaitable`/`Callable`, `AsyncConnectionPool` in Task 7) the first time they are needed, keeping every commit ruff-clean under the zero-warnings policy.

---

## Task 2: Test fixtures, `FakeReaper`, and seeding helpers (`conftest.py`)

**Files:**
- Create: `tests/reconciler/conftest.py`

- [ ] **Step 1: Write the conftest**

Create `tests/reconciler/conftest.py`. It re-exports the disposable-Postgres fixtures, defines a `FakeReaper` (and `_FakeDomain`) that structurally satisfy the `InfraReaper`/`OwnedDomain` ports, and provides seeding helpers that build the minimal FK-valid graph. Seeding helpers run on an **autocommit** connection so each insert commits immediately; `_run_repair` runs a repair on a **non-autocommit pool** connection so the transaction framing is genuinely exercised.

```python
"""Fixtures and seeding helpers for the reconciler tests (issue #12).

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py``. Seeding runs on
an autocommit connection (each insert self-commits); repairs run through a real pool
(non-autocommit) so a regression of the candidate-read transaction-nesting hazard is
caught. ``FakeReaper``/``_FakeDomain`` structurally satisfy the InfraReaper/OwnedDomain
ports (no import cycle — they are duck-typed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    DEBUG_SESSIONS,
    INVESTIGATIONS,
    RESOURCES,
    RUNS,
    SYSTEMS,
)
from kdive.domain.models import (
    Allocation,
    DebugSession,
    Investigation,
    Resource,
    ResourceKind,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.reconciler.loop import OwnedDomain
from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _FakeDomain:
    """An OwnedDomain stand-in (structural match: ``name`` + ``system_id``)."""

    name: str
    system_id: UUID | None


class FakeReaper:
    """Records ``destroy`` calls and returns scripted owned domains."""

    def __init__(self, *domains: OwnedDomain) -> None:
        self._domains: tuple[OwnedDomain, ...] = domains
        self.destroyed: list[str] = []

    async def list_owned(self) -> list[OwnedDomain]:
        return list(self._domains)

    async def destroy(self, name: str) -> None:
        self.destroyed.append(name)


async def connect(url: str) -> psycopg.AsyncConnection:
    """An autocommit connection for seeding and assertions."""
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def run_repair(
    pool: AsyncConnectionPool, repair: Callable[[psycopg.AsyncConnection], Awaitable[int]]
) -> int:
    """Run one repair on a non-autocommit pool connection (exercises real framing)."""
    async with pool.connection() as conn:
        return await repair(conn)


async def seed_system(
    conn: psycopg.AsyncConnection,
    *,
    system_state: SystemState = SystemState.READY,
    alloc_state: AllocationState = AllocationState.ACTIVE,
) -> UUID:
    """Insert resource → allocation → system; return the system id."""
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(), created_at=_DT, updated_at=_DT, kind=ResourceKind.LOCAL_LIBVIRT,
            pool="p", cost_class="c", status=ResourceStatus.AVAILABLE, host_uri="qemu:///system",
        ),
    )
    allocation = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
            resource_id=resource.id, state=alloc_state,
        ),
    )
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
            allocation_id=allocation.id, state=system_state, provisioning_profile={"k": "v"},
        ),
    )
    return system.id


async def seed_run(
    conn: psycopg.AsyncConnection, system_id: UUID, *, run_state: RunState = RunState.RUNNING
) -> UUID:
    """Insert investigation → run on ``system_id``; return the run id."""
    investigation = await INVESTIGATIONS.insert(
        conn,
        Investigation(
            id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
            title="t", state=InvestigationState.OPEN,
        ),
    )
    run = await RUNS.insert(
        conn,
        Run(
            id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
            investigation_id=investigation.id, system_id=system_id, state=run_state,
            build_profile={"cfg": 1},
        ),
    )
    return run.id


async def seed_debug_session(
    conn: psycopg.AsyncConnection,
    run_id: UUID,
    *,
    state: DebugSessionState = DebugSessionState.LIVE,
    heartbeat_ago: timedelta | None = None,
) -> UUID:
    """Insert a debug session; set ``worker_heartbeat_at = now() - heartbeat_ago`` if given.

    ``heartbeat_ago=None`` leaves the heartbeat NULL. The timestamp is set in SQL with
    the DB clock so there is no test-vs-Postgres clock skew.
    """
    session = await DEBUG_SESSIONS.insert(
        conn,
        DebugSession(
            id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
            run_id=run_id, state=state, transport="gdbstub", worker_heartbeat_at=None,
        ),
    )
    if heartbeat_ago is not None:
        await conn.execute(
            "UPDATE debug_sessions SET worker_heartbeat_at = now() - %s WHERE id = %s",
            (heartbeat_ago, session.id),
        )
    return session.id


async def seed_running_job(
    conn: psycopg.AsyncConnection,
    dedup_key: str,
    *,
    kind: str = "build",
    payload: dict[str, Any] | None = None,
    lease_seconds: int,
    attempt: int,
    max_attempts: int,
) -> UUID:
    """Insert a ``running`` job with a lease ``lease_seconds`` from now (negative = lapsed).

    Raw SQL because the lease timestamp is relative (``now() + make_interval(...)``) and
    a relative interval cannot be a bound ``timestamptz`` parameter.
    """
    cur = await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
        "    lease_expires_at, authorizing, dedup_key) "
        "VALUES (%s, %s, 'running', %s, %s, 'w-dead', now() + make_interval(secs => %s), "
        "    '{}', %s) RETURNING id",
        (kind, Jsonb(payload or {}), attempt, max_attempts, lease_seconds, dedup_key),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]
```

- [ ] **Step 2: Verify the fixtures and seeding resolve with a throwaway test**

Temporarily append to `tests/reconciler/test_loop.py`:

```python
def test_seeding_resolves(migrated_url: str) -> None:
    import asyncio

    from tests.reconciler.conftest import connect, seed_system

    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            system_id = await seed_system(conn)
            cur = await conn.execute("SELECT count(*) FROM systems WHERE id = %s", (system_id,))
            assert (await cur.fetchone())[0] == 1

    asyncio.run(_run())
```

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -q`
Expected: PASS — proves the cross-package fixture import and seeding graph work.

- [ ] **Step 3: Remove the throwaway test and commit**

Delete `test_seeding_resolves` from `test_loop.py`.

```bash
uv run ruff check tests/reconciler && uv run ruff format tests/reconciler
uv run ty check src
git add tests/reconciler/conftest.py tests/reconciler/test_loop.py
git commit -m "test(reconciler): fixtures, FakeReaper, and graph-seeding helpers"
```

---

## Task 3: `_repair_orphaned_systems`

**Files:**
- Modify: `src/kdive/reconciler/loop.py`
- Modify: `tests/reconciler/test_loop.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/reconciler/test_loop.py` (add these imports at the top: `from psycopg_pool import AsyncConnectionPool`; `from kdive.reconciler import loop`; `from tests.reconciler.conftest import connect, run_repair, seed_system`; `from kdive.domain.state import AllocationState, SystemState`. `import asyncio` is already present from Task 1):

```python
def test_orphaned_system_enqueues_gc_teardown(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM systems WHERE id = %s", (system_id,)
            )
            assert (await cur.fetchone())[0] == "ready"  # System untouched
            cur = await check.execute(
                "SELECT kind, authorizing FROM jobs WHERE dedup_key = %s",
                (f"{system_id}:teardown",),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "teardown"
            assert row[1]["principal"] == "system:reconciler"  # GC attribution

    asyncio.run(_run())


def test_orphaned_system_second_pass_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.FAILED
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, loop._repair_orphaned_systems)
            second = await run_repair(pool, loop._repair_orphaned_systems)
        assert first == 1
        assert second == 0  # already queued: a re-pass enqueues nothing new
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT count(*) FROM jobs WHERE kind = 'teardown'")
            assert (await cur.fetchone())[0] == 1  # exactly one job

    asyncio.run(_run())


def test_active_allocation_system_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.ACTIVE
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT count(*) FROM jobs")
            assert (await cur.fetchone())[0] == 0

    asyncio.run(_run())


def test_terminal_system_on_released_allocation_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.TORN_DOWN, alloc_state=AllocationState.RELEASED
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 0

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k orphan -q`
Expected: FAIL with `AttributeError: module 'kdive.reconciler.loop' has no attribute '_repair_orphaned_systems'`.

- [ ] **Step 3: Implement `_repair_orphaned_systems`**

First add these imports to `src/kdive/reconciler/loop.py` (after the existing `from uuid import UUID` and before `_log`):

```python
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.models import JobKind
from kdive.jobs import queue
```

Then append the repair function to `src/kdive/reconciler/loop.py`:

```python
async def _repair_orphaned_systems(conn: AsyncConnection) -> int:
    """Enqueue an idempotent GC teardown for each System whose Allocation is gone.

    A System is orphaned when it is non-terminal but its Allocation is ``released`` or
    ``failed`` ("a System never outlives its Allocation"). The teardown job carries the
    ``system:reconciler`` attribution and bypasses the tool-layer destructive gate by
    design (ADR-0021); the teardown handler (#15) drives the System to ``torn_down``.
    Counts only a genuinely new enqueue (a re-pass on an already-queued teardown is 0).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, s.project FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "WHERE s.state NOT IN ('torn_down', 'failed') "
            "  AND a.state IN ('released', 'failed')"
        )
        candidates = await cur.fetchall()
    enqueued = 0
    for candidate in candidates:
        system_id: UUID = candidate["id"]
        dedup_key = f"{system_id}:teardown"
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
                fresh = await cur.fetchone()
                if fresh is None or fresh["state"] in ("torn_down", "failed"):
                    continue
                await cur.execute("SELECT 1 FROM jobs WHERE dedup_key = %s", (dedup_key,))
                already_queued = await cur.fetchone() is not None
            await queue.enqueue(
                conn,
                JobKind.TEARDOWN,
                {"system_id": str(system_id)},
                {"principal": SYSTEM_RECONCILER_PRINCIPAL, "agent_session": None,
                 "project": candidate["project"]},
                dedup_key,
            )
        if not already_queued:
            enqueued += 1
            _log.info("reconciler: orphaned system %s -> teardown job enqueued", system_id)
    return enqueued
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k orphan -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/reconciler/loop.py tests/reconciler/test_loop.py && uv run ruff format src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
uv run ty check src
git add src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
git commit -m "feat(reconciler): orphaned-system repair enqueues GC teardown"
```

---

## Task 4: `_repair_abandoned_jobs`

**Files:**
- Modify: `src/kdive/reconciler/loop.py`
- Modify: `tests/reconciler/test_loop.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/reconciler/test_loop.py` (add imports: `from tests.reconciler.conftest import seed_run, seed_running_job`; and merge `RunState` into the existing `from kdive.domain.state import ...` line):

```python
def test_zombie_job_dead_lettered_with_lease_expired(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            job_id = await seed_running_job(
                seed, "dk-zombie", lease_seconds=-60, attempt=3, max_attempts=3
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_abandoned_jobs)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, error_category FROM jobs WHERE id = %s", (job_id,)
            )
            state, category = await cur.fetchone()
            assert state == "failed"
            assert category == "lease_expired"

    asyncio.run(_run())


def test_zombie_job_compensates_owning_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
            await seed_running_job(
                seed, "dk-run-zombie", payload={"run_id": str(run_id)},
                lease_seconds=-60, attempt=3, max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, loop._repair_abandoned_jobs)
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, failure_category FROM runs WHERE id = %s", (run_id,)
            )
            state, category = await cur.fetchone()
            assert state == "failed"
            assert category == "lease_expired"

    asyncio.run(_run())


def test_zombie_without_run_id_leaves_runs_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
            await seed_running_job(
                seed, "dk-sys-zombie", payload={"system_id": str(system_id)},
                lease_seconds=-60, attempt=3, max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, loop._repair_abandoned_jobs)
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
            assert (await cur.fetchone())[0] == "running"  # untouched

    asyncio.run(_run())


def test_live_lease_and_attempts_remaining_not_swept(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_running_job(
                seed, "dk-live", lease_seconds=300, attempt=3, max_attempts=3
            )  # future lease
            await seed_running_job(
                seed, "dk-retryable", lease_seconds=-60, attempt=1, max_attempts=3
            )  # lapsed but attempts remain -> dequeue's job, not the reconciler's
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_abandoned_jobs)
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT count(*) FROM jobs WHERE state = 'failed'")
            assert (await cur.fetchone())[0] == 0

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k "zombie or live_lease" -q`
Expected: FAIL — `_repair_abandoned_jobs` not defined.

- [ ] **Step 3: Implement `_repair_abandoned_jobs`**

Append to `src/kdive/reconciler/loop.py`:

```python
async def _repair_abandoned_jobs(conn: AsyncConnection) -> int:
    """Dead-letter zombie jobs the worker can never reclaim, compensating their Run.

    A zombie is ``running`` with a lapsed lease and ``attempt >= max_attempts`` —
    ``dequeue``'s ``attempt < max_attempts`` predicate excludes it, so only the
    reconciler can sweep it. Each zombie is processed in its own transaction that
    dead-letters the job (fenced on ``state = 'running'``) and, when the payload carries
    a ``run_id`` whose Run is non-terminal, fails that Run — atomically, so a crash
    cannot strand the Run un-compensated.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM jobs "
            "WHERE state = 'running' AND lease_expires_at < now() "
            "  AND attempt >= max_attempts"
        )
        zombie_ids: list[UUID] = [row["id"] for row in await cur.fetchall()]
    swept = 0
    for job_id in zombie_ids:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE jobs SET state = 'failed', error_category = 'lease_expired' "
                "WHERE id = %s AND state = 'running' RETURNING payload",
                (job_id,),
            )
            row = await cur.fetchone()
            if row is None:  # fence missed: a worker finalized it first
                continue
            run_id = row["payload"].get("run_id")
            if run_id is not None:
                await cur.execute(
                    "UPDATE runs SET state = 'failed', failure_category = 'lease_expired' "
                    "WHERE id = %s AND state IN ('created', 'running')",
                    (UUID(run_id),),
                )
        swept += 1
        _log.info("reconciler: abandoned job %s -> failed (lease_expired)", job_id)
    return swept
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k "zombie or live_lease" -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/reconciler/loop.py tests/reconciler/test_loop.py && uv run ruff format src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
uv run ty check src
git add src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
git commit -m "feat(reconciler): dead-letter zombie jobs with atomic Run compensation"
```

---

## Task 5: `_repair_dead_sessions`

**Files:**
- Modify: `src/kdive/reconciler/loop.py`
- Modify: `tests/reconciler/test_loop.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/reconciler/test_loop.py` (add imports: `from datetime import timedelta`; `from tests.reconciler.conftest import seed_debug_session`; and merge `DebugSessionState` into the existing `from kdive.domain.state import ...` line. The default `DEFAULT_DEBUG_SESSION_STALE_AFTER` is 2 minutes, so "stale" uses 1 hour and "recent" uses 1 second):

```python
def test_stale_live_session_detached(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(hours=1)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, lambda conn: loop._repair_dead_sessions(conn, loop.DEFAULT_DEBUG_SESSION_STALE_AFTER)
            )
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            assert (await cur.fetchone())[0] == "detached"

    asyncio.run(_run())


def test_recent_heartbeat_session_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(seconds=1)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, lambda conn: loop._repair_dead_sessions(conn, loop.DEFAULT_DEBUG_SESSION_STALE_AFTER)
            )
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            assert (await cur.fetchone())[0] == "live"

    asyncio.run(_run())


def test_null_heartbeat_session_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=None
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, lambda conn: loop._repair_dead_sessions(conn, loop.DEFAULT_DEBUG_SESSION_STALE_AFTER)
            )
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            assert (await cur.fetchone())[0] == "live"  # NULL heartbeat never swept

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k "session" -q`
Expected: FAIL — `_repair_dead_sessions` not defined.

- [ ] **Step 3: Implement `_repair_dead_sessions`**

Append to `src/kdive/reconciler/loop.py`:

```python
async def _repair_dead_sessions(conn: AsyncConnection, stale_after: timedelta) -> int:
    """Detach ``live`` debug sessions whose heartbeat is stale (non-NULL and old).

    A NULL heartbeat is never swept — it may be a session that just attached and has
    not beaten yet. ``stale_after`` is a provisional cadence contract (ADR-0021): the
    debug plane (#16) must beat at most every ``stale_after / 3``.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE debug_sessions SET state = 'detached' "
            "WHERE state = 'live' AND worker_heartbeat_at IS NOT NULL "
            "  AND worker_heartbeat_at < now() - %s RETURNING id",
            (stale_after,),
        )
        rows = await cur.fetchall()
    for row in rows:
        _log.info("reconciler: dead debug_session %s -> detached", row["id"])
    return len(rows)
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k "session" -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/reconciler/loop.py tests/reconciler/test_loop.py && uv run ruff format src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
uv run ty check src
git add src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
git commit -m "feat(reconciler): detach dead debug sessions on stale heartbeat"
```

---

## Task 6: `_repair_leaked_domains`

**Files:**
- Modify: `src/kdive/reconciler/loop.py`
- Modify: `tests/reconciler/test_loop.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/reconciler/test_loop.py` (add import: `from tests.reconciler.conftest import FakeReaper, _FakeDomain`; `from uuid import uuid4`). A leak repair takes the reaper, so the tests pass `lambda conn: loop._repair_leaked_domains(conn, reaper)`:

```python
def test_leaked_domain_with_no_row_is_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        reaper = FakeReaper(_FakeDomain(name="vm-leak", system_id=uuid4()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 1
        assert reaper.destroyed == ["vm-leak"]

    asyncio.run(_run())


def test_domain_with_ready_row_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.READY)
        reaper = FakeReaper(_FakeDomain(name="vm-ready", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_untagged_domain_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        reaper = FakeReaper(_FakeDomain(name="vm-untagged", system_id=None))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_torn_down_row_without_teardown_job_is_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        reaper = FakeReaper(_FakeDomain(name="vm-leftover", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 1
        assert reaper.destroyed == ["vm-leftover"]

    asyncio.run(_run())


def test_torn_down_row_with_inflight_teardown_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            await seed_running_job(
                seed, f"{system_id}:teardown", kind="teardown",
                payload={"system_id": str(system_id)},
                lease_seconds=300, attempt=1, max_attempts=3,
            )
        reaper = FakeReaper(_FakeDomain(name="vm-mid-teardown", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 0
        assert reaper.destroyed == []  # a live teardown is mid-destroy (guard b)

    asyncio.run(_run())


def test_mid_provision_domain_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        # Headline acceptance: a provisioning row protects the domain (guard a),
        # independent of any provision job (which keys on allocation_id, not system_id).
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.PROVISIONING)
        reaper = FakeReaper(_FakeDomain(name="vm-provisioning", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k "leaked or reaped or untagged or torn_down or mid_provision" -q`
Expected: FAIL — `_repair_leaked_domains` not defined.

- [ ] **Step 3: Implement `_repair_leaked_domains`**

Append to `src/kdive/reconciler/loop.py`:

```python
async def _repair_leaked_domains(conn: AsyncConnection, reaper: InfraReaper) -> int:
    """Destroy libvirt domains whose tagged System is gone and no teardown is in flight.

    Reap a tagged domain iff its ``systems`` row is absent or ``torn_down`` (guard a)
    and no ``teardown`` job for it is in flight (guard b). Guard (a) protects a
    mid-provision domain (row-first ordering gives it a ``provisioning`` row). The guards
    are read under the per-System advisory lock; ``destroy`` then runs **unlocked** (a
    slow provider call never holds a Postgres lock), so the idempotent-``destroy``
    contract — not the lock — is what makes a concurrent teardown safe. A ``destroy``
    that raises is logged and the pass continues to the next domain.
    """
    domains = await reaper.list_owned()
    reaped = 0
    for domain in domains:
        if domain.system_id is None:
            continue
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, domain.system_id):
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT 1 FROM systems WHERE id = %s AND state <> 'torn_down'",
                    (domain.system_id,),
                )
                has_live_row = await cur.fetchone() is not None
                await cur.execute(
                    "SELECT 1 FROM jobs WHERE state IN ('queued', 'running') "
                    "  AND kind = 'teardown' AND payload->>'system_id' = %s",
                    (str(domain.system_id),),
                )
                teardown_in_flight = await cur.fetchone() is not None
        if has_live_row or teardown_in_flight:
            continue
        try:
            await reaper.destroy(domain.name)
        except Exception:  # noqa: BLE001 - one domain's failure must not strand the others
            _log.warning(
                "reconciler: destroy of leaked domain %s failed; retry next pass",
                domain.name, exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: leaked domain %s (system %s) reaped", domain.name, domain.system_id)
    return reaped
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k "leaked or reaped or untagged or torn_down or mid_provision" -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/reconciler/loop.py tests/reconciler/test_loop.py && uv run ruff format src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
uv run ty check src
git add src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
git commit -m "feat(reconciler): reap leaked libvirt domains via the InfraReaper port"
```

---

## Task 7: `reconcile_once` composition + `Reconciler` loop

**Files:**
- Modify: `src/kdive/reconciler/loop.py`
- Modify: `tests/reconciler/test_loop.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/reconciler/test_loop.py` (add imports: `import pytest`; `from kdive.reconciler.loop import NullReaper, ReconcileReport, Reconciler, reconcile_once`):

```python
def test_reconcile_once_counts_a_mixed_pass(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            # orphan
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
            # zombie
            await seed_running_job(seed, "dk-z", lease_seconds=-60, attempt=3, max_attempts=3)
            # dead session
            sys2 = await seed_system(seed)
            run2 = await seed_run(seed, sys2)
            await seed_debug_session(
                seed, run2, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(hours=1)
            )
        reaper = FakeReaper(_FakeDomain(name="vm-leak", system_id=uuid4()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, reaper)
        assert report == ReconcileReport(
            orphaned_systems=1, abandoned_jobs=1, dead_sessions=1, leaked_domains=1,
            failures=(),
        )
        assert reaper.destroyed == ["vm-leak"]

    asyncio.run(_run())


def test_reconcile_once_isolates_a_failing_repair(migrated_url, monkeypatch) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(hours=1)
            )

        async def _boom(conn) -> int:
            raise RuntimeError("abandoned-jobs repair blew up")

        monkeypatch.setattr(loop, "_repair_abandoned_jobs", _boom)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper())
        assert report.dead_sessions == 1  # other repairs still ran
        assert report.failures == ("abandoned_jobs",)

    asyncio.run(_run())


def test_reconciler_run_survives_a_failing_pass(monkeypatch) -> None:
    async def _run() -> None:
        stop = asyncio.Event()
        calls = 0

        async def _run_once(self) -> ReconcileReport:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient pass failure")
            stop.set()
            return ReconcileReport(0, 0, 0, 0, ())

        monkeypatch.setattr(Reconciler, "run_once", _run_once)
        reconciler = Reconciler(object(), NullReaper(), interval=timedelta(milliseconds=5))
        await asyncio.wait_for(reconciler.run(stop), timeout=2.0)
        assert calls == 2  # raised once, retried, then stopped

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -k "reconcile_once or reconciler_run" -q`
Expected: FAIL — `reconcile_once`/`Reconciler` not defined.

- [ ] **Step 3: Implement `reconcile_once` and `Reconciler`**

First add these imports to `src/kdive/reconciler/loop.py` (with the other imports):

```python
import asyncio
from collections.abc import Awaitable, Callable

from psycopg_pool import AsyncConnectionPool
```

Then append to `src/kdive/reconciler/loop.py`:

```python
async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER,
) -> ReconcileReport:
    """Run the four repairs once, each isolated, each on a fresh pooled connection.

    A repair that raises is logged, its name recorded in ``failures``, and the pass
    continues — one repair never starves the others. Returns the partial counts.
    """
    counts: dict[str, int] = {
        "orphaned_systems": 0, "abandoned_jobs": 0, "dead_sessions": 0, "leaked_domains": 0,
    }
    failures: list[str] = []

    async def _isolated(name: str, repair: Callable[[AsyncConnection], Awaitable[int]]) -> None:
        try:
            async with pool.connection() as conn:
                counts[name] = await repair(conn)
        except Exception:  # noqa: BLE001 - isolate each repair; one failure must not starve the rest
            _log.warning("reconciler: repair %s failed this pass", name, exc_info=True)
            failures.append(name)

    await _isolated("orphaned_systems", _repair_orphaned_systems)
    await _isolated("abandoned_jobs", _repair_abandoned_jobs)
    await _isolated(
        "dead_sessions", lambda conn: _repair_dead_sessions(conn, debug_session_stale_after)
    )
    await _isolated("leaked_domains", lambda conn: _repair_leaked_domains(conn, reaper))

    return ReconcileReport(
        orphaned_systems=counts["orphaned_systems"],
        abandoned_jobs=counts["abandoned_jobs"],
        dead_sessions=counts["dead_sessions"],
        leaked_domains=counts["leaked_domains"],
        failures=tuple(failures),
    )


class Reconciler:
    """Runs :func:`reconcile_once` on an interval until stopped."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        reaper: InfraReaper,
        *,
        interval: timedelta = DEFAULT_INTERVAL,
        debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER,
    ) -> None:
        self._pool = pool
        self._reaper = reaper
        self._interval = interval
        self._debug_session_stale_after = debug_session_stale_after

    async def run_once(self) -> ReconcileReport:
        """Run one reconciliation pass."""
        return await reconcile_once(
            self._pool, self._reaper, debug_session_stale_after=self._debug_session_stale_after
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once` every ``interval``, surviving a transient pass error.

        ``reconcile_once`` already isolates each repair, so a raise here is a rare
        whole-pass failure (e.g. pool acquisition); it is logged and the loop continues
        — a durable reconciler must not die on one bad pass.
        """
        interval = self._interval.total_seconds()
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a durable reconciler survives a transient per-pass error
                _log.exception("reconcile pass failed; continuing after %ss", interval)
            await asyncio.sleep(interval)
```

- [ ] **Step 4: Run the tests and the type checker**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/reconciler/test_loop.py -q`
Expected: PASS (all reconciler loop tests).

Run: `uv run ty check` (whole project — matches the pre-commit hook, which checks `tests/` too).
Expected: clean.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff check src/kdive/reconciler/loop.py tests/reconciler/test_loop.py && uv run ruff format src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
git add src/kdive/reconciler/loop.py tests/reconciler/test_loop.py
git commit -m "feat(reconciler): reconcile_once composition with per-repair isolation and Reconciler loop"
```

---

## Task 8: `reconciler` subcommand in `__main__.py`

**Files:**
- Modify: `src/kdive/__main__.py`
- Create: `tests/reconciler/test_main.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/reconciler/test_main.py`:

```python
"""CLI wiring for the `python -m kdive reconciler` subcommand (issue #12)."""

from __future__ import annotations

import asyncio

from kdive.__main__ import build_parser


def test_reconciler_subcommand_parses() -> None:
    args = build_parser().parse_args(["reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "INFO"


def test_run_reconciler_builds_and_runs(monkeypatch) -> None:
    """`_run_reconciler` opens a pool, constructs a Reconciler with NullReaper, runs, closes."""
    from kdive import __main__
    from kdive.reconciler import loop

    events: list[str] = []

    class _FakePool:
        async def open(self) -> None:
            events.append("open")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(__main__, "create_pool", lambda **kw: _FakePool())

    constructed: dict[str, object] = {}

    def _fake_init(self, pool, reaper, **kw) -> None:
        constructed["reaper"] = reaper

    async def _fake_run(self, stop) -> None:
        events.append("run")

    monkeypatch.setattr(loop.Reconciler, "__init__", _fake_init)
    monkeypatch.setattr(loop.Reconciler, "run", _fake_run)

    asyncio.run(__main__._run_reconciler())

    assert events == ["open", "run", "close"]
    assert isinstance(constructed["reaper"], loop.NullReaper)


def test_reconciler_subcommand_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "DEBUG"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/reconciler/test_main.py -q`
Expected: FAIL — `argument command: invalid choice: 'reconciler'` (parser) and `AttributeError: ... '_run_reconciler'`.

- [ ] **Step 3: Add the subcommand and `_run_reconciler` to `__main__.py`**

In `src/kdive/__main__.py`, register the subcommand in `build_parser` (after the `worker` subparser line):

```python
    sub.add_parser("reconciler", help="run the drift-repair reconciler loop")
```

Add `_run_reconciler` (mirroring `_run_worker`):

```python
async def _run_reconciler() -> None:
    from kdive.reconciler.loop import NullReaper, Reconciler

    pool = create_pool(min_size=1)
    await pool.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        reconciler = Reconciler(pool, NullReaper())
        await reconciler.run(stop)
    finally:
        await pool.close()
```

Dispatch it in `main` (after the `worker` branch):

```python
    elif args.command == "reconciler":
        asyncio.run(_run_reconciler())
```

Update the module docstring's first line to mention `reconciler`:

```python
"""Process entrypoints: `python -m kdive server|worker|reconciler` (issues #10, #12)."""
```

- [ ] **Step 4: Run the tests**

Run: `uv run python -m pytest tests/reconciler/test_main.py -q`
Expected: PASS (3 passed). The `_run_reconciler` test monkeypatches the pool and `Reconciler` so it never touches Postgres — no Docker needed.

- [ ] **Step 5: Full guardrails and commit**

```bash
uv run ruff check . && uv run ruff format --check .
uv run ty check          # whole project (src + tests), matching the pre-commit hook
KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest -m "not live_vm" -q
git add src/kdive/__main__.py tests/reconciler/test_main.py
git commit -m "feat(reconciler): add the reconciler subcommand entrypoint"
```

Expected: ruff clean, `ty` clean over src and tests, full suite green (the gdbstub/libvirt/drgn integration tests stay gated/skipped). CI runs `ty check src`, but checking tests locally catches type errors in the suite before the commit hook.

---

## Self-review notes

- **Spec coverage:** `InfraReaper`/`OwnedDomain`/`NullReaper`/`ReconcileReport` → Task 1; fixtures + `FakeReaper` + seeding → Task 2; `_repair_orphaned_systems` (GC attribution, idempotent count, active/terminal not touched) → Task 3; `_repair_abandoned_jobs` (dead-letter, atomic Run compensation, no-run_id, live/attempts-remaining skip) → Task 4; `_repair_dead_sessions` (stale detach, recent/NULL untouched) → Task 5; `_repair_leaked_domains` (reap, ready-row/untagged skip, torn_down reap, teardown-in-flight skip, mid-provision guard) → Task 6; `reconcile_once` (mixed counts, per-repair isolation/`failures`) + `Reconciler.run` (survives a failing pass) → Task 7; `reconciler` subcommand → Task 8. Spec non-goals (provider impl, reclaim-with-attempts, NULL-heartbeat sweep, time-based grace window, deeper reconciliation) have no task, intentionally.
- **Type consistency:** `_repair_*` are `async (AsyncConnection, …) -> int`; `reconcile_once(pool, reaper, *, debug_session_stale_after) -> ReconcileReport`; `Reconciler.run_once() -> ReconcileReport`, `Reconciler.run(stop) -> None`. `FakeReaper`/`_FakeDomain` structurally satisfy `InfraReaper`/`OwnedDomain` (the `*domains: OwnedDomain` varargs avoid `list` invariance at the call site). `SYSTEM_RECONCILER_PRINCIPAL` is the single source for the `"system:reconciler"` string used in Task 3 and asserted in the Task 3 test and Task 8.
- **Transaction-framing guardrail:** every candidate read and every per-unit write is inside `conn.transaction()`; the tests run repairs through a non-autocommit `AsyncConnectionPool` (`run_repair`) while seeding/asserting on an autocommit connection (`connect`), so a regression to a bare candidate read (savepoint-never-commits) fails a test rather than silently dropping repairs.
- **Counting semantics:** `orphaned_systems` counts genuinely-new enqueues (dedup_key pre-check), asserted by the Task 3 idempotency test (first pass 1, second 0); `abandoned_jobs`/`dead_sessions`/`leaked_domains` count rows actually repaired; `failures` lists repairs that raised (Task 7).
