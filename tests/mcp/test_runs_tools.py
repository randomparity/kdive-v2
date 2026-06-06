"""runs.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
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
    provisioning_profile: dict[str, Any] | None = None,
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
                provisioning_profile=provisioning_profile
                if provisioning_profile is not None
                else {"schema_version": 1},
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
    pool: AsyncConnectionPool,
    *,
    state: RunState,
    failure: ErrorCategory | None = None,
    build_profile: dict[str, Any] | None = None,
    project: str = "proj",
    provisioning_profile: dict[str, Any] | None = None,
) -> str:
    inv_id = await _seed_investigation(pool, project=project)
    sys_id = await _seed_system(pool, project=project, provisioning_profile=provisioning_profile)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                investigation_id=UUID(inv_id),
                system_id=UUID(sys_id),
                state=state,
                build_profile=_profile() if build_profile is None else build_profile,
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


# --- runs.build (synchronous admission) ----------------------------------------------

_VALID_BUILD: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org#v6.9",
    "config_ref": "file:///configs/kdump.config",
}


async def _build(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await runs_tools.build_run(pool, ctx, run_id)


async def _count(pool: AsyncConnectionPool, query: LiteralString, params: tuple[Any, ...]) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    return 0 if row is None else int(row["n"])


async def _system_id_of(pool: AsyncConnectionPool, run_id: str) -> str:
    """Resolve the System a Run is bound to (the console artifact is System-owned)."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT system_id FROM runs WHERE id = %s", (run_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["system_id"])


def test_build_created_run_flips_running_and_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            resp = await _build(pool, _ctx(), run_id)
            assert resp.status == "queued"
            assert resp.data["run_id"] == run_id
            assert "jobs.wait" in resp.suggested_next_actions
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
                run_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind='build' AND dedup_key=%s",
                    (f"{run_id}:build",),
                )
                jobs = await cur.fetchone()
        assert run_row is not None and run_row["state"] == "running"
        assert jobs is not None and jobs["n"] == 1

    asyncio.run(_run())


