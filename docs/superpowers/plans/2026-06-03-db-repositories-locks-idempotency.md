# Repository Layer, Advisory Locks & Idempotency Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M0 data-access layer — typed async CRUD over the durable objects, per-Allocation/per-System advisory locks, and an idempotent step ledger — over the existing schema (#6) and domain models (#5).

**Architecture:** Three modules under `src/kdive/db/`. `repositories.py` provides a generic `Repository[M]` (insert/get) and `StatefulRepository[M, S]` (adds `update_state`, guarded by `kdive.domain.state.can_transition`), instantiated once per table; the DB owns `id`-generation fallback and the `created_at`/`updated_at` timestamps. `locks.py` wraps `pg_advisory_xact_lock` (single-bigint space, disjoint from the migration runner's two-int lock) in an async context manager that fails fast if no transaction is open. `idempotency.py`'s `run_step` records successful step results in `run_steps` keyed `(run_id, step)` and replays the stored value. Tests use the existing disposable-Postgres fixtures and `asyncio.run(...)` (no `pytest-asyncio`).

**Tech Stack:** Python 3.13 · `psycopg` 3 async (`AsyncConnection`, `dict_row`, `Jsonb`, `TransactionStatus`) · `pydantic` 2 · Postgres 17 · `testcontainers` · `pytest`.

**Design doc:** [`docs/superpowers/specs/2026-06-03-db-repositories-locks-idempotency-design.md`](../specs/2026-06-03-db-repositories-locks-idempotency-design.md) · **ADR:** [`docs/adr/0016-repository-layer-locks-idempotency.md`](../../adr/0016-repository-layer-locks-idempotency.md)

---

## File structure

- Create `src/kdive/db/locks.py` — `LockScope` enum, `_lock_key`, `advisory_xact_lock`.
- Create `src/kdive/db/repositories.py` — `ObjectNotFound`, `Repository[M]`, `StatefulRepository[M, S]`, eight module-level instances.
- Create `src/kdive/db/idempotency.py` — `JsonValue`, `run_step`.
- Modify `tests/db/conftest.py` — add the `migrated_url` fixture.
- Create `tests/db/test_locks.py`, `tests/db/test_repositories.py`, `tests/db/test_idempotency.py`.

`run_step`'s ledger is the existing `run_steps` table; no schema change. All three modules are async and consume an injected `AsyncConnection` — they never open their own pool.

**Note on the test command:** every DB test needs Docker. Run the suite with `KDIVE_REQUIRE_DOCKER=1` so a missing daemon fails loudly instead of skipping (matches the existing `conftest.py` / CI contract).

---

## Task 1: Test harness — `migrated_url` fixture

**Files:**
- Modify: `tests/db/conftest.py`

- [ ] **Step 1: Add the fixture**

Add to `tests/db/conftest.py` — a new import and a function-scoped fixture that resets the schema (via the existing `pg_conn`), applies migrations, and yields the conninfo for async connections:

```python
from kdive.db import migrate
```

```python
@pytest.fixture
def migrated_url(pg_conn: psycopg.Connection, postgres_url: str) -> str:
    """A migrated, freshly-emptied database; yields the conninfo for async tests.

    Depends on `pg_conn` (which drops and recreates `public`) so each test starts
    from a clean schema, then applies the migrations on that same autocommit
    connection before handing back the URL.
    """
    migrate.apply_migrations(pg_conn)
    return postgres_url
```

- [ ] **Step 2: Exercise the fixture before anything depends on it**

Append to `tests/db/test_harness.py` a test that uses `migrated_url` directly, so a
fixture bug surfaces here in isolation rather than two tasks later inside new
repository code:

```python
import asyncio


def test_migrated_url_has_schema(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            cur = await conn.execute("SELECT to_regclass('public.runs')")
            row = await cur.fetchone()
            assert row is not None and row[0] is not None

    asyncio.run(_run())
```

- [ ] **Step 3: Verify it passes (and nothing regressed)**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_harness.py tests/db/test_migrate.py -q`
Expected: PASS, including the new `test_migrated_url_has_schema`. A failure here means
the `pg_conn`→migrate→`postgres_url` wiring is wrong — fix it before proceeding.

- [ ] **Step 4: Guardrails**

Run: `uv run ruff check tests/db/conftest.py tests/db/test_harness.py && uv run ruff format --check tests/db/conftest.py tests/db/test_harness.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/db/conftest.py tests/db/test_harness.py
git commit -m "test(db): add migrated_url fixture for async repository tests"
```

---

## Task 2: Advisory locks (`locks.py`)

**Files:**
- Create: `tests/db/test_locks.py`
- Create: `src/kdive/db/locks.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_locks.py`:

```python
"""Tests for transaction-scoped advisory locks (ADR-0005, ADR-0016)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, _lock_key, advisory_xact_lock

_KEY = UUID("11111111-1111-1111-1111-111111111111")


def test_lock_key_deterministic_and_scope_sensitive() -> None:
    key = uuid4()
    assert _lock_key(LockScope.ALLOCATION, key) == _lock_key(LockScope.ALLOCATION, key)
    assert _lock_key(LockScope.ALLOCATION, key) != _lock_key(LockScope.SYSTEM, key)
    assert _lock_key(LockScope.ALLOCATION, key) != _lock_key(LockScope.ALLOCATION, uuid4())
    value = _lock_key(LockScope.ALLOCATION, key)
    assert -(2**63) <= value < 2**63


async def _wait_until_lock_waiting(observer: psycopg.AsyncConnection, waiter_pid: int) -> None:
    """Poll pg_locks until `waiter_pid` is blocked on an advisory lock (not merely slow)."""
    for _ in range(200):
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = %s AND NOT granted",
            (waiter_pid,),
        )
        if await cur.fetchone() is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the second connection never began waiting on the advisory lock")


def test_lock_blocks_until_holder_commits(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
        ):
            acquired_b = asyncio.Event()

            async def acquire_b() -> str:
                async with b.transaction(), advisory_xact_lock(b, LockScope.ALLOCATION, _KEY):
                    acquired_b.set()
                    return "acquired"

            async with a.transaction():
                async with advisory_xact_lock(a, LockScope.ALLOCATION, _KEY):
                    task = asyncio.create_task(acquire_b())
                    await _wait_until_lock_waiting(a, b.info.backend_pid)
                    assert not task.done()
                    assert not acquired_b.is_set()
                # advisory_xact_lock exit is a no-op; a's commit (below) releases.
            assert await asyncio.wait_for(task, timeout=5) == "acquired"

    asyncio.run(_run())


def test_different_key_does_not_block(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
        ):
            async with a.transaction(), advisory_xact_lock(a, LockScope.ALLOCATION, _KEY):

                async def acquire_b() -> str:
                    async with b.transaction(), advisory_xact_lock(
                        b, LockScope.ALLOCATION, uuid4()
                    ):
                        return "acquired"

                assert await asyncio.wait_for(acquire_b(), timeout=5) == "acquired"

    asyncio.run(_run())


def test_different_scope_does_not_block(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
        ):
            async with a.transaction(), advisory_xact_lock(a, LockScope.ALLOCATION, _KEY):

                async def acquire_b() -> str:
                    async with b.transaction(), advisory_xact_lock(b, LockScope.SYSTEM, _KEY):
                        return "acquired"

                assert await asyncio.wait_for(acquire_b(), timeout=5) == "acquired"

    asyncio.run(_run())


def test_no_open_transaction_raises(postgres_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as conn:
            with pytest.raises(RuntimeError, match="open transaction"):
                async with advisory_xact_lock(conn, LockScope.ALLOCATION, uuid4()):
                    pass

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_locks.py -q`
Expected: collection/import error — `kdive.db.locks` does not exist yet.

- [ ] **Step 3: Write `locks.py`**

Create `src/kdive/db/locks.py`:

```python
"""Transaction-scoped Postgres advisory locks (ADR-0005, ADR-0016).

`advisory_xact_lock` serializes per-Allocation / per-System operations using the
single-bigint `pg_advisory_xact_lock` — a lock space disjoint from the migration
runner's two-int lock (ADR-0015), so application and migration locks never contend.
The lock releases when the surrounding transaction ends; the helper fails fast when
no transaction is open to hold it.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus


class LockScope(StrEnum):
    """The advisory-lock scopes M0 serializes on (ADR-0016)."""

    ALLOCATION = "allocation"
    SYSTEM = "system"


def _lock_key(scope: LockScope, key: UUID) -> int:
    """Derive a deterministic signed 64-bit advisory-lock key from ``(scope, key)``.

    The digest folds an unbounded key space onto 64 bits: a collision over-serializes
    two unrelated keys (safe — never under-serializes). A ``0x00`` separator keeps the
    scope and key boundaries unambiguous for the NUL-free identifiers used here.
    """
    digest = hashlib.blake2b(digest_size=8)
    digest.update(scope.value.encode())
    digest.update(b"\x00")
    digest.update(str(key).encode())
    return int.from_bytes(digest.digest(), "big", signed=True)


@asynccontextmanager
async def advisory_xact_lock(
    conn: AsyncConnection, scope: LockScope, key: UUID
) -> AsyncIterator[None]:
    """Hold a transaction-scoped advisory lock for ``(scope, key)`` over the block.

    Blocks until any current holder's transaction ends, then yields. The lock is
    released by the caller's transaction commit/rollback, not on block exit.

    Args:
        conn: An async connection with an open (or about-to-open) transaction.
        scope: The lock scope.
        key: The object id the lock protects.

    Raises:
        RuntimeError: After acquiring, the connection is not in a transaction, so the
            lock already auto-released (e.g. an autocommit connection used without
            ``conn.transaction()``).
    """
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope, key),))
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError(
            "advisory_xact_lock must run inside an open transaction; the lock "
            "auto-released because no transaction is in progress (ADR-0005). Wrap the "
            "call in `async with conn.transaction()` or use a non-autocommit connection."
        )
    yield
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_locks.py -q`
Expected: all PASS. If `test_lock_blocks_until_holder_commits` hangs, the `transaction_status` guard or the `pg_locks` poll is wrong — fix before proceeding.

- [ ] **Step 5: Guardrails**

Run: `uv run ruff check src/kdive/db/locks.py tests/db/test_locks.py && uv run ruff format --check src/kdive/db/locks.py tests/db/test_locks.py && uv run ty check src`
Expected: clean. If `ty` flags `AsyncConnection` as needing a type argument, write `AsyncConnection[Any]` (import `Any` from `typing`) with no behavior change.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/locks.py tests/db/test_locks.py
git commit -m "feat(db): add transaction-scoped advisory lock helper"
```

