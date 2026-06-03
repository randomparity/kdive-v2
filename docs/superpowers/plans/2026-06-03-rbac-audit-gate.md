# RBAC, Audit Log & Destructive-Op Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the three M0 security primitives — project-scoped RBAC, append-only audit records, and the three-check destructive-op gate — that every later plane tool composes.

**Architecture:** Three new pure/DB modules under `src/kdive/security/` plus a minimal `roles` field on the existing `RequestContext`. `rbac.py` owns the `Role` vocabulary and `require_role`; `audit.py` writes one `audit_log` row per call inside the caller's transaction with a one-way `args_digest`; `gate.py` composes the three destructive checks fail-closed. The `auth ↔ rbac` cycle is broken by keeping `rbac`'s module-level dependency on `auth` type-only (one function-level `AuthError` import).

**Tech Stack:** Python 3.13, psycopg (async), Pydantic, `uv`/`ruff`/`ty`/`pytest`, testcontainers Postgres.

**Spec:** [`docs/superpowers/specs/2026-06-03-rbac-audit-gate-design.md`](../specs/2026-06-03-rbac-audit-gate-design.md) · **Decisions:** [ADR-0006](../../adr/0006-oidc-rbac-attribution.md), [ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md)

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/kdive/security/rbac.py` (create) | `Role` enum + rank, `AuthorizationError`, `roles_from_claims`, `require_role` |
| `src/kdive/security/audit.py` (create) | `args_digest`, `record` (one append-only `audit_log` row, caller's transaction) |
| `src/kdive/security/gate.py` (create) | `DestructiveOp`, `DestructiveOpDenied`, `assert_destructive_allowed` |
| `src/kdive/mcp/auth.py` (modify) | add `roles: Mapping[str, Role]` to `RequestContext`; populate it in `context_from_claims` |
| `tests/security/__init__.py` (create) | package marker |
| `tests/security/conftest.py` (create) | re-export the Postgres fixtures (existing idiom) |
| `tests/security/test_rbac.py` (create) | `roles_from_claims`, `require_role`, hashability |
| `tests/security/test_audit.py` (create) | `args_digest`, `record`, transition atomicity, project guard |
| `tests/security/test_gate.py` (create) | the three-check gate, including the three single-factor denials |

Guardrails after every task: `uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest -q`.

---

## Task 1: Test package + fixture re-export

**Files:**
- Create: `tests/security/__init__.py`
- Create: `tests/security/conftest.py`

- [ ] **Step 1: Create the package marker**

`tests/security/__init__.py`:

```python
```

(empty file — mirrors `tests/db/__init__.py`)

- [ ] **Step 2: Re-export the Postgres fixtures (existing idiom)**

`tests/security/conftest.py` — identical pattern to `tests/jobs/conftest.py`:

```python
"""Shared fixtures for the security tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so the audit
suite runs against the same per-test migrated schema (testcontainers Postgres).
"""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]
```

- [ ] **Step 3: Verify collection succeeds**

Run: `uv run python -m pytest tests/security -q`
Expected: `no tests ran` (collection succeeds, zero tests yet — not an error).

- [ ] **Step 4: Commit**

```bash
git add tests/security/__init__.py tests/security/conftest.py
git commit -m "test(security): add tests/security package + fixture re-export"
```

---

## Task 2: `rbac.py` — roles, claim parsing, enforcement

**Files:**
- Create: `src/kdive/security/rbac.py`
- Test: `tests/security/test_rbac.py`

- [ ] **Step 1: Write the failing tests**

`tests/security/test_rbac.py`:

```python
"""Tests for project-scoped RBAC (ADR-0006, ADR-0020)."""

from __future__ import annotations

import pytest

from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.rbac import AuthorizationError, Role, require_role, roles_from_claims


def _ctx(*, projects: tuple[str, ...] = ("proj",), roles: dict[str, Role] | None = None) -> RequestContext:
    return RequestContext(
        principal="alice",
        agent_session=None,
        projects=projects,
        roles=roles or {},
    )


def test_roles_from_claims_absent_is_empty() -> None:
    assert roles_from_claims({"sub": "alice"}) == {}


def test_roles_from_claims_parses_map() -> None:
    assert roles_from_claims({"roles": {"a": "admin", "b": "operator"}}) == {
        "a": Role.ADMIN,
        "b": Role.OPERATOR,
    }


