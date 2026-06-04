"""runs.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
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
from kdive.security.rbac import AuthorizationError, Role

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


async def _seed_run(
    pool: AsyncConnectionPool, *, state: RunState, failure: ErrorCategory | None = None
) -> str:
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
            run_id = await _seed_run(
                pool, state=RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE
            )
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


async def _create(
    pool: AsyncConnectionPool, ctx: RequestContext, inv_id: str, sys_id: str, profile=None
):
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


def test_create_accepts_empty_build_profile(migrated_url: str) -> None:
    # ADR-0026 §6: an empty {} build_profile is allowed in M0 (the build plane owns
    # content validation). Pin it so a future "reject empty profile" change is caught.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            # Call create_run directly: the _create helper's `profile or _profile()` would
            # coalesce a falsy {} away, so it cannot exercise the empty-profile path.
            resp = await runs_tools.create_run(
                pool, _ctx(), investigation_id=inv_id, system_id=sys_id, build_profile={}
            )
            assert resp.status == "created"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT build_profile FROM runs WHERE id = %s", (resp.object_id,))
                row = await cur.fetchone()
        assert row is not None and row["build_profile"] == {}

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
                await cur.execute(
                    "SELECT count(*) AS n FROM runs WHERE investigation_id = %s", (inv_id,)
                )
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
def test_create_on_terminal_investigation_is_config_error(
    migrated_url: str, state: InvestigationState
) -> None:
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
            sys_id = await _seed_system(pool, project="proj")
            other_inv = await _seed_investigation(pool, project="proj")
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE investigations SET project = 'p2' WHERE id = %s", (other_inv,)
                )
            ctx = RequestContext(
                principal="user-1",
                agent_session="s",
                projects=("proj", "p2"),
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
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.INVESTIGATION, UUID(inv_id)),
                ):
                    task = asyncio.create_task(_create(pool, _ctx(), inv_id, sys_id))
                    await asyncio.sleep(0.3)
                    assert not task.done()  # blocked on the held INVESTIGATION lock
                resp = await task
            assert resp.status == "created"

    asyncio.run(_run())
