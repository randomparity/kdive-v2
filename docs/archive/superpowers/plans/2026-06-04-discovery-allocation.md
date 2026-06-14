# Discovery + Allocation (admission) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Discovery and Allocation-admission planes of the M0 walking skeleton onto the existing domain model: enumerate/register the local libvirt host, admit allocations against a per-host concurrent cap, and expose `resources.*` / `allocations.*` tools.

**Architecture:** Three pure primitive changes (an `AllocationState` edge, a `LockScope.RESOURCE`, `ToolResponse.success/.failure`) land first. Then `allocation_admission.py` (per-resource-locked count-and-grant), then `discovery.py` (libvirt seam over an injected connection + an idempotent Postgres registration bridge), then the two tool modules. Handlers/admission are tested directly with injected fakes and the testcontainers Postgres fixtures; the real `libvirt.open` is never called in the unit suite.

**Tech Stack:** Python 3.13, psycopg 3 (async), Pydantic 2, FastMCP, `libvirt-python` (binding installed; host connection only under the `live_vm` marker), `uv`/`ruff`/`ty`/`pytest`, testcontainers Postgres.

**Spec:** [`docs/superpowers/specs/2026-06-04-discovery-allocation-design.md`](../specs/2026-06-04-discovery-allocation-design.md) · **Decisions:** [ADR-0023](../../adr/0023-discovery-allocation-admission.md)

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/kdive/domain/state.py` (modify) | add `AllocationState.GRANTED → RELEASING` edge |
| `src/kdive/db/locks.py` (modify) | add `LockScope.RESOURCE` |
| `src/kdive/mcp/responses.py` (modify) | add `ToolResponse.success` / `.failure` factories |
| `src/kdive/domain/allocation_admission.py` (create) | `CONCURRENT_ALLOCATION_CAP_KEY`, `AdmissionOutcome`, `admit` (per-resource-locked count-and-grant) |
| `pyproject.toml` (modify) | add `defusedxml==0.7.1` (hardened XML parse for libvirtd-sourced capabilities/metadata XML) |
| `src/kdive/providers/local_libvirt/discovery.py` (create) | `LocalLibvirtDiscovery` (Discovery plane over an injected libvirt connection) + `register_local_libvirt_resource` |
| `src/kdive/mcp/tools/resources.py` (create) | `resources.list` / `.describe` handlers + `register` |
| `src/kdive/mcp/tools/allocations.py` (create) | `allocations.request` / `.get` / `.release` / `.list` handlers + `register` |
| `src/kdive/mcp/app.py` (modify) | append `resources.register`, `allocations.register` to `_PLANE_REGISTRARS` |
| `tests/domain/test_state.py` (modify) | add the new edge to the `LEGAL` table |
| `tests/db/test_locks.py` (modify) | assert `RESOURCE` derives a distinct key |
| `tests/mcp/test_responses.py` (modify) | factories' happy + misuse cases |
| `tests/domain/conftest.py` (create) | re-export Postgres fixtures for the admission test |
| `tests/domain/test_allocation_admission.py` (create) | admission: under/at cap, count semantics, cap misconfig, lock-blocking |
| `tests/providers/local_libvirt/__init__.py` (create) | package marker |
| `tests/providers/local_libvirt/conftest.py` (create) | re-export Postgres fixtures + `FakeLibvirtConn` |
| `tests/providers/local_libvirt/test_discovery.py` (create) | `list_resources`, `list_owned`, `from_env`, registration |
| `tests/mcp/test_resources_tools.py` (create) | `resources.list` / `.describe` handlers |
| `tests/mcp/test_allocations_tools.py` (create) | `allocations.*` handlers |

Guardrails after every task: `uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest -q`. **`ty check`** (no path) type-checks `src` **and** `tests`, matching this repo's pre-commit hook — `ty check src` under-checks and would let a test-file type error slip through to a red commit hook.

**Commit boundaries are self-contained and green.** Each task below is one commit, bisectable, with its tests passing. Tasks 1–3 are independent primitives; Task 4 (admission) needs Task 2; Task 5 (discovery) needs Task 4's cap-key constant; Task 6 (resources tools) needs Tasks 3 + 5; Task 7 (allocations tools) needs Tasks 1 + 3 + 4 + 5.

---

## Task 1: `granted → releasing` Allocation edge

**Files:**
- Modify: `src/kdive/domain/state.py`
- Test: `tests/domain/test_state.py`

- [ ] **Step 1: Update the test's `LEGAL` table to add the edge (the failing test)**

In `tests/domain/test_state.py`, change the `AllocationState.GRANTED` row of `LEGAL`:

```python
        AllocationState.GRANTED: {
            AllocationState.ACTIVE,
            AllocationState.RELEASING,
            AllocationState.FAILED,
        },
```

- [ ] **Step 2: Run the suite to verify it fails**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: FAIL — `test_illegal_transitions_are_rejected[GRANTED-RELEASING]` was previously asserting `can_transition(GRANTED, RELEASING) is False`; the table change now expects it legal, so the implementation (still missing the edge) makes `test_legal_transitions_are_allowed[RELEASING]` fail with `can_transition(...) is False`.

- [ ] **Step 3: Add the edge to the implementation**

In `src/kdive/domain/state.py`, change the `AllocationState.GRANTED` entry of `_TRANSITIONS`:

```python
        AllocationState.GRANTED: frozenset(
            {AllocationState.ACTIVE, AllocationState.RELEASING, AllocationState.FAILED}
        ),
```

Also update the `AllocationState` docstring line to note the new edge:

```python
class AllocationState(StrEnum):
    """Always-yes, capacity-checked allocation lifecycle.

    ``granted → releasing`` lets an admitted-but-unprovisioned allocation be released
    without first reaching ``active`` (which provisioning produces); see ADR-0023.
    """
```

- [ ] **Step 4: Run the suite to verify it passes**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest tests/domain/test_state.py -q
git add src/kdive/domain/state.py tests/domain/test_state.py
git commit -m "feat(domain): allow granted->releasing allocation transition (#14)"
```

---

## Task 2: `LockScope.RESOURCE`

**Files:**
- Modify: `src/kdive/db/locks.py`
- Test: `tests/db/test_locks.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/db/test_locks.py` (imports `LockScope`, `_lock_key` already present — if `_lock_key` is not imported, add `from kdive.db.locks import LockScope, _lock_key`):