---

## Task 3: Repository layer (`repositories.py`)

**Files:**
- Create: `tests/db/test_repositories.py`
- Create: `src/kdive/db/repositories.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_repositories.py`:

```python
"""Tests for the typed async repositories (ADR-0003, ADR-0016)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import (
    ALLOCATIONS,
    ARTIFACTS,
    DEBUG_SESSIONS,
    INVESTIGATIONS,
    JOBS,
    RESOURCES,
    RUNS,
    SYSTEMS,
    ObjectNotFound,
)
from kdive.domain.models import (
    Allocation,
    Artifact,
    DebugSession,
    ExternalRef,
    Investigation,
    Job,
    JobKind,
    Resource,
    ResourceKind,
    Run,
    Sensitivity,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    IllegalTransition,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
)

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _resource(**kw: object) -> Resource:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, kind=ResourceKind.LOCAL_LIBVIRT,
        pool="p", cost_class="c", status=ResourceStatus.AVAILABLE, host_uri="qemu:///system",
    )
    base.update(kw)
    return Resource.model_validate(base)


def _allocation(resource_id: UUID, **kw: object) -> Allocation:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
        resource_id=resource_id, state=AllocationState.REQUESTED,
    )
    base.update(kw)
    return Allocation.model_validate(base)


def _system(allocation_id: UUID, **kw: object) -> System:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
        allocation_id=allocation_id, state=SystemState.DEFINED, provisioning_profile={"k": "v"},
    )
    base.update(kw)
    return System.model_validate(base)


def _investigation(**kw: object) -> Investigation:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
        title="t", state=InvestigationState.OPEN,
    )
    base.update(kw)
    return Investigation.model_validate(base)


def _run(investigation_id: UUID, system_id: UUID, **kw: object) -> Run:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
        investigation_id=investigation_id, system_id=system_id, state=RunState.CREATED,
        build_profile={"cfg": 1},
    )
    base.update(kw)
    return Run.model_validate(base)


def _debug_session(run_id: UUID, **kw: object) -> DebugSession:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
        run_id=run_id, state=DebugSessionState.ATTACH, transport="gdb",
    )
    base.update(kw)
    return DebugSession.model_validate(base)


def _job(**kw: object) -> Job:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, kind=JobKind.BUILD, state=JobState.QUEUED,
        max_attempts=3, authorizing={"principal": "alice"}, dedup_key=str(uuid4()),
    )
    base.update(kw)
    return Job.model_validate(base)


def _artifact(owner_id: UUID, **kw: object) -> Artifact:
    base: dict[str, object] = dict(
        id=uuid4(), created_at=_DT, updated_at=_DT, owner_kind="system", owner_id=owner_id,
        object_key="k", etag="e", sensitivity=Sensitivity.REDACTED, retention_class="default",
    )
    base.update(kw)
    return Artifact.model_validate(base)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_roundtrip_every_object(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource(capabilities={"kvm": True}))
            assert await RESOURCES.get(conn, res.id) == res

            alloc = await ALLOCATIONS.insert(
                conn, _allocation(res.id, capability_scope={"cpus": 4})
            )
            assert await ALLOCATIONS.get(conn, alloc.id) == alloc

            sysm = await SYSTEMS.insert(conn, _system(alloc.id))
            assert await SYSTEMS.get(conn, sysm.id) == sysm

            inv = await INVESTIGATIONS.insert(
                conn,
                _investigation(
                    external_refs=[ExternalRef(tracker="bz", id="1", url="http://x")]
                ),
            )
            assert await INVESTIGATIONS.get(conn, inv.id) == inv

            run = await RUNS.insert(conn, _run(inv.id, sysm.id))
            assert await RUNS.get(conn, run.id) == run

            ds = await DEBUG_SESSIONS.insert(conn, _debug_session(run.id))
            assert await DEBUG_SESSIONS.get(conn, ds.id) == ds

            job = await JOBS.insert(conn, _job(payload={"x": 1}))
            assert await JOBS.get(conn, job.id) == job

            art = await ARTIFACTS.insert(conn, _artifact(sysm.id))
            assert await ARTIFACTS.get(conn, art.id) == art

    asyncio.run(_run_test())


def test_get_miss_returns_none(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            assert await RESOURCES.get(conn, uuid4()) is None

    asyncio.run(_run_test())


def test_insert_timestamps_are_db_authoritative(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            wrong = datetime(2000, 1, 1, tzinfo=UTC)
            res = _resource(created_at=wrong, updated_at=wrong)
            inserted = await RESOURCES.insert(conn, res)
            assert inserted.created_at != wrong
            assert inserted.created_at.year >= 2026

    asyncio.run(_run_test())


def test_update_state_legal_bumps_updated_at(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource())
            alloc = await ALLOCATIONS.insert(conn, _allocation(res.id))
            updated = await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)
            assert updated.state is AllocationState.GRANTED
            assert updated.updated_at > alloc.updated_at  # trigger bumped it

    asyncio.run(_run_test())


def test_update_state_illegal_raises(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource())
            alloc = await ALLOCATIONS.insert(conn, _allocation(res.id))
            with pytest.raises(IllegalTransition):
                await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.RELEASED)

    asyncio.run(_run_test())


def test_update_state_unknown_id_raises(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ObjectNotFound):
                await ALLOCATIONS.update_state(conn, uuid4(), AllocationState.GRANTED)

    asyncio.run(_run_test())


def test_update_state_concurrent_same_target(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as setup:
            res = await RESOURCES.insert(setup, _resource())
            alloc = await ALLOCATIONS.insert(setup, _allocation(res.id))
        async with (
            await _connect(migrated_url) as a,
            await _connect(migrated_url) as b,
        ):

            async def go(conn: psycopg.AsyncConnection) -> object:
                return await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)

            results = await asyncio.gather(go(a), go(b), return_exceptions=True)
        successes = [r for r in results if isinstance(r, Allocation)]
        failures = [r for r in results if isinstance(r, IllegalTransition)]
        assert len(successes) == 1
        assert len(failures) == 1

    asyncio.run(_run_test())


def test_json_columns_match_schema(migrated_url: str) -> None:
    repos = [RESOURCES, ALLOCATIONS, SYSTEMS, INVESTIGATIONS, RUNS, DEBUG_SESSIONS, JOBS, ARTIFACTS]

    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            for repo in repos:
                cur = await conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s AND data_type = 'jsonb'",
                    (repo._table,),
                )
                actual = {row[0] for row in await cur.fetchall()}
                assert repo._json_columns == actual, f"{repo._table}: {repo._json_columns} != {actual}"

    asyncio.run(_run_test())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_repositories.py -q`