def test_build_is_idempotent_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            r1 = await _build(pool, _ctx(), run_id)
            r2 = await _build(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert r1.object_id == r2.object_id  # same job (dedup)
        assert njobs == 1

    asyncio.run(_run())


def test_build_on_succeeded_run_returns_same_job_no_transition(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            first = await _build(pool, _ctx(), run_id)
            # Drive the Run to succeeded directly (the handler would do this).
            async with pool.connection() as conn:
                await conn.execute("UPDATE runs SET state='running' WHERE id=%s", (run_id,))
                await conn.execute("UPDATE runs SET state='succeeded' WHERE id=%s", (run_id,))
            again = await _build(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert again.object_id == first.object_id
        assert njobs == 1

    asyncio.run(_run())


def test_build_malformed_profile_is_config_error_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile={"schema_version": 2}
            )
            resp = await _build(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
            ncreated = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='created'", (run_id,)
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert njobs == 0
        assert ncreated == 1  # the Run is untouched (still created), not flipped

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.CANCELED])
def test_build_on_terminal_run_is_config_error(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=state, build_profile=copy.deepcopy(_VALID_BUILD))
            resp = await _build(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


def test_build_missing_run_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _build(pool, _ctx(), str(uuid4()))
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_build_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            resp = await _build(pool, _ctx(projects=("other",)), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_build_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _build(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_build_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            with pytest.raises(AuthorizationError):
                await _build(pool, _ctx(Role.VIEWER), run_id)

    asyncio.run(_run())


def test_build_concurrent_flips_once(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            r1, r2 = await asyncio.gather(
                _build(pool, _ctx(), run_id), _build(pool, _ctx(), run_id)
            )
            assert {r1.status, r2.status} == {"queued"}
            assert r1.object_id == r2.object_id  # one job
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
            nflips = await _count(
                pool,
                "SELECT count(*) AS n FROM audit_log WHERE transition='created->running' "
                "AND object_id=%s",
                (run_id,),
            )
        assert njobs == 1
        assert nflips == 1

    asyncio.run(_run())


# --- build_handler (the worker) ------------------------------------------------------

from kdive.domain.models import Job, JobKind  # noqa: E402
from kdive.jobs import queue  # noqa: E402
from kdive.jobs.models import HandlerRegistry  # noqa: E402
from kdive.providers.local_libvirt.build import BuildOutput  # noqa: E402


class _FakeBuilder:
    """Records build() calls; returns a canned BuildOutput or raises."""

    def __init__(self, *, error: ErrorCategory | None = None) -> None:
        self.calls: list[UUID] = []
        self._error = error

    def build(self, run_id: UUID, profile: Any) -> BuildOutput:
        self.calls.append(run_id)
        if self._error is not None:
            raise CategorizedError("boom", category=self._error)
        return BuildOutput(
            kernel_ref=f"proj/runs/{run_id}/kernel",
            debuginfo_ref=f"proj/runs/{run_id}/vmlinux",
            build_id="abcdef0123456789",
        )


async def _enqueue_build_job(pool: AsyncConnectionPool, run_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.BUILD,
            {"run_id": run_id},
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{run_id}:build",
        )


async def _seed_running_run(pool: AsyncConnectionPool) -> str:
    """A Run admitted for build (created → running) with a valid profile."""
    run_id = await _seed_run(
        pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
    )
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET state='running' WHERE id=%s", (run_id,))
    return run_id


def test_build_handler_drives_run_succeeded_sets_refs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            builder = _FakeBuilder()
            async with pool.connection() as conn:
                result = await runs_tools.build_handler(conn, job, builder)
            assert result == run_id
            assert builder.calls == [UUID(run_id)]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, kernel_ref, debuginfo_ref FROM runs WHERE id=%s", (run_id,)
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='build'",
                    (run_id,),
                )
                steps = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition='running->succeeded' "
                    "AND object_id=%s",
                    (run_id,),
                )
                audit_n = await cur.fetchone()
        assert row is not None and row["state"] == "succeeded"
        assert row["kernel_ref"] == f"proj/runs/{run_id}/kernel"
        assert row["debuginfo_ref"] == f"proj/runs/{run_id}/vmlinux"
        assert steps is not None and steps["n"] == 1
        assert audit_n is not None and audit_n["n"] == 1

    asyncio.run(_run())


def test_build_handler_replay_does_not_rebuild(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            builder = _FakeBuilder()
            async with pool.connection() as conn:
                await runs_tools.build_handler(conn, job, builder)
            # Re-dispatch the same job: the ledger short-circuits the rebuild.
            async with pool.connection() as conn:
                await runs_tools.build_handler(conn, job, builder)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id=%s", (run_id,))
                row = await cur.fetchone()
        assert builder.calls == [UUID(run_id)]  # built exactly once
        assert row is not None and row["state"] == "succeeded"

    asyncio.run(_run())


def test_build_handler_build_failure_sets_run_failed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            builder = _FakeBuilder(error=ErrorCategory.BUILD_FAILURE)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_tools.build_handler(conn, job, builder)
            assert caught.value.category is ErrorCategory.BUILD_FAILURE
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, failure_category FROM runs WHERE id=%s", (run_id,))
                row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM run_steps WHERE run_id=%s", (run_id,))
                steps = await cur.fetchone()
        assert row is not None and row["state"] == "failed"
        assert row["failure_category"] == "build_failure"
        assert steps is not None and steps["n"] == 0  # no ledger row on failure

    asyncio.run(_run())


def test_build_handler_config_failure_sets_run_failed_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            builder = _FakeBuilder(error=ErrorCategory.CONFIGURATION_ERROR)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_tools.build_handler(conn, job, builder)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, failure_category FROM runs WHERE id=%s", (run_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "failed"
        assert row["failure_category"] == "configuration_error"

    asyncio.run(_run())


def test_build_handler_tolerates_concurrent_cancel(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            # Cancel the Run before the handler finalizes (running → canceled).
            async with pool.connection() as conn:
                await conn.execute("UPDATE runs SET state='canceled' WHERE id=%s", (run_id,))
            builder = _FakeBuilder()
            async with pool.connection() as conn:
                result = await runs_tools.build_handler(conn, job, builder)
            assert result == run_id  # does not crash
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id=%s", (run_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "canceled"  # cancel wins; build is inert

    asyncio.run(_run())


def test_build_handler_crash_window_re_dispatch_overwrites_no_orphan(migrated_url: str) -> None:
    # Simulate a finalize crash: the first builder stores its artifacts then raises (after the
    # puts). A re-dispatch with a succeeding builder must use the SAME deterministic keys and
    # finalize without a second ledger row.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            crashing = _FakeBuilder(error=ErrorCategory.BUILD_FAILURE)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_tools.build_handler(conn, job, crashing)
            # The failure drove the Run terminal (failed); a real lease-lapse crash would not.
            # Reset to running to model a crash that left no ledger row but the Run still running.
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET state='running', failure_category=NULL WHERE id=%s", (run_id,)
                )
            ok = _FakeBuilder()
            async with pool.connection() as conn:
                await runs_tools.build_handler(conn, job, ok)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, kernel_ref FROM runs WHERE id=%s", (run_id,))
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='build'",
                    (run_id,),
                )
                steps = await cur.fetchone()
        assert row is not None and row["state"] == "succeeded"
        assert row["kernel_ref"] == f"proj/runs/{run_id}/kernel"  # same deterministic key
        assert steps is not None and steps["n"] == 1  # exactly one ledger row

    asyncio.run(_run())


def test_register_handlers_binds_build() -> None:
    registry = HandlerRegistry()
    runs_tools.register_handlers(
        registry, builder=_FakeBuilder(), installer=_FakeInstaller(), booter=_FakeBooter()
    )
    assert registry.get(JobKind.BUILD) is not None


# --- runs.install / runs.boot (install + boot plane, #19) ----------------------------

from kdive.domain.capture import CaptureMethod  # noqa: E402
from kdive.providers.local_libvirt.install import (  # noqa: E402
    Booter,
    Installer,
)

_SUCCEEDED_BUILD: dict[str, Any] = {
    **_VALID_BUILD,
    "cmdline": "console=ttyS0 crashkernel=256M",
}


class _FakeInstaller:
    """Records install() calls (incl. method/initrd_ref); returns or raises a canned category."""

    def __init__(self, *, error: ErrorCategory | None = None) -> None:
        self.calls: list[tuple[UUID, UUID, str, str, CaptureMethod, str | None]] = []
        self._error = error

    def install(
        self,
        system_id: UUID,
        run_id: UUID,
        kernel_ref: str,
        *,
        cmdline: str,
        method: CaptureMethod = CaptureMethod.HOST_DUMP,
        initrd_ref: str | None = None,
    ) -> None:
        self.calls.append((system_id, run_id, kernel_ref, cmdline, method, initrd_ref))
        if self._error is not None:
            raise CategorizedError("boom", category=self._error)


class _FakeBooter:
    """Records boot() calls; returns or raises a canned category."""

    def __init__(self, *, error: ErrorCategory | None = None) -> None:
        self.calls: list[UUID] = []
        self._error = error

    def boot(self, system_id: UUID) -> None:
        self.calls.append(system_id)
        if self._error is not None:
            raise CategorizedError("boom", category=self._error)


async def _seed_succeeded_run(
    pool: AsyncConnectionPool,
    *,
    build_profile: dict[str, Any] | None = None,
    provisioning_profile: dict[str, Any] | None = None,
) -> str:
    """A built Run: state succeeded, kernel_ref set (the install plane's precondition)."""
    run_id = await _seed_run(
        pool,
        state=RunState.SUCCEEDED,
        build_profile=build_profile
        if build_profile is not None
        else copy.deepcopy(_SUCCEEDED_BUILD),
        provisioning_profile=provisioning_profile,
    )
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET kernel_ref=%s WHERE id=%s", (f"local/runs/{run_id}/kernel", run_id)
        )
    return run_id


async def _seed_succeeded_run_on_system(pool: AsyncConnectionPool, system_id: str) -> str:
    """A second built Run bound to an existing System (a re-boot of the same System)."""
    inv_id = await _seed_investigation(pool)
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
                system_id=UUID(system_id),
                state=RunState.SUCCEEDED,
                build_profile=copy.deepcopy(_SUCCEEDED_BUILD),
                failure_category=None,
            ),
        )
        await conn.execute(
            "UPDATE runs SET kernel_ref=%s WHERE id=%s", (f"local/runs/{run.id}/kernel", run.id)
        )
    return str(run.id)