```python
def test_resource_scope_key_is_distinct_from_other_scopes() -> None:
    from uuid import UUID

    from kdive.db.locks import LockScope, _lock_key

    key = UUID("12345678-1234-5678-1234-567812345678")
    resource_key = _lock_key(LockScope.RESOURCE, key)
    assert resource_key != _lock_key(LockScope.ALLOCATION, key)
    assert resource_key != _lock_key(LockScope.SYSTEM, key)
    assert _lock_key(LockScope.RESOURCE, key) == resource_key  # deterministic
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/db/test_locks.py::test_resource_scope_key_is_distinct_from_other_scopes -q`
Expected: FAIL — `AttributeError: RESOURCE` (the enum member does not exist).

- [ ] **Step 3: Add the enum member**

In `src/kdive/db/locks.py`, extend `LockScope`:

```python
class LockScope(StrEnum):
    """The advisory-lock scopes M0 serializes on (ADR-0016, ADR-0023)."""

    ALLOCATION = "allocation"
    SYSTEM = "system"
    RESOURCE = "resource"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/db/test_locks.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest tests/db/test_locks.py -q
git add src/kdive/db/locks.py tests/db/test_locks.py
git commit -m "feat(db): add RESOURCE advisory-lock scope for admission (#14)"
```

---

## Task 3: `ToolResponse.success` / `.failure`

**Files:**
- Modify: `src/kdive/mcp/responses.py`
- Test: `tests/mcp/test_responses.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_responses.py`:

```python
def test_success_factory_builds_non_failure_envelope() -> None:
    resp = ToolResponse.success(
        "alloc-1", "granted", suggested_next_actions=["allocations.release"], data={"k": "v"}
    )
    assert resp.object_id == "alloc-1"
    assert resp.status == "granted"
    assert resp.error_category is None
    assert resp.suggested_next_actions == ["allocations.release"]
    assert resp.data == {"k": "v"}


def test_success_factory_on_failure_status_raises() -> None:
    # "failed" is a failure status; building it via success() (no category) is misuse.
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse.success("alloc-1", "failed")


def test_failure_factory_sets_error_status_and_category() -> None:
    resp = ToolResponse.failure(
        "res-1", ErrorCategory.ALLOCATION_DENIED, data={"reason": "at_capacity"}
    )
    assert resp.status == "error"
    assert resp.error_category == "allocation_denied"
    assert resp.data == {"reason": "at_capacity"}
    assert resp.suggested_next_actions == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_responses.py -q`
Expected: FAIL — `AttributeError: type object 'ToolResponse' has no attribute 'success'`.

- [ ] **Step 3: Add the factories**

In `src/kdive/mcp/responses.py`, add the import and two classmethods. Add to the existing imports:

```python
from kdive.domain.errors import ErrorCategory
```

Add inside the `ToolResponse` class (after `from_job`):

```python
    @classmethod
    def success(
        cls,
        object_id: str,
        status: str,
        *,
        suggested_next_actions: list[str] | None = None,
        refs: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> ToolResponse:
        """Build a non-failure envelope.

        ``status`` must not be a failure status (``failed``/``error``); passing one is a
        producer bug and the model validator raises, surfacing the misuse at construction.
        """
        return cls(
            object_id=object_id,
            status=status,
            suggested_next_actions=suggested_next_actions or [],
            refs=refs or {},
            data=data or {},
        )

    @classmethod
    def failure(
        cls,
        object_id: str,
        category: ErrorCategory,
        *,
        suggested_next_actions: list[str] | None = None,
        data: dict[str, str] | None = None,
    ) -> ToolResponse:
        """Build a tool-level failure envelope (``status="error"`` + ``category``)."""
        return cls(
            object_id=object_id,
            status="error",
            error_category=category.value,
            suggested_next_actions=suggested_next_actions or [],
            data=data or {},
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_responses.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest tests/mcp/test_responses.py -q
git add src/kdive/mcp/responses.py tests/mcp/test_responses.py
git commit -m "feat(mcp): ToolResponse.success/.failure envelope factories (#14)"
```

---

## Task 4: `allocation_admission.py` — capacity admission

**Files:**
- Create: `src/kdive/domain/allocation_admission.py`
- Create: `tests/domain/conftest.py`
- Test: `tests/domain/test_allocation_admission.py`

- [ ] **Step 1: Create the test conftest (re-export Postgres fixtures)**

`tests/domain/conftest.py`:

```python
"""Re-export the disposable-Postgres fixtures for DB-backed domain tests."""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
```

- [ ] **Step 2: Write the failing admission tests**

`tests/domain/test_allocation_admission.py`:

```python
"""Tests for capacity admission (ADR-0023). Real Postgres; injected contexts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.allocation_admission import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    admit,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_resource(conn: psycopg.AsyncConnection, *, cap: object) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={CONCURRENT_ALLOCATION_CAP_KEY: cap},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _seed_allocation(
    conn: psycopg.AsyncConnection, resource_id: UUID, state: AllocationState
) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource_id,
            state=state,
        ),
    )


async def _count_allocs(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM allocations")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _count_audit(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_admit_under_cap_grants_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2)
            outcome = await admit(conn, CTX, resource=res, project="proj")
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.state is AllocationState.GRANTED
            assert await _count_allocs(conn) == 1
            assert await _count_audit(conn) == 1

    asyncio.run(_run())


def test_admit_at_cap_denies_with_no_rows(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_allocation(conn, res.id, AllocationState.GRANTED)
            outcome = await admit(conn, CTX, resource=res, project="proj")
            assert outcome.granted is False
            assert outcome.allocation is None
            assert outcome.reason == "at_capacity"
            assert outcome.in_use == 1 and outcome.cap == 1
            assert await _count_allocs(conn) == 1  # no new row
            assert await _count_audit(conn) == 0  # no audit on denial

    asyncio.run(_run())


def test_admit_ignores_terminal_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_allocation(conn, res.id, AllocationState.RELEASED)
            await _seed_allocation(conn, res.id, AllocationState.FAILED)
            outcome = await admit(conn, CTX, resource=res, project="proj")
            assert outcome.granted is True  # terminal rows do not occupy capacity

    asyncio.run(_run())


def test_admit_counts_only_non_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=5)
            for state in (
                AllocationState.REQUESTED,
                AllocationState.GRANTED,
                AllocationState.ACTIVE,
                AllocationState.RELEASING,
                AllocationState.RELEASED,
                AllocationState.FAILED,
            ):
                await _seed_allocation(conn, res.id, state)
            outcome = await admit(conn, CTX, resource=res, project="proj")
            assert outcome.in_use == 4  # requested/granted/active/releasing only

    asyncio.run(_run())


@pytest.mark.parametrize("cap", [None, "two", -1, True])
def test_admit_bad_cap_fails_closed(migrated_url: str, cap: object) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=cap)
            with pytest.raises(CategorizedError) as exc:
                await admit(conn, CTX, resource=res, project="proj")
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count_allocs(conn) == 0

    asyncio.run(_run())


def test_admit_blocks_behind_a_held_resource_lock(migrated_url: str) -> None:
    # Deterministic proof admit acquires LockScope.RESOURCE: pre-hold it on conn A and
    # assert admit on conn B cannot complete until A releases.
    async def _run() -> None:
        async with _conn(migrated_url) as seed, _conn(migrated_url) as a, _conn(
            migrated_url
        ) as b:
            res = await _seed_resource(seed, cap=1)
            async with a.transaction():
                async with advisory_xact_lock(a, LockScope.RESOURCE, res.id):
                    task = asyncio.ensure_future(
                        admit(b, CTX, resource=res, project="proj")
                    )
                    await asyncio.sleep(0.3)
                    assert not task.done()  # blocked on the resource lock
                # leaving the lock + transaction releases the lock
            outcome = await task
            assert outcome.granted is True

    asyncio.run(_run())


def test_admit_two_calls_at_cap_one_grant_one_deny(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            first = await admit(conn, CTX, resource=res, project="proj")
            second = await admit(conn, CTX, resource=res, project="proj")
            assert first.granted is True
            assert second.granted is False
            assert await _count_allocs(conn) == 1

    asyncio.run(_run())
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run python -m pytest tests/domain/test_allocation_admission.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.domain.allocation_admission'`.