Expected: import error — `kdive.db.repositories` does not exist.

- [ ] **Step 3: Write `repositories.py`**

Create `src/kdive/db/repositories.py`:

```python
"""Typed async CRUD over the M0 durable objects (ADR-0003, ADR-0016).

A base `Repository[M]` provides `insert` / `get`; `StatefulRepository[M, S]` adds
`update_state`, guarded by `kdive.domain.state.can_transition` and bound to the
object's state enum `S`. Module-level instances bind these to each table. Rows map to
Pydantic models field-for-column; the database owns the `created_at` / `updated_at`
timestamps (they are omitted from inserts and read back via `RETURNING *`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.models import (
    Allocation,
    Artifact,
    DebugSession,
    DomainModel,
    Investigation,
    Job,
    Resource,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
    ensure_transition,
)

M = TypeVar("M", bound=DomainModel)
S = TypeVar("S", bound=StrEnum)

# DB-authoritative columns, omitted from inserts so their defaults/trigger apply.
_SERVER_GENERATED = ("created_at", "updated_at")


class ObjectNotFound(RuntimeError):
    """An `update_state` target id does not exist — a consistency error."""


class Repository(Generic[M]):
    """Async `insert` / `get` for one durable-object table."""

    def __init__(
        self,
        model: type[M],
        table: str,
        *,
        json_columns: frozenset[str] = frozenset(),
    ) -> None:
        self._model = model
        self._table = table
        self._json_columns = json_columns
        self._insert_columns = tuple(
            name for name in model.model_fields if name not in _SERVER_GENERATED
        )

    def _insert_params(self, obj: M) -> dict[str, Any]:
        dumped = obj.model_dump()
        return {
            name: Jsonb(dumped[name]) if name in self._json_columns else dumped[name]
            for name in self._insert_columns
        }

    async def insert(self, conn: AsyncConnection, obj: M) -> M:
        """Insert ``obj`` and return it as persisted (DB-authoritative timestamps)."""
        columns = ", ".join(self._insert_columns)
        placeholders = ", ".join(f"%({name})s" for name in self._insert_columns)
        sql = f"INSERT INTO {self._table} ({columns}) VALUES ({placeholders}) RETURNING *"
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, self._insert_params(obj))
            row = await cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields one row.
        return self._model.model_validate(row)

    async def get(self, conn: AsyncConnection, obj_id: UUID) -> M | None:
        """Return the object with ``obj_id``, or ``None`` if absent."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT * FROM {self._table} WHERE id = %s", (obj_id,))
            row = await cur.fetchone()
        return None if row is None else self._model.model_validate(row)


class StatefulRepository(Repository[M], Generic[M, S]):
    """A `Repository` plus `update_state`, bound to the object's state enum ``S``."""

    def __init__(
        self,
        model: type[M],
        table: str,
        state_enum: type[S],
        *,
        state_column: str = "state",
        json_columns: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(model, table, json_columns=json_columns)
        self._state_enum = state_enum
        self._state_column = state_column

    async def update_state(self, conn: AsyncConnection, obj_id: UUID, new_state: S) -> M:
        """Transition ``obj_id`` to ``new_state`` if `can_transition` permits it.

        Reads the current state under `FOR UPDATE` and writes in one transaction, so
        concurrent updaters are serialized.

        Raises:
            ObjectNotFound: No row has ``obj_id``.
            IllegalTransition: The current → ``new_state`` edge is not permitted.
        """
        col = self._state_column
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {col} FROM {self._table} WHERE id = %s FOR UPDATE", (obj_id,)
            )
            row = await cur.fetchone()
            if row is None:
                raise ObjectNotFound(f"{self._table} id {obj_id} does not exist")
            ensure_transition(self._state_enum(row[col]), new_state)
            await cur.execute(
                f"UPDATE {self._table} SET {col} = %s WHERE id = %s RETURNING *",
                (new_state, obj_id),
            )
            updated = await cur.fetchone()
        assert updated is not None  # The row existed under FOR UPDATE.
        return self._model.model_validate(updated)


RESOURCES = StatefulRepository(
    Resource, "resources", ResourceStatus, state_column="status",
    json_columns=frozenset({"capabilities"}),
)
ALLOCATIONS = StatefulRepository(
    Allocation, "allocations", AllocationState, json_columns=frozenset({"capability_scope"})
)
SYSTEMS = StatefulRepository(
    System, "systems", SystemState, json_columns=frozenset({"provisioning_profile"})
)
INVESTIGATIONS = StatefulRepository(
    Investigation, "investigations", InvestigationState, json_columns=frozenset({"external_refs"})
)
RUNS = StatefulRepository(Run, "runs", RunState, json_columns=frozenset({"build_profile"}))
DEBUG_SESSIONS = StatefulRepository(DebugSession, "debug_sessions", DebugSessionState)
JOBS = StatefulRepository(
    Job, "jobs", JobState, json_columns=frozenset({"payload", "authorizing"})
)
ARTIFACTS = Repository(Artifact, "artifacts")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_repositories.py -q`
Expected: all PASS.