def test_roles_from_claims_rejects_non_object() -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": ["admin"]})


def test_roles_from_claims_rejects_unknown_role() -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": {"a": "superadmin"}})


def test_roles_from_claims_rejects_non_string_value() -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": {"a": 1}})


def test_require_role_admin_satisfies_operator() -> None:
    require_role(_ctx(roles={"proj": Role.ADMIN}), "proj", Role.OPERATOR)


def test_require_role_exact_match_ok() -> None:
    require_role(_ctx(roles={"proj": Role.OPERATOR}), "proj", Role.OPERATOR)


def test_require_role_too_low_denied() -> None:
    with pytest.raises(AuthorizationError):
        require_role(_ctx(roles={"proj": Role.OPERATOR}), "proj", Role.ADMIN)


def test_require_role_not_a_member_denied() -> None:
    with pytest.raises(AuthorizationError):
        require_role(_ctx(projects=("other",), roles={"proj": Role.ADMIN}), "proj", Role.VIEWER)


def test_require_role_member_without_role_denied_not_keyerror() -> None:
    # The common token shape: granted membership, no per-project role.
    with pytest.raises(AuthorizationError):
        require_role(_ctx(projects=("proj",), roles={}), "proj", Role.VIEWER)


def test_request_context_with_roles_is_hashable() -> None:
    ctx = _ctx(roles={"proj": Role.ADMIN})
    assert hash(ctx) == hash(ctx)  # does not raise
    assert ctx.roles["proj"] is Role.ADMIN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/security/test_rbac.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.security.rbac'` (and `RequestContext` has no `roles` field yet; that field is added in Task 3, so `_ctx` will also fail — acceptable, both go green together once Tasks 2+3 land. If you prefer a fully-green Task 2, run Task 3's `auth.py` edit first; the order is interchangeable but commit them separately).

- [ ] **Step 3: Write `rbac.py`**

`src/kdive/security/rbac.py`:

```python
"""Project-scoped RBAC: roles, claim parsing, and enforcement (ADR-0006, ADR-0020).

The three M0 roles form a total rank, so a higher role satisfies a lower requirement.
`roles_from_claims` turns a verified token's `roles` claim into the per-project role
map carried on `RequestContext`; `require_role` is the enforcement point every plane
tool calls before a privileged operation. A denial raises `AuthorizationError`
(distinct from `kdive.mcp.auth.AuthError`, which covers authentication/membership), so
a handler maps "you may not do this" separately from "who are you".
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kdive.mcp.auth import RequestContext

_ROLES_CLAIM = "roles"


class Role(StrEnum):
    """The three project-scoped M0 roles, ordered viewer < operator < admin."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


class AuthorizationError(Exception):
    """A verified, authenticated principal may not perform the requested operation.

    Distinct from `kdive.mcp.auth.AuthError` (no subject / project not granted): the
    caller is known and a project member, but lacks the role the operation needs.
    """


def roles_from_claims(claims: Mapping[str, object]) -> dict[str, Role]:
    """Parse the per-project role map from a verified token's ``roles`` claim.

    The claim is a JSON object mapping a project name to one role string
    (``{"proj-a": "admin"}``). An absent claim yields ``{}`` (membership without a
    role).

    Raises:
        AuthError: The claim is present but not an object, or a value is not a string
            or not a known role (fail closed — never silently drop or upgrade a grant).
    """
    raw = claims.get(_ROLES_CLAIM)
    if raw is None:
        return {}
    # Function-level import: the only runtime rbac->auth edge, kept here so rbac's
    # module-level dependency on auth stays type-only and the import cycle is broken.
    from kdive.mcp.auth import AuthError

    if not isinstance(raw, Mapping):
        raise AuthError("roles claim is not an object")
    roles: dict[str, Role] = {}
    for project, value in raw.items():
        if not isinstance(value, str):
            raise AuthError(f"roles claim value for project {project!r} is not a string")
        try:
            role = Role(value)
        except ValueError:
            raise AuthError(
                f"roles claim has unknown role {value!r} for project {project!r}"
            ) from None
        roles[str(project)] = role
    return roles


def require_role(ctx: RequestContext, project: str, role: Role) -> None:
    """Enforce that ``ctx`` holds at least ``role`` on ``project``.

    Raises:
        AuthorizationError: ``project`` is not granted to the principal, the principal
            carries no role on it, or the held role ranks below ``role``.
    """
    if project not in ctx.projects:
        raise AuthorizationError(f"{ctx.principal!r} is not a member of project {project!r}")
    held = ctx.roles.get(project)
    if held is None or _RANK[held] < _RANK[role]:
        held_name = held.value if held is not None else "none"
        raise AuthorizationError(
            f"{ctx.principal!r} needs role {role.value!r} on project {project!r}; "
            f"holds {held_name!r}"
        )
```