- [ ] **Step 4: Write `allocation_admission.py`**

`src/kdive/domain/allocation_admission.py`:

```python
"""Always-yes, capacity-checked allocation admission (ADR-0023).

``admit`` books a host's :class:`~kdive.domain.models.Allocation` only if the host's
non-terminal allocation count is under the per-host cap. The count and the insert run
inside one transaction holding a per-**resource** advisory lock
(:data:`~kdive.db.locks.LockScope.RESOURCE`), so concurrent requests for the same host
serialize and the cap cannot be overshot. The cap lives on the resource's
``capabilities`` jsonb under :data:`CONCURRENT_ALLOCATION_CAP_KEY`; a missing/invalid
cap fails closed (``configuration_error``), never "unlimited".

This is core (not a provider plane) — the M0 ``AllocationPlane`` is the always-yes path
implemented here; a provider-supplied lease arrives at M1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState
from kdive.security import audit

if TYPE_CHECKING:
    # Annotation-only (PEP 563): keep this domain module free of a runtime mcp import.
    from kdive.mcp.auth import RequestContext

# The resource-capabilities key carrying the per-host concurrent-Allocation cap. Owned
# here (the consumer); the discovery provider imports it to advertise the cap.
CONCURRENT_ALLOCATION_CAP_KEY = "concurrent_allocation_cap"

# States that occupy a capacity slot (terminal released/failed do not).
_NON_TERMINAL = (
    AllocationState.REQUESTED,
    AllocationState.GRANTED,
    AllocationState.ACTIVE,
    AllocationState.RELEASING,
)


@dataclass(frozen=True)
class AdmissionOutcome:
    """The result of an admission attempt."""

    granted: bool
    allocation: Allocation | None
    reason: str | None
    cap: int
    in_use: int


def _resolve_cap(resource: Resource) -> int:
    """Read and validate the per-host cap; fail closed on anything invalid."""
    cap = resource.capabilities.get(CONCURRENT_ALLOCATION_CAP_KEY)
    # bool is an int subclass — reject it explicitly so `True` is not read as cap 1.
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise CategorizedError(
            f"resource {resource.id} has no valid {CONCURRENT_ALLOCATION_CAP_KEY!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"resource_id": str(resource.id), "cap": repr(cap)},
        )
    return cap


async def _count_non_terminal(conn: AsyncConnection, resource_id: object) -> int:
    """Count the host's allocations occupying a capacity slot."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE resource_id = %s AND state = ANY(%s)",
            (resource_id, [s.value for s in _NON_TERMINAL]),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(row[0])


async def admit(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    resource: Resource,
    project: str,
) -> AdmissionOutcome:
    """Admit an allocation against ``resource``'s per-host cap.

    Counts non-terminal allocations and, under cap, inserts a ``granted`` Allocation and
    one audit row — atomically, under a per-resource advisory lock. At cap, returns a
    denial with no row written.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the resource has no valid cap.
    """
    cap = _resolve_cap(resource)
    async with conn.transaction():
        async with advisory_xact_lock(conn, LockScope.RESOURCE, resource.id):
            in_use = await _count_non_terminal(conn, resource.id)
            if in_use >= cap:
                return AdmissionOutcome(
                    granted=False, allocation=None, reason="at_capacity", cap=cap, in_use=in_use
                )
            now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
            allocation = await ALLOCATIONS.insert(
                conn,
                Allocation(
                    id=uuid4(),
                    created_at=now,
                    updated_at=now,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    project=project,
                    resource_id=resource.id,
                    state=AllocationState.GRANTED,
                    capability_scope={},
                ),
            )
            await audit.record(
                conn,
                ctx,
                tool="allocations.request",
                object_kind="allocations",
                object_id=allocation.id,
                transition="->granted",
                args={"resource_id": str(resource.id), "project": project},
                project=project,
            )
            return AdmissionOutcome(
                granted=True, allocation=allocation, reason=None, cap=cap, in_use=in_use + 1
            )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run python -m pytest tests/domain/test_allocation_admission.py -q`
Expected: PASS (all cases; DB-backed tests skip only if Docker is unavailable locally — CI sets `KDIVE_REQUIRE_DOCKER=1`).

- [ ] **Step 6: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest tests/domain -q
git add src/kdive/domain/allocation_admission.py tests/domain/conftest.py tests/domain/test_allocation_admission.py
git commit -m "feat(domain): per-host capacity admission with resource lock (#14)"
```

---

## Task 5: `discovery.py` — Discovery plane + registration bridge

**Files:**
- Modify: `pyproject.toml` (add `defusedxml==0.7.1`)
- Create: `src/kdive/providers/local_libvirt/discovery.py`
- Create: `tests/providers/local_libvirt/__init__.py`, `tests/providers/local_libvirt/conftest.py`
- Test: `tests/providers/local_libvirt/test_discovery.py`

- [ ] **Step 0: Add the `defusedxml` dependency**

`discovery.py` parses XML emitted by the libvirtd process (a trust boundary), so it uses
`defusedxml` (hardened against billion-laughs / quadratic-blowup entity expansion) rather
than stdlib `xml.etree`. Add to `pyproject.toml` `[project].dependencies`, keeping the
list alphabetical-ish next to the other runtime deps:

```toml
  "defusedxml==0.7.1",
