# Investigation + Run lifecycle & tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `investigations.*` and `runs.*` MCP tool surfaces (issue #17) onto the existing domain model — the Investigation campaign and the Run join-point — with the binding invariant, the first-Run `open→active` flip, and mutable `external_refs`.

**Architecture:** Two new tool modules of thin `@app.tool` wrappers over plain async handlers (pool + ctx injected, tested directly), mirroring `allocations.py` / `systems.py`. State changes run under transaction-scoped advisory locks (a new `LockScope.INVESTIGATION`) with `audit.record` in the same transaction. The durable models, state machines, repositories, and Postgres tables already exist (shipped by #7); this issue adds only the tool layer plus one `LockScope` member and the app wiring.

**Tech Stack:** Python 3.13, FastMCP, psycopg3 (async) + `psycopg_pool`, Pydantic v2, pytest against a disposable Postgres (`migrated_url` fixture, `asyncio.run(_run())` idiom). Guardrails: `uv run ruff check` / `ruff format`, `uv run ty check`, `uv run python -m pytest -q`.

**Design source:** [`docs/superpowers/specs/2026-06-04-investigation-run-lifecycle-design.md`](../specs/2026-06-04-investigation-run-lifecycle-design.md) · [`docs/adr/0026-investigation-run-lifecycle.md`](../../adr/0026-investigation-run-lifecycle.md)

**Reference patterns (read before starting):** `src/kdive/mcp/tools/allocations.py` (synchronous tool shape, `_envelope_for_*`, `IllegalTransition` backstop, per-object lock), `src/kdive/mcp/tools/systems.py` (advisory-lock + audit-in-one-transaction, `_config_error` with `data`), `tests/mcp/test_allocations_tools.py` + `tests/mcp/test_systems_tools.py` (the `_pool` / `_ctx` / seeding helpers and `asyncio.run` idiom), `tests/db/test_locks.py` (the held-lock blocking proof).

---

## File Structure

- **Modify** `src/kdive/db/locks.py` — add `LockScope.INVESTIGATION`; document the global lock-acquisition order in the docstring.
- **Create** `src/kdive/mcp/tools/investigations.py` — `open` / `get` / `close` / `link` / `unlink` handlers + `register`.
- **Create** `src/kdive/mcp/tools/runs.py` — `create` / `get` handlers + `register`.
- **Modify** `src/kdive/mcp/app.py` — append `investigations.register`, `runs.register` to `_PLANE_REGISTRARS`.
- **Modify** `tests/db/test_locks.py` — assert `INVESTIGATION` derives a distinct lock key.
- **Create** `tests/mcp/test_investigations_tools.py` — the investigations handler tests.
- **Create** `tests/mcp/test_runs_tools.py` — the runs handler tests.
- **Modify** `tests/mcp/test_app.py` — assert the new tools register.

---

## Task 1: Add `LockScope.INVESTIGATION` and document the lock order

**Files:**
- Modify: `src/kdive/db/locks.py:22-28` (the `LockScope` enum) and the module docstring `src/kdive/db/locks.py:1-8`
- Test: `tests/db/test_locks.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/db/test_locks.py` (after `test_resource_scope_key_is_distinct_from_other_scopes`):

```python
def test_investigation_scope_key_is_distinct_from_other_scopes() -> None:
    key = UUID("12345678-1234-5678-1234-567812345678")
    inv_key = _lock_key(LockScope.INVESTIGATION, key)
    assert inv_key != _lock_key(LockScope.ALLOCATION, key)
    assert inv_key != _lock_key(LockScope.SYSTEM, key)
    assert inv_key != _lock_key(LockScope.RESOURCE, key)
    assert _lock_key(LockScope.INVESTIGATION, key) == inv_key  # deterministic
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/db/test_locks.py::test_investigation_scope_key_is_distinct_from_other_scopes -v`
Expected: FAIL with `AttributeError: INVESTIGATION` (the member does not exist yet).

- [ ] **Step 3: Add the enum member and document the order**

In `src/kdive/db/locks.py`, change the `LockScope` enum:

```python
class LockScope(StrEnum):
    """The advisory-lock scopes M0 serializes on (ADR-0016, ADR-0023, ADR-0026).

    Operations that hold more than one scope at once acquire them in the fixed global
    order ``ALLOCATION → SYSTEM → INVESTIGATION → RUN`` to avoid deadlock; e.g.
    ``runs.create`` takes ``SYSTEM`` then ``INVESTIGATION``. (``RUN`` is reserved in the
    ordering; no M0 tool needs a per-Run lock yet.)
    """

    ALLOCATION = "allocation"
    SYSTEM = "system"
    RESOURCE = "resource"
    INVESTIGATION = "investigation"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/db/test_locks.py -q`
Expected: PASS (all lock tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/locks.py tests/db/test_locks.py
git commit -m "feat(locks): add INVESTIGATION advisory-lock scope (#17)"
```

---

## Task 2: `investigations.py` — `open` + `get`

**Files:**
- Create: `src/kdive/mcp/tools/investigations.py`
- Test: `tests/mcp/test_investigations_tools.py`

- [ ] **Step 1: Write the failing tests (and shared test helpers)**

Create `tests/mcp/test_investigations_tools.py`:

```python
"""investigations.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import investigations as inv_tools
from kdive.security.rbac import AuthorizationError, Role


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _open(pool: AsyncConnectionPool, ctx: RequestContext, **kw: Any):
    return await inv_tools.open_investigation(pool, ctx, **kw)


def test_open_mints_investigation_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="kernel oops in xfs")
            assert resp.status == "open"
            inv_id = resp.object_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, title FROM investigations WHERE id = %s", (inv_id,))
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = '->open' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert row is not None and row["state"] == "open" and row["title"] == "kernel oops in xfs"
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_open_persists_and_dedups_external_refs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            refs = [
                {"tracker": "bz", "id": "42", "url": "https://bz/42"},
                {"tracker": "bz", "id": "42", "url": "https://bz/42-dup"},  # same (tracker,id)
                {"tracker": "jira", "id": "K-1", "url": "https://jira/K-1"},
            ]
            resp = await _open(pool, _ctx(), project="proj", title="t", external_refs=refs)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT external_refs FROM investigations WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
        assert row is not None
        stored = {(r["tracker"], r["id"]): r["url"] for r in row["external_refs"]}
        assert stored == {("bz", "42"): "https://bz/42-dup", ("jira", "K-1"): "https://jira/K-1"}

    asyncio.run(_run())


def test_open_malformed_external_ref_is_config_error_no_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            bad = [{"tracker": "bz", "id": "42"}]  # missing url
            resp = await _open(pool, _ctx(), project="proj", title="t", external_refs=bad)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM investigations")
                n = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert n is not None and n["n"] == 0

    asyncio.run(_run())


def test_open_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            with pytest.raises(AuthorizationError):
                await _open(pool, _ctx(Role.VIEWER), project="proj", title="t")

    asyncio.run(_run())


def test_get_own_investigation_renders_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.get_investigation(pool, _ctx(), opened.object_id)
        assert resp.status == "open"
        assert resp.data["external_refs"] == "0"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.get_investigation(pool, _ctx(projects=("other",)), opened.object_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await inv_tools.get_investigation(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_investigations_tools.py -q`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError` (the module/handlers do not exist).

- [ ] **Step 3: Create the module with `open` + `get`**

Create `src/kdive/mcp/tools/investigations.py`:

```python
"""The `investigations.*` MCP tools — the Investigation campaign surface (ADR-0026).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`open` mints an Investigation (`open`); `close` drives it to `closed`; `link`/`unlink`
mutate the `external_refs` jsonb under a per-Investigation advisory lock, keyed on the
`(tracker, id)` natural key (link upserts, unlink removes-if-present — both idempotent).
`get`/the mutators render through `_envelope_for_investigation` (every Investigation state
is a non-failure status, so no failure mapping is needed). RBAC: mutations require
`operator`; reads require project membership. Authz denials raise (ADR-0020: no authz
ErrorCategory).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ExternalRef, Investigation
from kdive.domain.state import IllegalTransition, InvestigationState
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

_TERMINAL_INVESTIGATION = frozenset(
    {InvestigationState.CLOSED, InvestigationState.ABANDONED}
)


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_investigation(inv: Investigation) -> ToolResponse:
    """Render an Investigation; every state is a non-failure status (ADR-0026 §6)."""
    if inv.state in _TERMINAL_INVESTIGATION:
        actions = ["investigations.get"]
    else:
        actions = ["investigations.get", "investigations.close", "runs.create"]
    return ToolResponse.success(
        str(inv.id),
        inv.state.value,
        suggested_next_actions=actions,
        data={"project": inv.project, "external_refs": str(len(inv.external_refs))},
    )


def _parse_external_refs(raw: list[dict[str, Any]] | None) -> list[ExternalRef]:
    """Parse + dedup external refs by the ``(tracker, id)`` natural key (last-wins).

    Raises:
        ValidationError / TypeError: A malformed entry or a non-list container.
    """
    if raw is None:
        return []
    by_key: dict[tuple[str, str], ExternalRef] = {}
    for entry in raw:
        ref = ExternalRef.model_validate(entry)
        by_key[(ref.tracker, ref.id)] = ref
    return list(by_key.values())


async def open_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    title: str,
    external_refs: list[dict[str, Any]] | None = None,
) -> ToolResponse:
    """Mint an Investigation (`open`) for the caller's project."""
    require_project(ctx, project)
    require_role(ctx, project, Role.OPERATOR)
    with bind_context(principal=ctx.principal):
        try:
            refs = _parse_external_refs(external_refs)
        except (ValidationError, TypeError):
            return _config_error(project)
        now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
        async with pool.connection() as conn, conn.transaction():
            inv = await INVESTIGATIONS.insert(
                conn,
                Investigation(
                    id=uuid4(),
                    created_at=now,
                    updated_at=now,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    project=project,
                    title=title,
                    external_refs=refs,
                    state=InvestigationState.OPEN,
                ),
            )
            await audit.record(
                conn,
                ctx,
                tool="investigations.open",
                object_kind="investigations",
                object_id=inv.id,
                transition="->open",
                args={"project": project, "title": title},
                project=project,
            )
        return ToolResponse.success(
            str(inv.id),
            "open",
            suggested_next_actions=["investigations.get", "runs.create"],
            data={"project": project},
        )


async def get_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Return an Investigation the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
        if inv is None or inv.project not in ctx.projects:
            return _config_error(investigation_id)
        return _envelope_for_investigation(inv)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `investigations.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="investigations.open")
    async def investigations_open(
        project: str, title: str, external_refs: list[dict[str, Any]] | None = None
    ) -> ToolResponse:
        return await open_investigation(
            pool, current_context(), project=project, title=title, external_refs=external_refs
        )

    @app.tool(name="investigations.get")
    async def investigations_get(investigation_id: str) -> ToolResponse:
        return await get_investigation(pool, current_context(), investigation_id)
```

> Note: `_get_for_update`, `close`, `link`, `unlink`, and the remaining `register` tool
> bindings are added in Tasks 3-4. `IllegalTransition`, `dict_row`, `Jsonb`,
> `AsyncConnection`, and `advisory_xact_lock` are imported now and used there; if `ty`/`ruff`
> flags them as unused at the end of this task, that is expected and resolved by Task 3-4 in
> the same branch — to keep the guardrail green at *this* commit, add the close/link/unlink
> handlers (Tasks 3-4) before committing, OR temporarily omit those four imports here and add
> them with their first use. Prefer the latter: import `IllegalTransition`/`dict_row`/`Jsonb`/
> `AsyncConnection` in Task 3's first step.

To keep this commit's guardrails green, **remove every import not yet used by `open`+`get`**
(`ruff` selects `F`, so any unused import fails the commit). For the Task 2 commit, delete:
- `from psycopg import AsyncConnection`
- `from psycopg.rows import dict_row`
- `from psycopg.types.json import Jsonb`
- `from kdive.db.locks import LockScope, advisory_xact_lock` (the **whole** line — neither
  is used until Task 3's `_close_locked`)
- `IllegalTransition` from the `kdive.domain.state` import (leave `InvestigationState`)

All of these return in Tasks 3-4. The exact import block that compiles clean for the Task 2
commit is therefore:

```python
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ExternalRef, Investigation
from kdive.domain.state import InvestigationState
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import Role, require_role
```

- [ ] **Step 4: Run tests + guardrails to verify pass**

Run: `uv run python -m pytest tests/mcp/test_investigations_tools.py -q && uv run ruff check && uv run ty check`
Expected: PASS; zero ruff/ty findings.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/investigations.py tests/mcp/test_investigations_tools.py
git commit -m "feat(investigations): add open + get tools (#17)"
```

---

## Task 3: `investigations.close`

**Files:**
- Modify: `src/kdive/mcp/tools/investigations.py`
- Test: `tests/mcp/test_investigations_tools.py`

- [ ] **Step 1: Write the failing tests**

First add the now-used import to the top of `tests/mcp/test_investigations_tools.py` (it was
deferred from Task 2 because nothing used it there — adding it now keeps `ruff` F401 clean):

```python
from kdive.domain.state import InvestigationState
```

Then append to `tests/mcp/test_investigations_tools.py`:

```python
async def _seed_investigation(pool: AsyncConnectionPool, state: InvestigationState) -> str:
    """Insert an Investigation directly in ``state`` (bypassing the open->… tools)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from kdive.db.repositories import INVESTIGATIONS
    from kdive.domain.models import Investigation

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=dt,
                updated_at=dt,
                principal="user-1",
                project="proj",
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


def test_close_open_investigation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
            assert resp.status == "closed"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM investigations WHERE id = %s", (inv_id,))
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->closed' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert row is not None and row["state"] == "closed"
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_close_active_investigation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.ACTIVE)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "closed"

    asyncio.run(_run())


def test_close_already_closed_is_idempotent_no_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.CLOSED)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
            assert resp.status == "closed"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE object_id = %s", (inv_id,)
                )
                audit = await cur.fetchone()
        assert audit is not None and audit["n"] == 0  # no transition audited

    asyncio.run(_run())


def test_close_abandoned_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.ABANDONED)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "abandoned"

    asyncio.run(_run())


def test_close_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            with pytest.raises(AuthorizationError):
                await inv_tools.close_investigation(pool, _ctx(Role.VIEWER), inv_id)

    asyncio.run(_run())


def test_close_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            resp = await inv_tools.close_investigation(pool, _ctx(projects=("other",)), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_close_backstop_maps_illegal_transition(monkeypatch: pytest.MonkeyPatch, migrated_url: str) -> None:
    # Force the IllegalTransition backstop: make update_state raise so the handler's
    # except-branch maps it to configuration_error rather than letting it escape.
    from kdive.db.repositories import INVESTIGATIONS
    from kdive.domain.state import IllegalTransition

    async def _boom(*_a: object, **_k: object) -> object:
        raise IllegalTransition("forced")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            monkeypatch.setattr(INVESTIGATIONS, "update_state", _boom)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_investigations_tools.py -k close -q`
Expected: FAIL with `AttributeError: close_investigation`.

- [ ] **Step 3: Implement `close` (and restore the imports)**

In `src/kdive/mcp/tools/investigations.py`, restore the imports removed in Task 2 Step 3
(`AsyncConnection`, `dict_row`, `Jsonb`, `IllegalTransition`) and add the helpers + handler.
Add after `get_investigation`:

```python
async def _get_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    """Read an Investigation row ``FOR UPDATE`` (held under the per-Investigation lock)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _close_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, *, project: str
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await INVESTIGATIONS.get(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state is InvestigationState.CLOSED:
            return ToolResponse.success(
                str(uid),
                "closed",
                suggested_next_actions=["investigations.get"],
                data={"project": project},
            )
        if current.state is InvestigationState.ABANDONED:
            return _config_error(str(uid), data={"current_status": "abandoned"})
        old = current.state
        await INVESTIGATIONS.update_state(conn, uid, InvestigationState.CLOSED)
        await audit.record(
            conn,
            ctx,
            tool="investigations.close",
            object_kind="investigations",
            object_id=uid,
            transition=f"{old.value}->closed",
            args={"investigation_id": str(uid)},
            project=project,
        )
    return ToolResponse.success(
        str(uid), "closed", suggested_next_actions=["investigations.get"], data={"project": project}
    )


async def close_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Drive an Investigation to `closed` (idempotent on an already-`closed` row)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            try:
                return await _close_locked(conn, ctx, uid, project=inv.project)
            except IllegalTransition:
                # Backstop for an interleaving the lock did not cover (e.g. a future
                # non-advisory writer). Caught OUTSIDE the rolled-back transaction; re-read.
                async with pool.connection() as conn2:
                    latest = await INVESTIGATIONS.get(conn2, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return _config_error(investigation_id, data=data)
```

Add the tool binding inside `register`:

```python
    @app.tool(name="investigations.close")
    async def investigations_close(investigation_id: str) -> ToolResponse:
        return await close_investigation(pool, current_context(), investigation_id)
```

- [ ] **Step 4: Run tests + guardrails**

Run: `uv run python -m pytest tests/mcp/test_investigations_tools.py -q && uv run ruff check && uv run ty check`
Expected: PASS; zero findings.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/investigations.py tests/mcp/test_investigations_tools.py
git commit -m "feat(investigations): add close tool with idempotent + backstop paths (#17)"
```

---

## Task 4: `investigations.link` + `unlink`

**Files:**
- Modify: `src/kdive/mcp/tools/investigations.py`
- Test: `tests/mcp/test_investigations_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_investigations_tools.py`:

```python
def _refs_of(pool: AsyncConnectionPool, inv_id: str):
    async def _q() -> dict[tuple[str, str], str]:
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT external_refs FROM investigations WHERE id = %s", (inv_id,))
            row = await cur.fetchone()
        assert row is not None
        return {(r["tracker"], r["id"]): r["url"] for r in row["external_refs"]}

    return _q


def test_link_then_unlink_round_trip(migrated_url: str) -> None:
    # The issue's first acceptance criterion: open -> link -> unlink mutates external_refs.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            ref = {"tracker": "bz", "id": "7", "url": "https://bz/7"}
            await inv_tools.link_external_ref(pool, _ctx(), inv_id, ref)
            after_link = await _refs_of(pool, inv_id)()
            await inv_tools.unlink_external_ref(pool, _ctx(), inv_id, ref)
            after_unlink = await _refs_of(pool, inv_id)()
        assert after_link == {("bz", "7"): "https://bz/7"}
        assert after_unlink == {}

    asyncio.run(_run())


def test_link_upserts_changed_url(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            await inv_tools.link_external_ref(pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7"})
            await inv_tools.link_external_ref(pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7-fixed"})
            refs = await _refs_of(pool, inv_id)()
        assert refs == {("bz", "7"): "https://bz/7-fixed"}  # one entry, url corrected

    asyncio.run(_run())


def test_unlink_by_natural_key_without_url(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            await inv_tools.link_external_ref(pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7"})
            # No url, and a differing url, both unlink the (bz,7) entry.
            await inv_tools.unlink_external_ref(pool, _ctx(), inv_id, {"tracker": "bz", "id": "7"})
            refs = await _refs_of(pool, inv_id)()
        assert refs == {}

    asyncio.run(_run())


def test_unlink_absent_is_idempotent_no_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            resp = await inv_tools.unlink_external_ref(pool, _ctx(), inv_id, {"tracker": "bz", "id": "nope"})
            assert resp.status == "open"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'unlink' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert audit is not None and audit["n"] == 0

    asyncio.run(_run())


def test_link_on_closed_investigation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.CLOSED)
            resp = await inv_tools.link_external_ref(pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7"})
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "closed"

    asyncio.run(_run())


def test_link_malformed_ref_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            resp = await inv_tools.link_external_ref(pool, _ctx(), inv_id, {"tracker": "bz"})  # no id/url
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_link_acquires_investigation_lock(migrated_url: str) -> None:
    # Deterministic lock proof: hold the INVESTIGATION advisory lock on a separate
    # connection; the link must block until it is released.
    import psycopg

    from kdive.db.locks import LockScope, advisory_xact_lock

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = (await _open(pool, _ctx(), project="proj", title="t")).object_id
            uid = UUID(inv_id)
            async with await psycopg.AsyncConnection.connect(migrated_url) as holder:
                async with holder.transaction(), advisory_xact_lock(holder, LockScope.INVESTIGATION, uid):
                    task = asyncio.create_task(
                        inv_tools.link_external_ref(
                            pool, _ctx(), inv_id, {"tracker": "bz", "id": "7", "url": "https://bz/7"}
                        )
                    )
                    await asyncio.sleep(0.3)
                    assert not task.done()  # blocked on the held INVESTIGATION lock
                # holder transaction committed here -> lock released
                resp = await task
            assert resp.status == "open"

    asyncio.run(_run())
```

Add the missing import at the top of the test file: `from uuid import UUID`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_investigations_tools.py -k "link or unlink" -q`
Expected: FAIL with `AttributeError: link_external_ref`.

- [ ] **Step 3: Implement `link` + `unlink`**

In `src/kdive/mcp/tools/investigations.py`, add after `close_investigation`:

```python
def _natural_key(ref: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(tracker, id)`` identity of a ref input; ``None`` if either is missing/blank."""
    tracker = ref.get("tracker")
    rid = ref.get("id")
    if not isinstance(tracker, str) or not tracker:
        return None
    if not isinstance(rid, str) or not rid:
        return None
    return (tracker, rid)


def _refs_jsonb(refs: list[ExternalRef]) -> Jsonb:
    return Jsonb([r.model_dump() for r in refs])


async def _link_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, ref: ExternalRef, *, project: str
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_for_update(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state in _TERMINAL_INVESTIGATION:
            return _config_error(str(uid), data={"current_status": current.state.value})
        kept = [r for r in current.external_refs if (r.tracker, r.id) != (ref.tracker, ref.id)]
        kept.append(ref)
        await conn.execute(
            "UPDATE investigations SET external_refs = %s WHERE id = %s", (_refs_jsonb(kept), uid)
        )
        await audit.record(
            conn,
            ctx,
            tool="investigations.link",
            object_kind="investigations",
            object_id=uid,
            transition="link",
            args={"tracker": ref.tracker, "id": ref.id},
            project=project,
        )
        updated = current.model_copy(update={"external_refs": kept})
    return _envelope_for_investigation(updated)


async def _unlink_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    key: tuple[str, str],
    *,
    project: str,
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_for_update(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state in _TERMINAL_INVESTIGATION:
            return _config_error(str(uid), data={"current_status": current.state.value})
        kept = [r for r in current.external_refs if (r.tracker, r.id) != key]
        if len(kept) != len(current.external_refs):
            await conn.execute(
                "UPDATE investigations SET external_refs = %s WHERE id = %s", (_refs_jsonb(kept), uid)
            )
            await audit.record(
                conn,
                ctx,
                tool="investigations.unlink",
                object_kind="investigations",
                object_id=uid,
                transition="unlink",
                args={"tracker": key[0], "id": key[1]},
                project=project,
            )
        updated = current.model_copy(update={"external_refs": kept})
    return _envelope_for_investigation(updated)


async def link_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: dict[str, Any]
) -> ToolResponse:
    """Upsert an external ref onto an Investigation (keyed on `(tracker, id)`)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    try:
        parsed = ExternalRef.model_validate(ref)
    except ValidationError:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            return await _link_locked(conn, ctx, uid, parsed, project=inv.project)


async def unlink_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: dict[str, Any]
) -> ToolResponse:
    """Remove an external ref by its `(tracker, id)` key (idempotent; `url` ignored)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    key = _natural_key(ref)
    if key is None:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            return await _unlink_locked(conn, ctx, uid, key, project=inv.project)
```

Add the two tool bindings inside `register`:

```python
    @app.tool(name="investigations.link")
    async def investigations_link(investigation_id: str, ref: dict[str, Any]) -> ToolResponse:
        return await link_external_ref(pool, current_context(), investigation_id, ref)

    @app.tool(name="investigations.unlink")
    async def investigations_unlink(investigation_id: str, ref: dict[str, Any]) -> ToolResponse:
        return await unlink_external_ref(pool, current_context(), investigation_id, ref)
```

- [ ] **Step 4: Run tests + guardrails**

Run: `uv run python -m pytest tests/mcp/test_investigations_tools.py -q && uv run ruff check && uv run ty check`
Expected: PASS; zero findings.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/investigations.py tests/mcp/test_investigations_tools.py
git commit -m "feat(investigations): add link/unlink external-ref tools (#17)"
```

---

## Task 5: `runs.py` — `get` + envelope

**Files:**
- Create: `src/kdive/mcp/tools/runs.py`
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Write the failing tests (and the runs seeding helpers)**

Create `tests/mcp/test_runs_tools.py`:

```python
"""runs.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import (
    Allocation,
    Investigation,
    Resource,
    ResourceKind,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import runs as runs_tools
from kdive.security.rbac import Role
```

> Note: `import pytest` and `AuthorizationError` are **deferred to Task 6** — Task 5's `get`
> tests use neither (`ruff` F401 would fail this commit). Task 6 adds them with its
> parametrized/raise tests.

```python

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROFILE: dict[str, Any] = {"kernel_source_ref": "git+https://git.kernel.org#v6.9"}


def _profile() -> dict[str, Any]:
    return copy.deepcopy(_PROFILE)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    system_state: SystemState = SystemState.READY,
    alloc_state: AllocationState = AllocationState.ACTIVE,
    project: str = "proj",
) -> str:
    """Insert a Resource + Allocation + System directly and return the system id."""
    async with pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=res.id,
                state=alloc_state,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=alloc.id,
                state=system_state,
                provisioning_profile={"schema_version": 1},
            ),
        )
    return str(system.id)


async def _seed_investigation(
    pool: AsyncConnectionPool,
    *,
    state: InvestigationState = InvestigationState.OPEN,
    project: str = "proj",
) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


async def _seed_run(pool: AsyncConnectionPool, *, state: RunState, failure: ErrorCategory | None = None) -> str:
    inv_id = await _seed_investigation(pool)
    sys_id = await _seed_system(pool)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=UUID(inv_id),
                system_id=UUID(sys_id),
                state=state,
                build_profile=_profile(),
                failure_category=failure,
            ),
        )
    return str(run.id)