- [ ] **Step 5: Guardrails**

Run: `uv run ruff check src/kdive/db/repositories.py tests/db/test_repositories.py && uv run ruff format --check src/kdive/db/repositories.py tests/db/test_repositories.py && uv run ty check src`
Expected: clean. If `ty` rejects the `StatefulRepository(Repository[M], Generic[M, S])` base list, this is the standard "subclass adds a type variable" pattern; keep the explicit `Generic[M, S]`. If `ty` flags `AsyncConnection` as needing an argument, use `AsyncConnection[Any]`.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/repositories.py tests/db/test_repositories.py
git commit -m "feat(db): add typed async repositories with state-transition guard"
```

---

## Task 4: Idempotency ledger (`idempotency.py`)

**Files:**
- Create: `tests/db/test_idempotency.py`
- Create: `src/kdive/db/idempotency.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_idempotency.py`:

```python
"""Tests for the idempotent step ledger (ADR-0005, ADR-0016)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import psycopg
import pytest

from kdive.db.idempotency import run_step


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _seed_run(conn: psycopg.AsyncConnection) -> UUID:
    """Insert the resource->allocation->system + investigation -> run FK chain."""

    async def _ins(sql: str, params: tuple[object, ...] = ()) -> Any:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        assert row is not None
        return row[0]

    rid = await _ins(
        "INSERT INTO resources (kind, pool, cost_class, status, host_uri) "
        "VALUES ('local-libvirt', 'p', 'c', 'available', 'qemu:///system') RETURNING id"
    )
    aid = await _ins(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, 'requested', 'alice', 'proj') RETURNING id",
        (rid,),
    )
    sid = await _ins(
        "INSERT INTO systems (allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, 'defined', '{}'::jsonb, 'alice', 'proj') RETURNING id",
        (aid,),
    )
    iid = await _ins(
        "INSERT INTO investigations (title, state, principal, project) "
        "VALUES ('t', 'open', 'alice', 'proj') RETURNING id"
    )
    return await _ins(
        "INSERT INTO runs (investigation_id, system_id, state, build_profile, principal, project) "
        "VALUES (%s, %s, 'created', '{}'::jsonb, 'alice', 'proj') RETURNING id",
        (iid, sid),
    )


def test_runs_fn_once_across_replays(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def fn() -> dict[str, int]:
                nonlocal calls
                calls += 1
                return {"v": 1}

            first = await run_step(conn, run_id, "build", fn)
            second = await run_step(conn, run_id, "build", fn)
            assert first == {"v": 1}
            assert second == {"v": 1}
            assert calls == 1

    asyncio.run(_run_test())


def test_none_result_is_recorded(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def fn() -> None:
                nonlocal calls
                calls += 1
                return None

            assert await run_step(conn, run_id, "s", fn) is None
            assert await run_step(conn, run_id, "s", fn) is None
            assert calls == 1

    asyncio.run(_run_test())


def test_noncanonical_result_returns_stored_form(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def fn() -> Any:
                nonlocal calls
                calls += 1
                return (1, 2)  # a tuple; jsonb returns it as a list

            first = await run_step(conn, run_id, "t", fn)
            second = await run_step(conn, run_id, "t", fn)
            assert first == [1, 2]
            assert second == [1, 2]
            assert calls == 1

    asyncio.run(_run_test())


def test_distinct_steps_are_independent(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)

            async def fn_a() -> dict[str, str]:
                return {"step": "a"}

            async def fn_b() -> dict[str, str]:
                return {"step": "b"}

            assert await run_step(conn, run_id, "a", fn_a) == {"step": "a"}
            assert await run_step(conn, run_id, "b", fn_b) == {"step": "b"}

    asyncio.run(_run_test())


def test_failed_fn_is_not_recorded(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def boom() -> dict[str, int]:
                nonlocal calls
                calls += 1
                raise ValueError("boom")

            async def ok() -> dict[str, bool]:
                nonlocal calls
                calls += 1
                return {"ok": True}

            with pytest.raises(ValueError, match="boom"):
                await run_step(conn, run_id, "step", boom)
            assert await run_step(conn, run_id, "step", ok) == {"ok": True}
            assert calls == 2

    asyncio.run(_run_test())


def test_concurrent_first_call_resolves_to_one_result(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as setup:
            run_id = await _seed_run(setup)
        async with (
            await _connect(migrated_url) as a,
            await _connect(migrated_url) as b,
        ):

            async def go(conn: psycopg.AsyncConnection, tag: str) -> Any:
                async def fn() -> dict[str, str]:
                    await asyncio.sleep(0.05)  # widen the race so both miss the cache
                    return {"by": tag}

                return await run_step(conn, run_id, "race", fn)

            results = await asyncio.gather(go(a, "a"), go(b, "b"))
        assert results[0] == results[1]  # both return the committed winner's value

    asyncio.run(_run_test())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_idempotency.py -q`
Expected: import error — `kdive.db.idempotency` does not exist.

- [ ] **Step 3: Write `idempotency.py`**

Create `src/kdive/db/idempotency.py`:

```python
"""Idempotent step execution backed by the `run_steps` ledger (ADR-0005, ADR-0016).