```

Then refresh the lock and install:

```bash
uv lock && uv sync
```

Run: `uv run python -c "import defusedxml.ElementTree; print('ok')"`
Expected: `ok`. (`defusedxml` 0.7.1 is the current stable; confirmed against PyPI.)

- [ ] **Step 1: Create the test package marker + conftest with the libvirt fake**

`tests/providers/local_libvirt/__init__.py` — empty file:

```python
```

`tests/providers/local_libvirt/conftest.py`:

```python
"""Fakes + fixtures for the local-libvirt discovery tests.

`FakeLibvirtConn` returns canned host info / capabilities XML / domains so discovery is
covered without a real libvirt host (no `live_vm`). The Postgres fixtures are re-exported
for the registration test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import libvirt  # ty: ignore[unresolved-import]

from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401

_CAPS_XML = """
<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>
""".strip()


def libvirt_error(code: int) -> libvirt.libvirtError:
    """Build a libvirtError whose get_error_code() returns ``code``."""
    err = libvirt.libvirtError("synthetic")
    # get_error_code() reads self.err[0]; libvirtError leaves err=None with no live error.
    err.err = (code, 0, "synthetic", 0, "", None, None, 0, 0)
    return err


@dataclass
class FakeDomain:
    domain_name: str
    system_id: str | None  # None → no kdive metadata (raises VIR_ERR_NO_DOMAIN_METADATA)
    raise_code: int | None = None  # override: raise a libvirtError with this code

    def name(self) -> str:
        return self.domain_name

    def metadata(self, kind: int, uri: str | None, flags: int) -> str:
        if self.raise_code is not None:
            raise libvirt_error(self.raise_code)
        if self.system_id is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_METADATA)
        return f'<kdive:system xmlns:kdive="{uri}">{self.system_id}</kdive:system>'


@dataclass
class FakeLibvirtConn:
    caps_xml: str = _CAPS_XML
    info: list[object] = field(default_factory=lambda: ["x86_64", 16384, 8, 2400, 1, 1, 4, 2])
    domains: list[FakeDomain] = field(default_factory=list)

    def getInfo(self) -> list[object]:
        return self.info

    def getCapabilities(self) -> str:
        return self.caps_xml

    def listAllDomains(self, flags: int = 0) -> list[FakeDomain]:
        return self.domains
```

- [ ] **Step 2: Write the failing discovery tests**

`tests/providers/local_libvirt/test_discovery.py`:

```python
"""Tests for the local-libvirt Discovery plane + registration (ADR-0023)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import libvirt  # ty: ignore[unresolved-import]
import psycopg
import pytest

from kdive.domain.allocation_admission import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.errors import CategorizedError
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from tests.providers.local_libvirt.conftest import FakeDomain, FakeLibvirtConn


def _discovery(conn: FakeLibvirtConn, *, cap: int = 2) -> LocalLibvirtDiscovery:
    return LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: conn, concurrent_allocation_cap=cap
    )