- [ ] **Step 4: Run tests to verify they pass (after Task 3's `auth.py` edit is also in place)**

Run: `uv run python -m pytest tests/security/test_rbac.py -q`
Expected: PASS (10 tests). If `RequestContext` has no `roles` field yet, do Task 3 Step 3 first.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest tests/security/test_rbac.py -q
git add src/kdive/security/rbac.py tests/security/test_rbac.py
git commit -m "feat(security): project-scoped roles, claim parsing, require_role"
```

---

## Task 3: Thread `roles` through `RequestContext`

**Files:**
- Modify: `src/kdive/mcp/auth.py`
- Test: `tests/mcp/test_auth.py` (add cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_auth.py`:

```python
def test_context_from_claims_parses_roles() -> None:
    from kdive.security.rbac import Role

    ctx = context_from_claims(
        {"sub": "alice", "projects": ["a"], "roles": {"a": "admin"}}
    )
    assert ctx.roles == {"a": Role.ADMIN}


def test_context_from_claims_absent_roles_is_empty() -> None:
    ctx = context_from_claims({"sub": "alice", "projects": ["a"]})
    assert ctx.roles == {}
```

(Check the existing imports at the top of `tests/mcp/test_auth.py` already include `context_from_claims`; it is exercised by existing tests, so no new import is needed.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/test_auth.py -k roles -q`
Expected: FAIL — `AttributeError: 'RequestContext' object has no attribute 'roles'`.

- [ ] **Step 3: Edit `RequestContext` and `context_from_claims`**

In `src/kdive/mcp/auth.py`:

Change the imports block — add `field` and the rbac runtime import:

```python
from dataclasses import dataclass, field
```

and after the existing imports add:

```python
from kdive.security.rbac import Role, roles_from_claims
```

Add the `roles` field (note `compare=False` keeps the frozen dataclass hashable despite the `dict`):

```python
@dataclass(frozen=True)
class RequestContext:
    """The `(principal, agent_session, project)` attribution tuple (ADR-0006)."""

    principal: str
    agent_session: str | None
    projects: tuple[str, ...]
    roles: Mapping[str, Role] = field(default_factory=dict, compare=False)
```

In `context_from_claims`, populate `roles` on the returned context:

```python
    return RequestContext(
        principal=subject,
        agent_session=agent_session,
        projects=projects,
        roles=roles_from_claims(claims),
    )
```

- [ ] **Step 4: Run to verify pass (auth + rbac suites)**

Run: `uv run python -m pytest tests/mcp/test_auth.py tests/security/test_rbac.py -q`
Expected: PASS. (`compare=False` means existing equality assertions on `RequestContext` are unaffected by roles.)

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest tests/mcp tests/security/test_rbac.py -q
git add src/kdive/mcp/auth.py tests/mcp/test_auth.py
git commit -m "feat(security): carry per-project roles on RequestContext"
```

---

## Task 4: `audit.py` — `args_digest` + `record`

**Files:**
- Create: `src/kdive/security/audit.py`
- Test: `tests/security/test_audit.py`

- [ ] **Step 1: Write the failing tests**

`tests/security/test_audit.py`:

```python
"""Tests for the append-only audit record (ADR-0006, ADR-0020)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.audit import args_digest, record

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> RequestContext:
    return RequestContext(principal="alice", agent_session="sess-1", projects=("proj",))


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _seed_allocation(conn: psycopg.AsyncConnection) -> Allocation:
    res = await RESOURCES.insert(
        conn,
        Resource.model_validate(
            dict(
                id=uuid4(), created_at=_DT, updated_at=_DT, kind=ResourceKind.LOCAL_LIBVIRT,
                pool="p", cost_class="c", status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            )
        ),
    )
    return await ALLOCATIONS.insert(
        conn,
        Allocation.model_validate(
            dict(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
                resource_id=res.id, state=AllocationState.REQUESTED,
            )
        ),
    )


async def _count_audit(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_args_digest_is_order_independent() -> None:
    assert args_digest({"a": 1, "b": {"x": 1, "y": 2}}) == args_digest(
        {"b": {"y": 2, "x": 1}, "a": 1}
    )


def test_args_digest_differs_for_different_args() -> None:
    assert args_digest({"a": 1}) != args_digest({"a": 2})


def test_args_digest_does_not_contain_secret() -> None:
    secret = "hunter2-supersecret"
    assert secret not in args_digest({"password": secret})


def test_args_digest_handles_uuid_and_datetime() -> None:
    args = {"id": uuid4(), "when": _DT}
    assert args_digest(args) == args_digest(dict(args))  # deterministic over scalars


def test_record_writes_one_row(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            obj_id = uuid4()
            audit_id = await record(
                conn, _ctx(), tool="systems.teardown", object_kind="systems",
                object_id=obj_id, transition="ready->torn_down",
                args={"system_id": str(obj_id)}, project="proj",
            )
            assert isinstance(audit_id, UUID)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session, project, tool, object_kind, "
                    "object_id, transition, args_digest FROM audit_log WHERE id = %s",
                    (audit_id,),
                )
                row = await cur.fetchone()
            assert row == (
                "alice", "sess-1", "proj", "systems.teardown", "systems", obj_id,
                "ready->torn_down", args_digest({"system_id": str(obj_id)}),
            )
            assert await _count_audit(conn) == 1

    asyncio.run(_run_test())


def test_record_rejects_ungranted_project(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(AuthError):
                await record(
                    conn, _ctx(), tool="systems.teardown", object_kind="systems",
                    object_id=uuid4(), transition="ready->torn_down", args={},
                    project="not-granted",
                )
            assert await _count_audit(conn) == 0

    asyncio.run(_run_test())


def test_record_in_transition_transaction_is_atomic(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            alloc = await _seed_allocation(conn)
            async with conn.transaction():
                await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)
                await record(
                    conn, _ctx(), tool="allocations.grant", object_kind="allocations",
                    object_id=alloc.id, transition="requested->granted", args={},
                    project="proj",
                )
            assert await _count_audit(conn) == 1  # exactly one row per transition
            updated = await ALLOCATIONS.get(conn, alloc.id)
            assert updated is not None and updated.state is AllocationState.GRANTED

    asyncio.run(_run_test())


def test_record_rolls_back_with_failed_transition(migrated_url: str) -> None:
    class _Boom(RuntimeError):
        pass

    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            alloc = await _seed_allocation(conn)
            with pytest.raises(_Boom):
                async with conn.transaction():
                    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)
                    await record(
                        conn, _ctx(), tool="allocations.grant", object_kind="allocations",
                        object_id=alloc.id, transition="requested->granted", args={},
                        project="proj",
                    )
                    raise _Boom  # abort the whole transaction
            assert await _count_audit(conn) == 0  # audit row rolled back with the transition
            still = await ALLOCATIONS.get(conn, alloc.id)
            assert still is not None and still.state is AllocationState.REQUESTED

    asyncio.run(_run_test())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/security/test_audit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.security.audit'`.

- [ ] **Step 3: Write `audit.py`**

`src/kdive/security/audit.py`:

```python
"""Append-only audit records for state transitions (ADR-0006, ADR-0020).

`record` writes exactly one `audit_log` row inside the caller's transaction, so a
state transition and its audit entry commit atomically. `args_digest` stores a
one-way SHA-256 of the tool arguments — never the raw values — so secret-bearing
arguments cannot leak as plaintext (confidentiality of low-entropy secret *values* is
ADR-0012's secrets-by-reference contract; the digest is tamper-evidence/correlation).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from kdive.mcp.auth import AuthError

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from kdive.mcp.auth import RequestContext


def args_digest(args: Mapping[str, object]) -> str:
    """Return the SHA-256 hex of a canonical JSON encoding of ``args``.

    ``args`` are JSON-native values (MCP tool arguments) plus the scalar ``UUID`` /
    ``datetime`` the codebase carries, which ``default=str`` renders deterministically.
    The digest is one-way: no plaintext argument is stored.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def record(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    tool: str,
    object_kind: str,
    object_id: UUID,
    transition: str,
    args: Mapping[str, object],
    project: str,
) -> UUID:
    """Append one `audit_log` row for a transition; return its id.

    Runs the INSERT on ``conn`` without opening a transaction, so the caller composes
    it with the audited state transition in one ``conn.transaction()`` (both commit or
    neither does). ``project`` is the audited object's project, not ``ctx.projects``
    (the granted set).

    Raises:
        AuthError: ``project`` is not in ``ctx.projects`` — a misattribution guard on
            the append-only row.
    """
    if project not in ctx.projects:
        raise AuthError(
            f"cannot audit under project {project!r} not granted to {ctx.principal!r}"
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO audit_log "
            "(principal, agent_session, project, tool, object_kind, object_id, "
            " transition, args_digest) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                ctx.principal,
                ctx.agent_session,
                project,
                tool,
                object_kind,
                object_id,
                transition,
                args_digest(args),
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into audit_log returned no row")
    return row[0]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/security/test_audit.py -q`
Expected: PASS (8 tests). If Docker/testcontainers is unavailable locally the DB-backed tests skip — run with `KDIVE_REQUIRE_DOCKER=1` to force them in CI.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest tests/security/test_audit.py -q
git add src/kdive/security/audit.py tests/security/test_audit.py
git commit -m "feat(security): append-only audit record with hashed args_digest"
```

---

## Task 5: `gate.py` — the three-check destructive gate

**Files:**
- Create: `src/kdive/security/gate.py`
- Test: `tests/security/test_gate.py`

- [ ] **Step 1: Write the failing tests**

`tests/security/test_gate.py`:

```python
"""Tests for the three-check destructive-op gate (ADR-0006, ADR-0020)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.security.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.rbac import Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(role: Role = Role.ADMIN) -> RequestContext:
    return RequestContext(
        principal="alice", agent_session=None, projects=("proj",), roles={"proj": role}
    )


def _allocation(scope: dict[str, Any]) -> Allocation:
    return Allocation.model_validate(
        dict(
            id=uuid4(), created_at=_DT, updated_at=_DT, principal="alice", project="proj",
            resource_id=uuid4(), state=AllocationState.ACTIVE, capability_scope=scope,
        )
    )


def _op(opt_in: bool = True) -> DestructiveOp:
    return DestructiveOp(kind="force_crash", profile_opt_in=opt_in)


def test_all_three_present_is_allowed() -> None:
    assert (
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation({"destructive_ops": ["force_crash"]}), _op(True)
        )
        is None
    )


def test_scope_absent_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.ADMIN), _allocation({}), _op(True))
    assert exc.value.missing == ["capability_scope"]


def test_not_admin_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.OPERATOR), _allocation({"destructive_ops": ["force_crash"]}), _op(True)
        )
    assert exc.value.missing == ["admin_role"]


def test_opt_in_false_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation({"destructive_ops": ["force_crash"]}), _op(False)
        )
    assert exc.value.missing == ["profile_opt_in"]


def test_opt_in_defaults_false() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN),
            _allocation({"destructive_ops": ["force_crash"]}),
            DestructiveOp(kind="force_crash"),
        )
    assert exc.value.missing == ["profile_opt_in"]


def test_all_three_absent_lists_all() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation({}), _op(False))
    assert exc.value.missing == ["capability_scope", "admin_role", "profile_opt_in"]


def test_scope_with_non_list_value_fails_closed() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation({"destructive_ops": "force_crash"}), _op(True)
        )
    assert exc.value.missing == ["capability_scope"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/security/test_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.security.gate'`.

- [ ] **Step 3: Write `gate.py`**

`src/kdive/security/gate.py`:

```python
"""The three-check destructive-op gate (ADR-0006, ADR-0020).

A destructive operation is allowed only when all three independent checks pass: the
allocation's capability scope grants the op, the principal holds `admin` on the
allocation's project, and the controlling profile explicitly opted the op in. The gate
is pure policy over `(ctx, allocation, op)`; it reads the first two checks from data
and trusts the handler to resolve the third (`profile_opt_in`). A denial raises
`DestructiveOpDenied` listing every missing check, so an audit/log line shows the full
reason. The gate never writes audit rows (it has no connection); a handler that catches
`DestructiveOpDenied` audits the denied attempt with `transition=f"{op.kind}:denied"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kdive.security.rbac import AuthorizationError, Role, require_role

if TYPE_CHECKING:
    from kdive.domain.models import Allocation
    from kdive.mcp.auth import RequestContext

_DESTRUCTIVE_OPS_KEY = "destructive_ops"


@dataclass(frozen=True)
class DestructiveOp:
    """A destructive operation and whether its controlling profile opted it in.

    ``profile_opt_in`` defaults to ``False`` so a handler that forgets to resolve the
    opt-in is denied (deny-by-default).
    """

    kind: str
    profile_opt_in: bool = False


class DestructiveOpDenied(AuthorizationError):
    """A destructive op failed one or more of the three gate checks."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"destructive op denied; missing checks: {missing}")


def _scope_permits(allocation: Allocation, op: DestructiveOp) -> bool:
    granted = allocation.capability_scope.get(_DESTRUCTIVE_OPS_KEY)
    return isinstance(granted, (list, tuple)) and op.kind in granted


def assert_destructive_allowed(
    ctx: RequestContext, allocation: Allocation, op: DestructiveOp
) -> None:
    """Allow a destructive op only if all three checks pass.

    Raises:
        DestructiveOpDenied: One or more of capability scope, ``admin`` role, or profile
            opt-in is absent; ``.missing`` lists every failed check in check order.
    """
    missing: list[str] = []
    if not _scope_permits(allocation, op):
        missing.append("capability_scope")
    try:
        require_role(ctx, allocation.project, Role.ADMIN)
    except AuthorizationError:
        missing.append("admin_role")
    if not op.profile_opt_in:
        missing.append("profile_opt_in")
    if missing:
        raise DestructiveOpDenied(missing)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/security/test_gate.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Guardrails (full suite) + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest -q
git add src/kdive/security/gate.py tests/security/test_gate.py
git commit -m "feat(security): three-check destructive-op gate"
```

---

## Self-Review

**Spec coverage:**
- `Role` enum + rank, `roles_from_claims`, `require_role` → Task 2. ✓
- `RequestContext.roles` + `context_from_claims` population, `compare=False` hashability → Task 3. ✓
- `args_digest` (canonical JSON, one-way), `record` (one row, caller's transaction, `project in ctx.projects` guard) → Task 4. ✓
- Transition+audit atomicity and rollback-leaves-zero → Task 4 (`test_record_in_transition_transaction_is_atomic`, `test_record_rolls_back_with_failed_transition`). ✓
- `DestructiveOp`, `DestructiveOpDenied`, three-check gate with the three single-factor denials → Task 5. ✓
- `require_role` member-without-role → `AuthorizationError` not `KeyError` → Task 2 (`test_require_role_member_without_role_denied_not_keyerror`). ✓
- `args_digest` does not contain secret; `default=str` scalar domain → Task 4. ✓
- Denial-audit shape: `DestructiveOpDenied.missing` is ordered and asserted → Task 5 (`test_all_three_absent_lists_all`); the handler wiring is a later-issue contract (out of scope, per spec Non-goals). ✓
- Import-cycle break (function-level `AuthError` import in `roles_from_claims`) → Task 2 code + comment. ✓
- Append-only: `audit.py` exposes only `record`/`args_digest` (no update/delete) → structural, satisfied by the module surface. ✓

**Placeholder scan:** none — every step carries complete code or an exact command.

**Type consistency:** `Role`, `RequestContext`, `roles_from_claims`, `require_role`, `record`, `args_digest`, `DestructiveOp(kind, profile_opt_in)`, `DestructiveOpDenied(missing)`, `assert_destructive_allowed(ctx, allocation, op)` are spelled identically across tasks and tests. `record`'s keyword-only signature `(conn, ctx, *, tool, object_kind, object_id, transition, args, project)` matches every call site in the tests.

**Edge note for the executor:** Tasks 2 and 3 are mutually dependent (the rbac tests construct a `RequestContext` with `roles`, which Task 3 adds). Land them as two commits but expect the rbac suite to go green only once both are applied; run `uv run python -m pytest tests/security/test_rbac.py tests/mcp/test_auth.py -q` after Task 3 to confirm both.