def test_get_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await runs_tools.get_run(pool, _ctx(), run_id)
        assert resp.status == "created"
        assert resp.suggested_next_actions == ["runs.get", "runs.build"]

    asyncio.run(_run())


def test_get_failed_run_renders_failure_category(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE)
            resp = await runs_tools.get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "build_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_get_failed_run_null_category_defaults_infra(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.FAILED, failure=None)
            resp = await runs_tools.get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "infrastructure_failure"

    asyncio.run(_run())


def test_get_canceled_run_is_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CANCELED)
            resp = await runs_tools.get_run(pool, _ctx(), run_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await runs_tools.get_run(pool, _ctx(projects=("other",)), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await runs_tools.get_run(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.mcp.tools.runs`.

- [ ] **Step 3: Create `runs.py` with `get` + envelope**

Create `src/kdive/mcp/tools/runs.py`:

```python
"""The `runs.*` MCP tools — the Run join-point (ADR-0026).

`runs.create` binds a Run to a `ready` System (whose Allocation must be `active`, fixing
the Run's Allocation per the binding invariant) and an Investigation, and flips the
Investigation `open -> active` on its first Run — all in one transaction holding a
per-System then per-Investigation advisory lock (the global ALLOCATION→SYSTEM→
INVESTIGATION→RUN order). `runs.get` renders a Run; a `failed` Run maps to a failure
envelope carrying the Run's own `failure_category`. RBAC: `create` requires `operator`;
`get` requires project membership. Authz denials raise (ADR-0020: no authz ErrorCategory).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Investigation, Run
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

_RUN_HOSTABLE = frozenset({SystemState.READY})
_SYSTEM_GONE = frozenset(
    {SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED}
)
_ALLOC_HOSTABLE = frozenset({AllocationState.ACTIVE})
_INVESTIGATION_OPEN_FOR_RUN = frozenset(
    {InvestigationState.OPEN, InvestigationState.ACTIVE}
)


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _stale_handle(object_id: str, *, current_status: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.STALE_HANDLE, data={"current_status": current_status}
    )


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_run(run: Run) -> ToolResponse:
    """Render a Run; `failed` becomes a failure envelope carrying its `failure_category`."""
    if run.state is RunState.FAILED:
        category = run.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(
            str(run.id), category, data={"current_status": run.state.value}
        )
    if run.state in (RunState.CREATED, RunState.RUNNING):
        actions = ["runs.get", "runs.build"]
    else:
        actions = ["runs.get"]
    return ToolResponse.success(
        str(run.id),
        run.state.value,
        suggested_next_actions=actions,
        data={"project": run.project},
    )


async def get_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Return a Run the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
        if run is None or run.project not in ctx.projects:
            return _config_error(run_id)
        return _envelope_for_run(run)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="runs.get")
    async def runs_get(run_id: str) -> ToolResponse:
        return await get_run(pool, current_context(), run_id)
```

> **Keep-vs-defer for the Task 5 commit (`ruff` selects `F`, so any unused *import* fails;
> module-level constants/helpers defined ahead of use are NOT flagged).** Keep the four
> frozensets (`_RUN_HOSTABLE`, `_SYSTEM_GONE`, `_ALLOC_HOSTABLE`, `_INVESTIGATION_OPEN_FOR_RUN`)
> and `_stale_handle` defined as written — they are harmless ahead of use and, crucially, they
> keep the `SystemState`/`AllocationState`/`InvestigationState`/`ErrorCategory` imports *used*,
> so those imports stay. **Drop** only the imports nothing references until `create_run`
> (Task 6): `from psycopg import AsyncConnection`, `from psycopg.rows import dict_row`,
> `from kdive.db.locks import LockScope, advisory_xact_lock`, `datetime, UTC` (the
> `datetime` import line), `uuid4` (leave `UUID`), `from kdive.db.repositories import` — narrow
> to **`RUNS`** only (drop `ALLOCATIONS, INVESTIGATIONS, SYSTEMS`), `Investigation` from the
> models import (leave `Run`), `audit`, `require_role` + `Role`, and `Any` from `typing`
> (unused until `create_run`'s `dict[str, Any]`). They all return in Task 6 Step 3. The exact
> import block that compiles clean for the Task 5 commit is:
>
> ```python
> import logging
> from uuid import UUID
>
> from fastmcp import FastMCP
> from psycopg_pool import AsyncConnectionPool
>
> from kdive.db.repositories import RUNS
> from kdive.domain.errors import ErrorCategory
> from kdive.domain.models import Run
> from kdive.domain.state import (
>     AllocationState,
>     InvestigationState,
>     RunState,
>     SystemState,
> )
> from kdive.log import bind_context
> from kdive.mcp.auth import RequestContext, current_context
> from kdive.mcp.responses import ToolResponse
> ```

- [ ] **Step 4: Run tests + guardrails**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q && uv run ruff check && uv run ty check`
Expected: PASS; zero findings.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): add runs.get tool and run envelope (#17)"
```

---

## Task 6: `runs.create`

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py`
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Write the failing tests**

First add the two imports deferred from Task 5 to the top of `tests/mcp/test_runs_tools.py`
(now used by the parametrized + raise tests below; this keeps `ruff` F401 clean): add
`import pytest` (in the third-party import group) and change
`from kdive.security.rbac import Role` to `from kdive.security.rbac import AuthorizationError, Role`.

Then append to `tests/mcp/test_runs_tools.py`:

```python
async def _create(pool: AsyncConnectionPool, ctx: RequestContext, inv_id: str, sys_id: str, profile=None):
    return await runs_tools.create_run(
        pool, ctx, investigation_id=inv_id, system_id=sys_id, build_profile=profile or _profile()
    )


def test_create_first_run_flips_investigation_active(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
            assert resp.status == "created"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (resp.object_id,))
                run_row = await cur.fetchone()
                await cur.execute(
                    "SELECT state, last_run_at FROM investigations WHERE id = %s", (inv_id,)
                )
                inv_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->active' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                flip = await cur.fetchone()
        assert run_row is not None and run_row["state"] == "created"
        assert inv_row is not None and inv_row["state"] == "active"
        assert inv_row["last_run_at"] is not None
        assert flip is not None and flip["n"] == 1

    asyncio.run(_run())


def test_create_second_run_no_second_flip(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_a = await _seed_system(pool)
            sys_b = await _seed_system(pool)
            await _create(pool, _ctx(), inv_id, sys_a)
            resp = await _create(pool, _ctx(), inv_id, sys_b)
            assert resp.status == "created"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->active' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                flip = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM runs WHERE investigation_id = %s", (inv_id,))
                runs = await cur.fetchone()
        assert flip is not None and flip["n"] == 1  # flipped exactly once
        assert runs is not None and runs["n"] == 2

    asyncio.run(_run())


@pytest.mark.parametrize("state", [SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED])
def test_create_on_gone_system_is_stale_handle(migrated_url: str, state: SystemState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, system_state=state)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


@pytest.mark.parametrize("state", [SystemState.DEFINED, SystemState.PROVISIONING])
def test_create_on_not_ready_system_is_config_error(migrated_url: str, state: SystemState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, system_state=state)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


def test_create_with_non_active_allocation_is_stale_handle(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            # System ready, but its Allocation is released (the orphaned-System window).
            sys_id = await _seed_system(pool, alloc_state=AllocationState.RELEASED)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"
        assert resp.data["current_status"] == "released"

    asyncio.run(_run())


@pytest.mark.parametrize("state", [InvestigationState.CLOSED, InvestigationState.ABANDONED])
def test_create_on_terminal_investigation_is_config_error(migrated_url: str, state: InvestigationState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=state)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


def test_create_cross_project_join_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, project="proj")
            sys_id = await _seed_system(pool, project="proj")
            # Investigation seeded in another project for the same caller (multi-project ctx).
            other_inv = await _seed_investigation(pool, project="proj")
            async with pool.connection() as conn:
                await conn.execute("UPDATE investigations SET project = 'p2' WHERE id = %s", (other_inv,))
            ctx = RequestContext(
                principal="user-1", agent_session="s", projects=("proj", "p2"),
                roles={"proj": Role.OPERATOR, "p2": Role.OPERATOR},
            )
            resp = await runs_tools.create_run(
                pool, ctx, investigation_id=other_inv, system_id=sys_id, build_profile=_profile()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_create_non_dict_build_profile_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            # Bind the deliberately-wrong value to an Any-typed local so `ty` (whose
            # whole-tree check covers tests) does not flag the str->dict argument. Do NOT
            # use a mypy-style `# type: ignore[arg-type]` — ty's directive is
            # `# ty: ignore[invalid-argument-type]`, and avoiding the error entirely is cleaner.
            bad: Any = "nope"
            resp = await runs_tools.create_run(
                pool, _ctx(), investigation_id=inv_id, system_id=sys_id, build_profile=bad
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM runs")
                n = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert n is not None and n["n"] == 0

    asyncio.run(_run())


def test_create_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            with pytest.raises(AuthorizationError):
                await _create(pool, _ctx(Role.VIEWER), inv_id, sys_id)

    asyncio.run(_run())


def test_create_missing_investigation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), str(uuid4()), sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_create_concurrent_first_runs_flip_once(migrated_url: str) -> None:
    # Two first-Runs on one open Investigation (distinct ready Systems) -> both created,
    # exactly one open->active audit row (the per-Investigation lock makes the flip
    # exactly-once; distinct Systems keep the System locks from serializing the test).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_a = await _seed_system(pool)
            sys_b = await _seed_system(pool)
            r1, r2 = await asyncio.gather(
                _create(pool, _ctx(), inv_id, sys_a),
                _create(pool, _ctx(), inv_id, sys_b),
            )
            assert {r1.status, r2.status} == {"created"}
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->active' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                flip = await cur.fetchone()
        assert flip is not None and flip["n"] == 1

    asyncio.run(_run())


def test_create_blocks_on_held_investigation_lock(migrated_url: str) -> None:
    # Deterministic proof create_run takes the INVESTIGATION lock: hold it externally;
    # create_run acquires SYSTEM, then blocks on INVESTIGATION until release.
    import psycopg

    from kdive.db.locks import LockScope, advisory_xact_lock

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            async with await psycopg.AsyncConnection.connect(migrated_url) as holder:
                async with holder.transaction(), advisory_xact_lock(holder, LockScope.INVESTIGATION, UUID(inv_id)):
                    task = asyncio.create_task(_create(pool, _ctx(), inv_id, sys_id))
                    await asyncio.sleep(0.3)
                    assert not task.done()  # blocked on the held INVESTIGATION lock
                resp = await task
            assert resp.status == "created"

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -k create -q`
Expected: FAIL with `AttributeError: create_run`.

- [ ] **Step 3: Implement `create_run` (restore the Task-5-deferred imports)**

In `src/kdive/mcp/tools/runs.py`, restore **only the imports** deferred in Task 5 — add back
`from psycopg import AsyncConnection`, `from psycopg.rows import dict_row`,
`from kdive.db.locks import LockScope, advisory_xact_lock`, `from datetime import UTC, datetime`,
`uuid4` (alongside `UUID`), `Any` (in `from typing import Any`), `ALLOCATIONS, INVESTIGATIONS,
SYSTEMS` (widen the `kdive.db.repositories` import), `Investigation` (alongside `Run`),
`from kdive.security import audit`, and `require_role`/`Role` (`from kdive.security.rbac import
Role, require_role`). The four frozensets and `_stale_handle` already exist from Task 5 — do
**not** redefine them. Add after `get_run`:

```python
async def _investigation_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    inv_uid: UUID,
    sys_uid: UUID,
    build_profile: dict[str, Any],
    *,
    project: str,
) -> ToolResponse:
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, sys_uid),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, inv_uid),
    ):
        system = await SYSTEMS.get(conn, sys_uid)
        if system is None:
            return _config_error(str(sys_uid))
        if system.state in _SYSTEM_GONE:
            return _stale_handle(str(sys_uid), current_status=system.state.value)
        if system.state not in _RUN_HOSTABLE:
            return _config_error(str(sys_uid), data={"current_status": system.state.value})
        inv = await _investigation_for_update(conn, inv_uid)
        if inv is None:
            return _config_error(str(inv_uid))
        if inv.state not in _INVESTIGATION_OPEN_FOR_RUN:
            return _config_error(str(inv_uid), data={"current_status": inv.state.value})
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=project,
                investigation_id=inv_uid,
                system_id=sys_uid,
                state=RunState.CREATED,
                build_profile=build_profile,
            ),
        )
        await audit.record(
            conn,
            ctx,
            tool="runs.create",
            object_kind="runs",
            object_id=run.id,
            transition="->created",
            args={"investigation_id": str(inv_uid), "system_id": str(sys_uid)},
            project=project,
        )
        if inv.state is InvestigationState.OPEN:
            await INVESTIGATIONS.update_state(conn, inv_uid, InvestigationState.ACTIVE)
            await audit.record(
                conn,
                ctx,
                tool="runs.create",
                object_kind="investigations",
                object_id=inv_uid,
                transition="open->active",
                args={"investigation_id": str(inv_uid)},
                project=project,
            )
        await conn.execute(
            "UPDATE investigations SET last_run_at = now() WHERE id = %s", (inv_uid,)
        )
    return ToolResponse.success(
        str(run.id),
        "created",
        suggested_next_actions=["runs.get", "runs.build"],
        data={
            "project": project,
            "investigation_id": str(inv_uid),
            "system_id": str(sys_uid),
        },
    )


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    investigation_id: str,
    system_id: str,
    build_profile: dict[str, Any],
) -> ToolResponse:
    """Bind a Run to a `ready` System + an Investigation; flip `open -> active` on the first Run."""
    inv_uid = _as_uuid(investigation_id)
    if inv_uid is None:
        return _config_error(investigation_id)
    sys_uid = _as_uuid(system_id)
    if sys_uid is None:
        return _config_error(system_id)
    if not isinstance(build_profile, dict):
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, inv_uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            system = await SYSTEMS.get(conn, sys_uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            if system.project != inv.project:
                return _config_error(system_id)
            alloc = await ALLOCATIONS.get(conn, system.allocation_id)
            if alloc is None or alloc.state not in _ALLOC_HOSTABLE:
                current = alloc.state.value if alloc is not None else "missing"
                return _stale_handle(system_id, current_status=current)
            return await _create_locked(
                conn, ctx, inv_uid, sys_uid, build_profile, project=inv.project
            )
```

Add the `runs.create` binding inside `register`:

```python
    @app.tool(name="runs.create")
    async def runs_create(
        investigation_id: str, system_id: str, build_profile: dict[str, Any]
    ) -> ToolResponse:
        return await create_run(
            pool,
            current_context(),
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=build_profile,
        )
```

- [ ] **Step 4: Run tests + guardrails**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q && uv run ruff check && uv run ty check`
Expected: PASS (all runs tests incl. the parametrized + concurrency cases); zero findings.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): add runs.create with binding invariant + first-Run activation (#17)"
```

---

## Task 7: Register the planes + final verification

**Files:**
- Modify: `src/kdive/mcp/app.py:21-29`
- Test: `tests/mcp/test_app.py`

- [ ] **Step 1: Write the failing test**

In `tests/mcp/test_app.py`, extend `test_build_app_registers_jobs_tools`'s `_run` body
assertions (add after the existing `systems.*` assertion):

```python
        assert {
            "investigations.open",
            "investigations.get",
            "investigations.close",
            "investigations.link",
            "investigations.unlink",
        } <= names
        assert {"runs.create", "runs.get"} <= names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_app.py::test_build_app_registers_jobs_tools -v`
Expected: FAIL (the new tool names are absent from the registered set).

- [ ] **Step 3: Wire the registrars**

In `src/kdive/mcp/app.py`, update the import and the `_PLANE_REGISTRARS` tuple:

```python
from kdive.mcp.tools import allocations, investigations, jobs, resources, runs, systems

# Tool seam: each plane exposes register(app, pool); build_app calls them all.
_PLANE_REGISTRARS: tuple[Callable[[FastMCP, AsyncConnectionPool], None], ...] = (
    jobs.register,
    resources.register,
    allocations.register,
    systems.register,
    investigations.register,
    runs.register,
)
```

- [ ] **Step 4: Run test + guardrails**

Run: `uv run python -m pytest tests/mcp/test_app.py -q && uv run ruff check && uv run ty check`
Expected: PASS; zero findings.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/app.py tests/mcp/test_app.py
git commit -m "feat(mcp): register investigations.* and runs.* planes (#17)"
```

- [ ] **Step 6: Full-suite + format gate**

Run:
```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run ty check
uv run python -m pytest -q
```
Expected: all green, zero warnings. If `ruff format --check` reports a diff, run `uv run ruff format src tests`, re-run the suite, and amend the relevant commit. The env-gated libvirt/gdb/drgn integration tests remain skipped (expected).

---

## Self-Review notes (spec coverage)

- **Acceptance 1 (open→link→unlink mutate `external_refs`):** Task 4 `test_link_then_unlink_round_trip`.
- **Acceptance 2 (first Run flips `open→active`):** Task 6 `test_create_first_run_flips_investigation_active` + `test_create_second_run_no_second_flip`.
- **Acceptance 3 (Run on torn-down System → `stale_handle`):** Task 6 `test_create_on_gone_system_is_stale_handle[torn_down]`.
- **Binding invariant (`run.system → allocation`):** Task 6 `test_create_with_non_active_allocation_is_stale_handle` (allowlist) + the System-state checks; structural via the schema (no `allocation_id` on `runs`).
- **Lock scope + order:** Task 1 (`INVESTIGATION` member + docstring) and Task 6 `test_create_blocks_on_held_investigation_lock`.
- **Envelope/`failed` rendering + `build_profile` opaque:** Task 5 (`_envelope_for_run`, profile is a plain dict).
- **Registration:** Task 7.
- **Non-idempotency / no `runs.cancel` / no reconciler:** recorded non-goals; no task (correctly absent).