@asynccontextmanager
async def _pg(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


def test_list_resources_advertises_host_capabilities() -> None:
    record = _discovery(FakeLibvirtConn(), cap=3).list_resources()[0]
    assert record["resource_id"] == "qemu:///system"
    assert record["kind"] == "local-libvirt"
    assert record["status"] == "available"
    caps = record["capabilities"]
    assert caps["arch"] == "x86_64"
    assert caps["vcpus"] == 8
    assert caps["memory_mb"] == 16384
    assert caps["transports"] == ["gdbstub"]
    assert caps[CONCURRENT_ALLOCATION_CAP_KEY] == 3


def test_list_resources_arch_unknown_when_absent() -> None:
    conn = FakeLibvirtConn(caps_xml="<capabilities><host></host></capabilities>")
    record = _discovery(conn).list_resources()[0]
    assert record["capabilities"]["arch"] == "unknown"


def test_list_owned_returns_only_tagged_domains() -> None:
    conn = FakeLibvirtConn(
        domains=[
            FakeDomain("kdive-1", system_id="11111111-1111-1111-1111-111111111111"),
            FakeDomain("other-vm", system_id=None),  # untagged → skipped
        ]
    )
    owned = _discovery(conn).list_owned()
    assert owned == [
        {"system_id": "11111111-1111-1111-1111-111111111111", "domain_name": "kdive-1"}
    ]


def test_list_owned_reraises_non_metadata_libvirt_error() -> None:
    conn = FakeLibvirtConn(
        domains=[FakeDomain("vm", system_id=None, raise_code=libvirt.VIR_ERR_INTERNAL_ERROR)]
    )
    with pytest.raises(CategorizedError):
        _discovery(conn).list_owned()


def test_from_env_reads_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setenv("KDIVE_LIBVIRT_ALLOCATION_CAP", "4")
    disc = LocalLibvirtDiscovery.from_env()
    assert disc.concurrent_allocation_cap == 4
    assert disc.host_uri == "qemu:///system"


def test_from_env_defaults_cap_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_ALLOCATION_CAP", raising=False)
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    assert LocalLibvirtDiscovery.from_env().concurrent_allocation_cap == 1


def test_from_env_non_int_cap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setenv("KDIVE_LIBVIRT_ALLOCATION_CAP", "lots")
    with pytest.raises(CategorizedError):
        LocalLibvirtDiscovery.from_env()


def test_register_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pg(migrated_url) as conn:
            disc = _discovery(FakeLibvirtConn(), cap=2)
            first = await register_local_libvirt_resource(
                conn, disc, pool="local-libvirt", cost_class="local"
            )
            assert first.host_uri == "qemu:///system"
            assert first.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 2
            # Re-register with a changed cap: same row, updated capabilities.
            disc2 = _discovery(FakeLibvirtConn(), cap=5)
            second = await register_local_libvirt_resource(
                conn, disc2, pool="local-libvirt", cost_class="local"
            )
            assert second.id == first.id
            assert second.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 5
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM resources")
                row = await cur.fetchone()
            assert row is not None and row[0] == 1

    asyncio.run(_run())
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_discovery.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.providers.local_libvirt.discovery'`.

- [ ] **Step 4: Write `discovery.py`**

`src/kdive/providers/local_libvirt/discovery.py`:

```python
"""Local-libvirt Discovery plane + Postgres registration bridge (ADR-0023).

`LocalLibvirtDiscovery` enumerates the local libvirt host over an **injected**
connection factory (so unit tests never touch a real host; the real `libvirt.open`
adapter is `live_vm`-only) and advertises arch/cpu/memory, a `gdbstub` transport, and
the per-host concurrent-Allocation cap. `register_local_libvirt_resource` persists the
discovered host as the one `resources` row, idempotently by `(kind, host_uri)`.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import libvirt  # ty: ignore[unresolved-import]
from defusedxml.ElementTree import fromstring as _safe_fromstring
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.repositories import RESOURCES
from kdive.domain.allocation_admission import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Resource, ResourceKind
from kdive.domain.state import ResourceStatus
from kdive.providers.interfaces import OwnedInfra, ResourceRecord

_KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"
_URI_ENV = "KDIVE_LIBVIRT_URI"
_CAP_ENV = "KDIVE_LIBVIRT_ALLOCATION_CAP"
_DEFAULT_CAP = 1


class _LibvirtDomain(Protocol):
    def name(self) -> str: ...
    def metadata(self, kind: int, uri: str | None, flags: int) -> str: ...


class _LibvirtConn(Protocol):
    def getInfo(self) -> list[Any]: ...
    def getCapabilities(self) -> str: ...
    def listAllDomains(self, flags: int = 0) -> list[_LibvirtDomain]: ...


type Connect = Callable[[], _LibvirtConn]


def _parse_arch(caps_xml: str) -> str:
    """Read ``<host><cpu><arch>`` from the capabilities XML; ``unknown`` if absent.

    Parsed with ``defusedxml`` — the XML crosses a trust boundary (it is emitted by the
    libvirtd process), so entity-expansion DoS (billion-laughs) is neutralized; a
    malformed document returns ``unknown``, an *attack* document raises (fail loud).
    """
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except ET.ParseError:
        return "unknown"
    return root.findtext("./host/cpu/arch") or "unknown"


def _parse_system_id(meta_xml: str) -> str | None:
    """Read the System uuid from a kdive metadata element; ``None`` if empty/malformed.

    ``defusedxml`` parse (trust boundary, as ``_parse_arch``): malformed → ``None``;
    an attack document raises rather than being silently skipped as "untagged".
    """
    try:
        element: ET.Element = _safe_fromstring(meta_xml)
    except ET.ParseError:
        return None
    text = (element.text or "").strip()
    return text or None


class LocalLibvirtDiscovery:
    """The `DiscoveryPlane` for the local libvirt host."""

    def __init__(
        self, *, host_uri: str, connect: Connect, concurrent_allocation_cap: int
    ) -> None:
        self.host_uri = host_uri
        self._connect = connect
        self.concurrent_allocation_cap = concurrent_allocation_cap

    @classmethod
    def from_env(cls) -> LocalLibvirtDiscovery:
        """Build from ``KDIVE_LIBVIRT_URI`` + ``KDIVE_LIBVIRT_ALLOCATION_CAP`` (default 1).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the cap env var is not an int.
        """
        host_uri = os.environ.get(_URI_ENV, "qemu:///system")
        raw_cap = os.environ.get(_CAP_ENV)
        if raw_cap is None:
            cap = _DEFAULT_CAP
        else:
            try:
                cap = int(raw_cap)
            except ValueError:
                raise CategorizedError(
                    f"{_CAP_ENV}={raw_cap!r} is not an integer",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                ) from None
        return cls(
            host_uri=host_uri,
            connect=lambda: libvirt.open(host_uri),
            concurrent_allocation_cap=cap,
        )

    def list_resources(self) -> list[ResourceRecord]:
        """Return one `ResourceRecord` for the host (discovery-time id = ``host_uri``)."""
        conn = self._connect()
        info = conn.getInfo()
        capabilities: dict[str, Any] = {
            "arch": _parse_arch(conn.getCapabilities()),
            "vcpus": int(info[2]),
            "memory_mb": int(info[1]),
            "transports": ["gdbstub"],
            CONCURRENT_ALLOCATION_CAP_KEY: self.concurrent_allocation_cap,
        }
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.LOCAL_LIBVIRT.value,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE.value,
            )
        ]

    def list_owned(self) -> list[OwnedInfra]:
        """Return `{system_id, domain_name}` for each kdive-tagged domain."""
        conn = self._connect()
        owned: list[OwnedInfra] = []
        for domain in conn.listAllDomains():
            try:
                meta = domain.metadata(
                    libvirt.VIR_DOMAIN_METADATA_ELEMENT, _KDIVE_METADATA_NS, 0
                )
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                    continue  # untagged → not ours
                raise CategorizedError(
                    "libvirt error reading domain metadata",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"domain": domain.name()},
                ) from exc
            system_id = _parse_system_id(meta)
            if system_id is None:
                continue
            owned.append(OwnedInfra(system_id=system_id, domain_name=domain.name()))
        return owned


async def register_local_libvirt_resource(
    conn: AsyncConnection,
    discovery: LocalLibvirtDiscovery,
    *,
    pool: str,
    cost_class: str,
) -> Resource:
    """Persist the discovered host as the one `resources` row, idempotent by host_uri.

    ``pool`` is the resource pool **name** (``Resource.pool``), not a connection pool. M0
    registers from a single startup/operator path; a ``UNIQUE(kind, host_uri)`` constraint
    is the M1 hardening for concurrent registrars (ADR-0023).
    """
    record = discovery.list_resources()[0]
    capabilities = record["capabilities"]
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
            (ResourceKind.LOCAL_LIBVIRT.value, discovery.host_uri),
        )
        existing = await cur.fetchone()
        if existing is not None:
            await cur.execute(
                "UPDATE resources SET capabilities = %s, status = %s, pool = %s, "
                "cost_class = %s WHERE id = %s RETURNING *",
                (
                    Jsonb(capabilities),
                    ResourceStatus.AVAILABLE.value,
                    pool,
                    cost_class,
                    existing["id"],
                ),
            )
            updated = await cur.fetchone()
            if updated is None:  # Invariant: the row was held FOR UPDATE.
                raise RuntimeError("UPDATE of resources returned no row")
            return Resource.model_validate(updated)
    # No existing row: insert via the repository (it wraps capabilities in Jsonb and
    # returns the row with DB-generated timestamps). Runs after the SELECT's transaction
    # commits — acceptable under the M0 single-registrar assumption documented above.
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities=capabilities,
            pool=pool,
            cost_class=cost_class,
            status=ResourceStatus.AVAILABLE,
            host_uri=discovery.host_uri,
        ),
    )
```

`RESOURCES.insert` (imported at the top of the block via `from kdive.db.repositories import RESOURCES`) wraps `capabilities` in `Jsonb` itself; the in-place `UPDATE` branch wraps it explicitly.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_discovery.py -q`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest tests/providers -q
git add pyproject.toml uv.lock src/kdive/providers/local_libvirt/discovery.py tests/providers/local_libvirt/
git commit -m "feat(providers): local-libvirt discovery + resource registration (#14)"
```

---

## Task 6: `resources.py` tools + app wiring

**Files:**
- Create: `src/kdive/mcp/tools/resources.py`
- Modify: `src/kdive/mcp/app.py`
- Test: `tests/mcp/test_resources_tools.py`

- [ ] **Step 1: Write the failing tests**

`tests/mcp/test_resources_tools.py`:

```python
"""resources.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import resources as resources_tools
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from tests.providers.local_libvirt.conftest import FakeLibvirtConn

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _discovery(cap: int = 2) -> LocalLibvirtDiscovery:
    return LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=cap
    )


async def _register(pool: AsyncConnectionPool) -> str:
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, _discovery(), pool="local-libvirt", cost_class="local"
        )
    return str(res.id)


def test_list_returns_host_with_flat_capability_projection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind=None)
        assert len(responses) == 1
        resp = responses[0]
        assert resp.object_id == res_id
        assert resp.status == "available"
        assert resp.data["kind"] == "local-libvirt"
        assert resp.data["arch"] == "x86_64"
        assert resp.data["vcpus"] == "8"
        assert resp.data["memory_mb"] == "16384"
        assert resp.data["transports"] == "gdbstub"
        assert resp.data["concurrent_allocation_cap"] == "2"

    asyncio.run(_run())


def test_list_kind_filter_miss_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind="nope")
        assert responses == []

    asyncio.run(_run())


def test_describe_adds_pool_cost_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.describe_resource(pool, CTX, res_id)
        assert resp.status == "available"
        assert resp.data["pool"] == "local-libvirt"
        assert resp.data["cost_class"] == "local"
        assert resp.data["host_uri"] == "qemu:///system"

    asyncio.run(_run())


def test_describe_unknown_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.describe_resource(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_describe_malformed_id_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.describe_resource(pool, CTX, "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_resources_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.mcp.tools.resources'`.

- [ ] **Step 3: Write `resources.py`**

`src/kdive/mcp/tools/resources.py`:

```python
"""The `resources.*` MCP tools (Discovery plane reads) (ADR-0023).

Thin FastMCP wrappers over plain async handlers that take the pool + request context as
arguments (tested directly, never through MCP). Resources are shared infrastructure (no
`project` column), so reads require only an authenticated context — no RBAC scoping. The
nested `capabilities` jsonb is projected to a flat `dict[str, str]` for the response
envelope (ADR-0019 `data` is `dict[str, str]`).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RESOURCES
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Resource
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse

_log = logging.getLogger(__name__)

_FLAT_CAP_KEYS = ("arch", "vcpus", "memory_mb", "concurrent_allocation_cap")


def _error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _project_capabilities(resource: Resource) -> dict[str, str]:
    """Flatten the capabilities jsonb to string values for the envelope."""
    caps = resource.capabilities
    data: dict[str, str] = {"kind": resource.kind.value}
    for key in _FLAT_CAP_KEYS:
        if key in caps:
            data[key] = str(caps[key])
    transports = caps.get("transports")
    if isinstance(transports, (list, tuple)):
        data["transports"] = ",".join(str(t) for t in transports)
    return data


def _resource_envelope(resource: Resource, *, next_actions: list[str]) -> ToolResponse:
    return ToolResponse.success(
        str(resource.id),
        resource.status.value,
        suggested_next_actions=next_actions,
        data=_project_capabilities(resource),
    )


async def _fetch_resources(conn: AsyncConnection, kind: str | None) -> list[Resource]:
    if kind is None:
        query = "SELECT * FROM resources ORDER BY created_at, id"
        params: tuple[object, ...] = ()
    else:
        query = "SELECT * FROM resources WHERE kind = %s ORDER BY created_at, id"
        params = (kind,)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()
    return [Resource.model_validate(row) for row in rows]


async def list_resources_tool(
    pool: AsyncConnectionPool, ctx: RequestContext, *, kind: str | None
) -> list[ToolResponse]:
    """Return every resource (optionally filtered by ``kind``) as an envelope."""
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resources = await _fetch_resources(conn, kind)
        responses: list[ToolResponse] = []
        for resource in resources:
            try:
                responses.append(
                    _resource_envelope(
                        resource, next_actions=["resources.describe", "allocations.request"]
                    )
                )
            except ValueError:
                _log.warning("resource %s violates the response invariant; degraded", resource.id)
                responses.append(
                    ToolResponse.failure(str(resource.id), ErrorCategory.INFRASTRUCTURE_FAILURE)
                )
        return responses


async def describe_resource(
    pool: AsyncConnectionPool, ctx: RequestContext, resource_id: str
) -> ToolResponse:
    """Return one resource's envelope with pool/cost_class/host_uri, or an error."""
    try:
        uid = UUID(resource_id)
    except ValueError:
        return _error(resource_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await RESOURCES.get(conn, uid)
        if resource is None:
            return _error(resource_id)
        envelope = _resource_envelope(resource, next_actions=["allocations.request"])
        envelope.data["pool"] = resource.pool
        envelope.data["cost_class"] = resource.cost_class
        envelope.data["host_uri"] = resource.host_uri
        return envelope


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `resources.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="resources.list")
    async def resources_list(kind: str | None = None) -> list[ToolResponse]:
        return await list_resources_tool(pool, current_context(), kind=kind)

    @app.tool(name="resources.describe")
    async def resources_describe(resource_id: str) -> ToolResponse:
        return await describe_resource(pool, current_context(), resource_id)