async def _record_install_step(pool: AsyncConnectionPool, run_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'install', 'succeeded', '{}'::jsonb)",
            (run_id,),
        )


async def _install(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await runs_tools.install_run(pool, ctx, run_id)


async def _boot(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await runs_tools.boot_run(pool, ctx, run_id)


def test_install_succeeded_run_enqueues_no_state_flip(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await _install(pool, _ctx(), run_id)
            assert resp.status == "queued"
            assert resp.data["run_id"] == run_id
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='install' AND dedup_key=%s",
                (f"{run_id}:install",),
            )
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
            naudit = await _count(
                pool,
                "SELECT count(*) AS n FROM audit_log WHERE tool='runs.install' AND object_id=%s",
                (run_id,),
            )
        assert njobs == 1
        assert nstate == 1  # Run stays succeeded (no flip)
        assert naudit == 1

    asyncio.run(_run())


def test_install_is_idempotent_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            r1 = await _install(pool, _ctx(), run_id)
            r2 = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert r1.object_id == r2.object_id
        assert njobs == 1

    asyncio.run(_run())


def test_cmdline_default_is_kdump_reserving_for_kdump() -> None:
    run = _run_with_build_profile({"schema_version": 1})
    assert "crashkernel=" in runs_tools._cmdline_for(run, CaptureMethod.KDUMP)


def test_cmdline_default_omits_crashkernel_for_non_kdump() -> None:
    run = _run_with_build_profile({"schema_version": 1})
    assert "crashkernel=" not in runs_tools._cmdline_for(run, CaptureMethod.CONSOLE)


def test_cmdline_explicit_overrides_default_for_any_method() -> None:
    run = _run_with_build_profile({"cmdline": "console=ttyS0 dhash_entries=1"})
    assert runs_tools._cmdline_for(run, CaptureMethod.KDUMP) == "console=ttyS0 dhash_entries=1"


def _run_with_build_profile(build_profile: dict[str, Any]) -> Run:
    return Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        investigation_id=uuid4(),
        system_id=uuid4(),
        state=RunState.SUCCEEDED,
        build_profile=build_profile,
    )


def test_install_nonkdump_system_admits_cmdline_without_crashkernel(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "console=ttyS0"}
            )  # bare System (default seed profile) => method console
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())