`run_step` runs a step's function at most once per `(run_id, step)`: a recorded row
short-circuits to the stored result; otherwise the function runs and its result is
stored under the unique `(run_id, step)` key. Every path returns the value as read
back from `jsonb`, so a replay equals the original even for a value the round-trip
would normalize. The concurrent-first-call resolution assumes the caller's
transaction runs at READ COMMITTED (psycopg's default).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


async def run_step(
    conn: AsyncConnection,
    run_id: UUID,
    step: str,
    fn: Callable[[], Awaitable[JsonValue]],
) -> JsonValue:
    """Execute ``step`` for ``run_id`` once, returning the stored result on replay.

    Args:
        conn: An async connection (READ COMMITTED).
        run_id: The owning run; must reference an existing ``runs`` row.
        step: The step name, unique within the run.
        fn: The step body, awaited only when no result is recorded yet.

    Returns:
        The step result as read back from ``jsonb`` (identical across replays).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step)
        )
        existing = await cur.fetchone()
        if existing is not None:
            return existing["result"]
        result = await fn()
        await cur.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, %s, 'succeeded', %s) "
            "ON CONFLICT (run_id, step) DO NOTHING RETURNING result",
            (run_id, step, Jsonb(result)),
        )
        inserted = await cur.fetchone()
        if inserted is not None:
            return inserted["result"]
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step)
        )
        winner = await cur.fetchone()
    assert winner is not None  # ON CONFLICT fired, so a committed row exists.
    return winner["result"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_idempotency.py -q`
Expected: all PASS.

- [ ] **Step 5: Guardrails**

Run: `uv run ruff check src/kdive/db/idempotency.py tests/db/test_idempotency.py && uv run ruff format --check src/kdive/db/idempotency.py tests/db/test_idempotency.py && uv run ty check src`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/idempotency.py tests/db/test_idempotency.py
git commit -m "feat(db): add idempotent run_step ledger"
```

---

## Task 5: Full-suite guardrails

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite + all guardrails**

```bash
KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest -q
uv run ruff check
uv run ruff format --check
uv run ty check src
```
Expected: all PASS, zero warnings. `ty check src` is hard-gating in CI.

- [ ] **Step 2: Commit any formatting fixups (only if needed)**

```bash
git add -A
git commit -m "style(db): apply ruff formatting"
```

---

## Self-review (spec coverage)

- `repositories.py` insert/get for all eight objects + `update_state` for the seven lifecycle objects → Task 3; `test_roundtrip_every_object`, `test_update_state_*`. ✓
- `Artifact` write-once (no `update_state`) → `ARTIFACTS = Repository(...)` (base class, no method) → Task 3. ✓
- DB-authoritative timestamps, caller-minted id → `_SERVER_GENERATED` omission + `RETURNING *`; `test_insert_timestamps_are_db_authoritative`. ✓
- State changes guarded by `can_transition` → `ensure_transition` in `update_state`; `test_update_state_illegal_raises`. ✓
- `update_state` raises `ObjectNotFound` (unknown id) / `IllegalTransition`; concurrent loser raises `IllegalTransition` → Task 3; `test_update_state_unknown_id_raises`, `test_update_state_concurrent_same_target`. ✓
- jsonb columns wrapped + `json_columns` drift guard → `_insert_params` + `test_json_columns_match_schema`. ✓
- `locks.py` `advisory_xact_lock` with single-bigint key, transaction-status guard → Task 2; `test_lock_blocks_until_holder_commits` (acceptance: blocks until commit, two connections, proven via `pg_locks`), `test_no_open_transaction_raises`, `test_lock_key_deterministic_and_scope_sensitive`. ✓
- `idempotency.py` `run_step` replay without re-execution, stored-form return, failure-not-recorded, concurrency → Task 4; `test_runs_fn_once_across_replays` (acceptance: call count == 1), `test_noncanonical_result_returns_stored_form`, `test_failed_fn_is_not_recorded`, `test_concurrent_first_call_resolves_to_one_result`. ✓
- Async tests via `asyncio.run` (no `pytest-asyncio`); disposable Postgres via existing fixtures + `migrated_url` → Task 1. ✓
- Env-gated libvirt/gdb/drgn integration tests untouched → no changes to those paths. ✓