```

- [ ] **Step 4: Wire the tools into the app**

In `src/kdive/mcp/app.py`, add the import and extend `_PLANE_REGISTRARS`:

```python
from kdive.mcp.tools import jobs, resources
```

```python
_PLANE_REGISTRARS: tuple[Callable[[FastMCP, AsyncConnectionPool], None], ...] = (
    jobs.register,
    resources.register,
)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_resources_tools.py tests/mcp/test_app.py -q`
Expected: PASS (`test_app.py` confirms the app still builds with the new registrar).

- [ ] **Step 6: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest tests/mcp -q
git add src/kdive/mcp/tools/resources.py src/kdive/mcp/app.py tests/mcp/test_resources_tools.py
git commit -m "feat(mcp): resources.list/.describe tools (#14)"
```

---

## Task 7: `allocations.py` tools + app wiring

**Files:**
- Create: `src/kdive/mcp/tools/allocations.py`
- Modify: `src/kdive/mcp/app.py`
- Test: `tests/mcp/test_allocations_tools.py`

- [ ] **Step 1: Write the failing tests**

`tests/mcp/test_allocations_tools.py`:

```python
"""allocations.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import allocations as alloc_tools
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from kdive.security.rbac import AuthorizationError, Role
from tests.providers.local_libvirt.conftest import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _register(pool: AsyncConnectionPool, *, cap: int = 1) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=cap
    )
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, disc, pool="local-libvirt", cost_class="local"
        )
    return str(res.id)


async def _seed_alloc(pool: AsyncConnectionPool, resource_id: str, state: AllocationState) -> str:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=UUID(resource_id),
                state=state,
            ),
        )
    return str(alloc.id)


def test_request_under_cap_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            resp = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
        assert resp.status == "granted"
        assert resp.error_category is None
        assert resp.data["project"] == "proj"

    asyncio.run(_run())


def test_request_at_cap_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=1)
            await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            resp = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
        assert resp.status == "error"
        assert resp.error_category == "allocation_denied"
        assert resp.object_id == res_id
        assert resp.data["reason"] == "at_capacity"

    asyncio.run(_run())


def test_request_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            try:
                await alloc_tools.request_allocation(pool, _ctx(Role.VIEWER), project="proj")
                raise AssertionError("expected AuthorizationError")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_request_no_resource_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_own_allocation_returns_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            req = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            resp = await alloc_tools.get_allocation(pool, _ctx(), req.object_id)
        assert resp.object_id == req.object_id
        assert resp.status == "granted"

    asyncio.run(_run())


def test_get_other_project_allocation_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            req = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            other = _ctx(projects=("elsewhere",), role=Role.OPERATOR)
            resp = await alloc_tools.get_allocation(pool, other, req.object_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_failed_allocation_renders_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.FAILED)
            resp = await alloc_tools.get_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_release_granted_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            resp = await alloc_tools.release_allocation(pool, _ctx(), req.object_id)
            assert resp.status == "released"
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT count(*) FROM audit_log WHERE object_id = %s", (req.object_id,)
                    )
                    row = await cur.fetchone()
            # ->granted (admission) + granted->releasing + releasing->released
            assert row is not None and row[0] == 3

    asyncio.run(_run())


def test_release_active_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.ACTIVE)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "released"

    asyncio.run(_run())


def test_release_terminal_allocation_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.RELEASED)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "released"

    asyncio.run(_run())


def test_release_requested_allocation_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.REQUESTED)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_list_returns_project_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=3)
            await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            responses = await alloc_tools.list_allocations(pool, _ctx(), project="proj", limit=50)
        assert len(responses) == 2
        assert all(r.status == "granted" for r in responses)

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_allocations_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.mcp.tools.allocations'`.