def test_install_kdump_system_without_crashkernel_is_config_error_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "console=ttyS0"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "cmdline_missing_crashkernel"
        assert njobs == 0

    asyncio.run(_run())


def test_install_kdump_system_with_crashkernel_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "console=ttyS0 crashkernel=256M"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            resp = await _install(pool, _ctx(), run_id)
        assert resp.status == "queued"

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.CREATED, RunState.RUNNING])
def test_install_on_unbuilt_run_is_config_error(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=state, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )
            resp = await _install(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.CANCELED])
def test_install_on_terminal_run_is_config_error(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=state, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )
            resp = await _install(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == state.value

    asyncio.run(_run())


def test_install_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await _install(pool, _ctx(projects=("other",)), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_install_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _install(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_install_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            with pytest.raises(AuthorizationError):
                await _install(pool, _ctx(Role.VIEWER), run_id)

    asyncio.run(_run())


def test_boot_without_install_step_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            resp = await _boot(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs WHERE kind='boot'", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert njobs == 0  # no boot job without a succeeded install step

    asyncio.run(_run())


def test_boot_after_install_step_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            resp = await _boot(pool, _ctx(), run_id)
            assert resp.status == "queued"
            again = await _boot(pool, _ctx(), run_id)
            njobs = await _count(
                pool,
                "SELECT count(*) AS n FROM jobs WHERE kind='boot' AND dedup_key=%s",
                (f"{run_id}:boot",),
            )
        assert resp.object_id == again.object_id  # idempotent
        assert njobs == 1

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.CREATED, RunState.FAILED])
def test_boot_on_non_succeeded_run_is_config_error(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=state, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )
            resp = await _boot(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_boot_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            with pytest.raises(AuthorizationError):
                await _boot(pool, _ctx(Role.VIEWER), run_id)

    asyncio.run(_run())


# --- install_handler / boot_handler (the worker) -------------------------------------


async def _enqueue_job(pool: AsyncConnectionPool, kind: JobKind, run_id: str, step: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            kind,
            {"run_id": run_id},
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{run_id}:{step}",
        )


def test_install_handler_records_step_run_stays_succeeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                result = await runs_tools.install_handler(conn, job, installer)
            assert result == run_id
            assert len(installer.calls) == 1
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
        assert nsteps == 1
        assert nstate == 1  # Run unchanged

    asyncio.run(_run())


def test_install_handler_replay_does_not_restage(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert len(installer.calls) == 1  # built once

    asyncio.run(_run())


class _SlowInstaller:
    """An installer that sleeps inside install() to widen the concurrent-dispatch window."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    def install(
        self,
        system_id: UUID,
        run_id: UUID,
        kernel_ref: str,
        *,
        cmdline: str,
        method: CaptureMethod = CaptureMethod.HOST_DUMP,
        initrd_ref: str | None = None,
    ) -> None:
        import time

        self.calls.append(run_id)
        time.sleep(0.2)


def test_install_handler_concurrent_dispatch_invokes_once(migrated_url: str) -> None:
    # Two concurrent dispatches of the SAME install job (the queue's at-least-once delivery)
    # on distinct connections: the per-Run lock serializes them, so the installer runs once
    # and exactly one ledger row is written.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _SlowInstaller()

            async def _dispatch() -> None:
                async with pool.connection() as conn:
                    await runs_tools.install_handler(conn, job, installer)

            await asyncio.gather(_dispatch(), _dispatch())
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
        assert len(installer.calls) == 1  # the per-Run lock prevents a double redefine
        assert nsteps == 1

    asyncio.run(_run())


def test_install_handler_failure_records_no_step(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller(error=ErrorCategory.INSTALL_FAILURE)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_tools.install_handler(conn, job, installer)
            assert caught.value.category is ErrorCategory.INSTALL_FAILURE
            nsteps = await _count(
                pool, "SELECT count(*) AS n FROM run_steps WHERE run_id=%s", (run_id,)
            )
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
        assert nsteps == 0  # no ledger row on failure
        assert nstate == 1  # Run still succeeded

    asyncio.run(_run())


def test_install_handler_missing_kernel_ref_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.SUCCEEDED, build_profile=copy.deepcopy(_SUCCEEDED_BUILD)
            )  # no kernel_ref set
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_tools.install_handler(conn, job, installer)
            assert installer.calls == []  # never reached the installer
            nsteps = await _count(
                pool, "SELECT count(*) AS n FROM run_steps WHERE run_id=%s", (run_id,)
            )
        assert nsteps == 0

    asyncio.run(_run())


def test_boot_handler_records_step_run_stays_succeeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()
            async with pool.connection() as conn:
                result = await runs_tools.boot_handler(conn, job, booter)
            assert result == run_id
            assert len(booter.calls) == 1
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert nsteps == 1

    asyncio.run(_run())


def test_boot_handler_replay_does_not_reboot(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()
            async with pool.connection() as conn:
                await runs_tools.boot_handler(conn, job, booter)
            async with pool.connection() as conn:
                await runs_tools.boot_handler(conn, job, booter)
        assert len(booter.calls) == 1

    asyncio.run(_run())


@pytest.mark.parametrize("category", [ErrorCategory.BOOT_TIMEOUT, ErrorCategory.READINESS_FAILURE])
def test_boot_handler_failure_records_no_step(migrated_url: str, category: ErrorCategory) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=category)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_tools.boot_handler(conn, job, booter)
            assert caught.value.category is category
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert nsteps == 0  # no ledger row on failure

    asyncio.run(_run())


def test_register_handlers_binds_install_and_boot() -> None:
    registry = HandlerRegistry()
    runs_tools.register_handlers(
        registry, builder=_FakeBuilder(), installer=_FakeInstaller(), booter=_FakeBooter()
    )
    assert registry.get(JobKind.INSTALL) is not None
    assert registry.get(JobKind.BOOT) is not None


def test_boot_handler_registers_console_on_success(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The clean-boot console is the A/B baseline (the `ls /proc`-ran-without-panic
    # evidence) the feature exists to produce, so registration must fire on success too.
    # A real clean boot's console is non-empty (it prints the readiness marker).
    monkeypatch.setattr(runs_tools, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_tools, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            (tmp_path / f"{sid}.log").write_bytes(b"[    0.0] KDIVE-BUSYBOX-READY\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()  # clean success, no error
            async with pool.connection() as conn:
                result = await runs_tools.boot_handler(conn, job, booter)
            assert result == run_id
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console",),
            )
        assert nsteps == 1  # boot step recorded succeeded
        assert n == 1  # non-empty console registered on the happy path

    asyncio.run(_run())


def test_boot_handler_registers_console_even_on_failure(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # On a crash the panic fires before readiness, but the oops console IS on disk — so a
    # non-empty console must still be captured even though the boot step raises.
    monkeypatch.setattr(runs_tools, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_tools, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            (tmp_path / f"{sid}.log").write_bytes(b"Kernel panic - not syncing: __d_lookup\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.BOOT_TIMEOUT)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_tools.boot_handler(conn, job, booter)
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console",),
            )
        assert n == 1

    asyncio.run(_run())


def test_boot_handler_skips_empty_console(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # An empty/unreadable console means capture FAILED (a real boot's console is non-empty).
    # Registering empty bytes as an `available` artifact would be indistinguishable from a
    # crash-free console and could drive a false "fixed" A/B verdict, so it must NOT register.
    monkeypatch.setattr(runs_tools, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_tools, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()  # clean success, but no console file was written
            async with pool.connection() as conn:
                result = await runs_tools.boot_handler(conn, job, booter)
            assert result == run_id
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console",),
            )
        assert nsteps == 1  # boot itself succeeded
        assert n == 0  # but an empty console capture registers nothing

    asyncio.run(_run())


def test_boot_handler_console_is_readable_via_artifacts(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The registered console artifact must be readable through artifacts_list (ADR-0049 D4).

    The SQL-count tests only verify the row was inserted; this test proves the artifacts
    read surface actually returns the console artifact, closing the behavioral gap.
    """
    from kdive.mcp.tools import artifacts as artifacts_tools

    monkeypatch.setattr(runs_tools, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_tools, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            system_id = await _system_id_of(pool, run_id)
            (tmp_path / f"{system_id}.log").write_bytes(b"[    0.0] KDIVE-BUSYBOX-READY\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()  # clean success
            async with pool.connection() as conn:
                result = await runs_tools.boot_handler(conn, job, booter)
            assert result == run_id

            # artifacts_list must return the console as a redacted artifact envelope.
            listed = await artifacts_tools.artifacts_list(pool, _ctx(), system_id=system_id)

        assert len(listed) == 1
        console = listed[0]
        assert console.status == "available"
        assert console.refs is not None
        assert console.refs.get("object", "").endswith("/console")

    asyncio.run(_run())


def test_boot_handler_reboot_refreshes_console_etag(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Re-booting a System refreshes the console row's etag to match the rewritten object.

    The console object key is System-scoped, so a second boot of the same System (a new
    Run) overwrites the object with a new etag. Before the fix the ledger insert was skipped
    whenever a `%/console` row already existed, leaving the row pinned to the FIRST boot's
    etag while the object held the second boot's content — a consumer's conditional `If-Match`
    GET then hit STALE_HANDLE. The row's etag must instead track the stored object.

    The two boots run sequentially, matching M0 (a System's Runs boot one at a time). Two Runs
    booting one System *concurrently* is not serialized by boot_handler and is out of scope.
    """
    monkeypatch.setattr(runs_tools, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_tools, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # First boot of the System registers the console row at the first object's etag.
            run1 = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run1)
            sid = await _system_id_of(pool, run1)
            (tmp_path / f"{sid}.log").write_bytes(b"[    0.0] FIRST-BOOT-MARKER ready\n")
            job1 = await _enqueue_job(pool, JobKind.BOOT, run1, "boot")
            async with pool.connection() as conn:
                await runs_tools.boot_handler(conn, job1, _FakeBooter())

            # Second boot of the SAME System (new Run): the host console log is overwritten
            # with different content, so put_artifact rewrites the object at the same key.
            run2 = await _seed_succeeded_run_on_system(pool, sid)
            await _record_install_step(pool, run2)
            (tmp_path / f"{sid}.log").write_bytes(b"[    0.0] SECOND-BOOT-MARKER oops\n")
            job2 = await _enqueue_job(pool, JobKind.BOOT, run2, "boot")
            async with pool.connection() as conn:
                await runs_tools.boot_handler(conn, job2, _FakeBooter())

            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console",),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT object_key, etag FROM artifacts WHERE object_key LIKE %s",
                    ("%/console",),
                )
                row = await cur.fetchone()

        assert n == 1  # still one System-scoped console row, never a duplicate
        assert row is not None
        # The row's etag must equal the rewritten object's etag — no stale-etag row.
        head = minio_store.head(row["object_key"])
        assert head is not None
        assert row["etag"] == head.etag
        # And the consequence the issue names: a conditional If-Match GET (the pattern
        # get_artifact uses) resolves to the SECOND boot's console, not a STALE_HANDLE.
        fetched = minio_store.get_artifact(row["object_key"], row["etag"])
        assert b"SECOND-BOOT-MARKER" in fetched.data
        assert b"FIRST-BOOT-MARKER" not in fetched.data

    asyncio.run(_run())


def _assert_ports() -> None:
    # Structural conformance: the fakes satisfy the realized Protocols (ty enforces; this
    # keeps the import used and documents the contract).
    _i: Installer = _FakeInstaller()
    _b: Booter = _FakeBooter()
    assert _i is not None and _b is not None


def _system_with_profile(profile: dict[str, Any]) -> System:
    return System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        allocation_id=uuid4(),
        state=SystemState.READY,
        provisioning_profile=profile,
    )


def _profile_dump(**local_libvirt: Any) -> dict[str, Any]:
    """A real ProvisioningProfile.model_dump(by_alias=True) — pins the 'local-libvirt' alias."""
    from kdive.profiles.provisioning import ProvisioningProfile

    section: dict[str, Any] = {"rootfs": {"kind": "path", "path": "/img"}}
    section.update(local_libvirt)
    return ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {"local-libvirt": section},
        }
    ).model_dump(by_alias=True)


def test_install_method_kdump_when_crashkernel_set() -> None:
    system = _system_with_profile(_profile_dump(crashkernel="256M"))
    assert runs_tools._install_method_for(system) is CaptureMethod.KDUMP


def test_install_method_gdbstub_when_flag_set() -> None:
    system = _system_with_profile(_profile_dump(debug={"gdbstub": True}))
    assert runs_tools._install_method_for(system) is CaptureMethod.GDBSTUB


def test_install_method_host_dump_when_preserve_on_crash() -> None:
    system = _system_with_profile(_profile_dump(debug={"preserve_on_crash": True}))
    assert runs_tools._install_method_for(system) is CaptureMethod.HOST_DUMP


def test_install_method_console_for_bare_system() -> None:
    system = _system_with_profile(_profile_dump())
    assert runs_tools._install_method_for(system) is CaptureMethod.CONSOLE


def test_install_method_console_for_partial_profile_does_not_raise() -> None:
    # The minimal seed profile (no provider section) must resolve, not raise.
    system = _system_with_profile({"schema_version": 1})
    assert runs_tools._install_method_for(system) is CaptureMethod.CONSOLE


def test_install_method_reads_alias_not_attribute_spelling() -> None:
    # A crashkernel under the WRONG key 'local_libvirt' must NOT resolve kdump:
    # the resolver reads the persisted alias 'local-libvirt' (ADR-0051 Decision 1).
    system = _system_with_profile({"provider": {"local_libvirt": {"crashkernel": "256M"}}})
    assert runs_tools._install_method_for(system) is CaptureMethod.CONSOLE


async def _record_build_ledger(
    pool: AsyncConnectionPool, run_id: str, result: dict[str, Any]
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run_id, Jsonb(result)),
        )


def test_install_handler_forwards_console_method_for_bare_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)  # bare System => console
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][4] is CaptureMethod.CONSOLE
        assert installer.calls[0][5] is None  # no initrd

    asyncio.run(_run())


def test_install_handler_forwards_host_dump_for_preserve_on_crash(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, provisioning_profile=_profile_dump(debug={"preserve_on_crash": True})
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][4] is CaptureMethod.HOST_DUMP

    asyncio.run(_run())


def test_install_handler_forwards_initrd_ref_from_build_ledger(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_build_ledger(
                pool, run_id, {"kernel_ref": "k", "initrd_ref": "local/runs/x/initrd"}
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][5] == "local/runs/x/initrd"

    asyncio.run(_run())


def test_install_handler_no_initrd_when_ledger_initrd_blank(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_build_ledger(pool, run_id, {"kernel_ref": "k", "initrd_ref": ""})
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][5] is None

    asyncio.run(_run())