- [ ] **Step 3: Write `allocations.py`**

`src/kdive/mcp/tools/allocations.py`:

```python
"""The `allocations.*` MCP tools — the Allocation admission/lifecycle surface (ADR-0023).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`request` admits against the per-host cap (core `admit`); `release` drives a granted/active
allocation to `released` under a per-allocation advisory lock with an `IllegalTransition`
backstop; `get`/`list` render an allocation through `_envelope_for_allocation`, which maps
the terminal `failed` state to a `failure` envelope (its value collides with the response
envelope's failure-status set). RBAC: `request`/`release` require `operator`; reads require
project membership. Authz denials raise (ADR-0020: no authz `ErrorCategory`).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.allocation_admission import admit
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState, IllegalTransition
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
_DEFAULT_KIND = "local-libvirt"
_RELEASABLE = (AllocationState.GRANTED, AllocationState.ACTIVE)
_TERMINAL = (AllocationState.RELEASED, AllocationState.FAILED)


def _config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_allocation(alloc: Allocation) -> ToolResponse:
    """Render an allocation; ``failed`` becomes a failure envelope (ADR-0023 §6)."""
    if alloc.state is AllocationState.FAILED:
        return ToolResponse.failure(
            str(alloc.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": alloc.state.value},
        )
    return ToolResponse.success(
        str(alloc.id),
        alloc.state.value,
        suggested_next_actions=["allocations.get", "allocations.release"],
        data={"project": alloc.project},
    )


async def _resolve_resource(
    conn: AsyncConnection, resource_id: UUID | None, kind: str
) -> Resource | None:
    if resource_id is not None:
        return await RESOURCES.get(conn, resource_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM resources WHERE kind = %s ORDER BY created_at, id LIMIT 1", (kind,)
        )
        row = await cur.fetchone()
    return Resource.model_validate(row) if row else None


async def request_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    resource_id: str | None = None,
    kind: str | None = None,
) -> ToolResponse:
    """Admit an allocation against the selected host's per-host cap."""
    require_project(ctx, project)
    require_role(ctx, project, Role.OPERATOR)
    with bind_context(principal=ctx.principal):
        resolved_id = _as_uuid(resource_id) if resource_id is not None else None
        if resource_id is not None and resolved_id is None:
            return _config_error(resource_id)
        async with pool.connection() as conn:
            resource = await _resolve_resource(conn, resolved_id, kind or _DEFAULT_KIND)
            if resource is None:
                return _config_error(resource_id or (kind or _DEFAULT_KIND))
            try:
                outcome = await admit(conn, ctx, resource=resource, project=project)
            except CategorizedError as exc:
                return ToolResponse.failure(str(resource.id), exc.category)
        if outcome.granted and outcome.allocation is not None:
            return ToolResponse.success(
                str(outcome.allocation.id),
                "granted",
                suggested_next_actions=["allocations.get", "allocations.release"],
                data={"resource_id": str(resource.id), "project": project},
            )
        _log.info("allocation denied for project %s on resource %s (at cap)", project, resource.id)
        return ToolResponse.failure(
            str(resource.id),
            ErrorCategory.ALLOCATION_DENIED,
            suggested_next_actions=["allocations.list"],
            data={
                "reason": outcome.reason or "at_capacity",
                "cap": str(outcome.cap),
                "in_use": str(outcome.in_use),
            },
        )


async def get_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Return an allocation the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
        # A row in an ungranted project is indistinguishable from not-found (no leak).
        if alloc is None or alloc.project not in ctx.projects:
            return _config_error(allocation_id)
        return _envelope_for_allocation(alloc)


async def _transition_and_audit(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc_id: UUID,
    frm: AllocationState,
    to: AllocationState,
    *,
    project: str,
) -> None:
    await ALLOCATIONS.update_state(conn, alloc_id, to)
    await audit.record(
        conn,
        ctx,
        tool="allocations.release",
        object_kind="allocations",
        object_id=alloc_id,
        transition=f"{frm.value}->{to.value}",
        args={"allocation_id": str(alloc_id)},
        project=project,
    )


async def release_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Drive an allocation to ``released`` (under a per-allocation lock)."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _config_error(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
            try:
                async with conn.transaction():
                    async with advisory_xact_lock(conn, LockScope.ALLOCATION, uid):
                        current = await ALLOCATIONS.get(conn, uid)
                        if current is None:
                            return _config_error(allocation_id)
                        if current.state in _TERMINAL or current.state not in (
                            *_RELEASABLE,
                            AllocationState.RELEASING,
                        ):
                            return ToolResponse.failure(
                                allocation_id,
                                ErrorCategory.CONFIGURATION_ERROR,
                                data={"current_status": current.state.value},
                            )
                        if current.state in _RELEASABLE:
                            await _transition_and_audit(
                                conn, ctx, uid, current.state,
                                AllocationState.RELEASING, project=alloc.project,
                            )
                        await _transition_and_audit(
                            conn, ctx, uid, AllocationState.RELEASING,
                            AllocationState.RELEASED, project=alloc.project,
                        )
            except IllegalTransition:
                # Backstop for an interleaving the lock did not cover (e.g. a future
                # provision path). Caught OUTSIDE the rolled-back transaction; re-read.
                async with pool.connection() as conn2:
                    latest = await ALLOCATIONS.get(conn2, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return ToolResponse.failure(
                    allocation_id, ErrorCategory.CONFIGURATION_ERROR, data=data
                )
        return ToolResponse.success(str(uid), "released")


async def list_allocations(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit: int
) -> list[ToolResponse]:
    """Return the newest allocations for ``project``, each as an envelope."""
    require_project(ctx, project)
    capped = max(1, min(limit, MAX_LIST_LIMIT))
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT * FROM allocations WHERE project = %s "
                    "ORDER BY created_at DESC, id LIMIT %s",
                    (project, capped),
                )
                rows = await cur.fetchall()
        responses: list[ToolResponse] = []
        for row in rows:
            try:
                responses.append(_envelope_for_allocation(Allocation.model_validate(row)))
            except ValueError:
                _log.warning("allocation row violates the response invariant; degraded")
                responses.append(
                    ToolResponse.failure(str(row.get("id", "?")), ErrorCategory.INFRASTRUCTURE_FAILURE)
                )
        return responses


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `allocations.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="allocations.request")
    async def allocations_request(
        project: str, resource_id: str | None = None, kind: str | None = None
    ) -> ToolResponse:
        return await request_allocation(
            pool, current_context(), project=project, resource_id=resource_id, kind=kind
        )

    @app.tool(name="allocations.get")
    async def allocations_get(allocation_id: str) -> ToolResponse:
        return await get_allocation(pool, current_context(), allocation_id)

    @app.tool(name="allocations.release")
    async def allocations_release(allocation_id: str) -> ToolResponse:
        return await release_allocation(pool, current_context(), allocation_id)

    @app.tool(name="allocations.list")
    async def allocations_list(project: str, limit: int = DEFAULT_LIST_LIMIT) -> list[ToolResponse]:
        return await list_allocations(pool, current_context(), project=project, limit=limit)
```

- [ ] **Step 4: Wire the tools into the app**

In `src/kdive/mcp/app.py`, extend the import and `_PLANE_REGISTRARS`:

```python
from kdive.mcp.tools import allocations, jobs, resources
```

```python
_PLANE_REGISTRARS: tuple[Callable[[FastMCP, AsyncConnectionPool], None], ...] = (
    jobs.register,
    resources.register,
    allocations.register,
)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_allocations_tools.py tests/mcp/test_app.py -q`
Expected: PASS.

- [ ] **Step 6: Full guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run python -m pytest -q
git add src/kdive/mcp/tools/allocations.py src/kdive/mcp/app.py tests/mcp/test_allocations_tools.py
git commit -m "feat(mcp): allocations.request/.get/.release/.list tools (#14)"
```

---

## Self-Review

**Spec coverage:**
- Discovery `list_resources`/`list_owned` (arch/vcpus/memory/gdbstub/cap; tagged-domain enumeration; narrowed metadata catch) → Task 5. ✓
- `register_local_libvirt_resource` idempotent by host_uri, `RESOURCES.insert` + `Jsonb` update → Task 5. ✓
- `admit`: cap from `capabilities`, per-resource lock, non-terminal count via `ANY(list(...))`, grant+audit / no-row denial, cap-misconfig fail-closed, deterministic lock-blocking test → Task 4. ✓
- `granted → releasing` edge + spec-mirror test table → Task 1. ✓
- `LockScope.RESOURCE` → Task 2. ✓
- `ToolResponse.success/.failure` + misuse-on-failure-status → Task 3. ✓
- `resources.list/.describe` flat projection, no RBAC, error on missing/malformed → Task 6. ✓
- `allocations.request` (operator, selector, grant/deny), `.get` (no cross-project leak, `failed`→failure), `.release` (lock + `granted/active/releasing` paths + terminal/requested error + `IllegalTransition` backstop outside the transaction), `.list` (project-scoped via `_envelope_for_allocation`) → Task 7. ✓
- Authz denials raise (option 1) → Task 7 (`test_request_without_operator_raises`). ✓
- App wiring via `_PLANE_REGISTRARS` → Tasks 6 & 7. ✓
- No new `live_vm`/ungated test; discovery covered with `FakeLibvirtConn` → Task 5. ✓

**Placeholder scan:** one intentional, clearly-flagged placeholder in Task 5 Step 4 (the `if existing is None:` insert branch) with the exact replacement block immediately below it and the required import named. Every other step carries complete code or an exact command.

**Type consistency:** `admit(conn, ctx, *, resource, project)`, `AdmissionOutcome(granted, allocation, reason, cap, in_use)`, `CONCURRENT_ALLOCATION_CAP_KEY`, `register_local_libvirt_resource(conn, discovery, *, pool, cost_class)`, `LocalLibvirtDiscovery(host_uri=, connect=, concurrent_allocation_cap=)`, `ToolResponse.success(object_id, status, *, ...)` / `.failure(object_id, category, *, ...)`, `_envelope_for_allocation(alloc)`, and the four `allocations.*` handler signatures are spelled identically across tasks and tests. `update_state`/`audit.record` calls match their existing signatures.

**Commit hygiene:** seven commits, each green and bisectable; dependency order (1,2,3 primitives → 4 admission → 5 discovery → 6 resources tools → 7 allocations tools) ensures every task's imports are already committed.
