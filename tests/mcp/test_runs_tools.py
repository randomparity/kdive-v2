"""runs.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, LiteralString
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
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
    pool: AsyncConnectionPool,
    *,
    state: RunState,
    failure: ErrorCategory | None = None,
    build_profile: dict[str, Any] | None = None,
    project: str = "proj",
) -> str:
    inv_id = await _seed_investigation(pool, project=project)
    sys_id = await _seed_system(pool, project=project)
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
    runs_tools.register_handlers(registry, builder=_FakeBuilder())
    assert registry.get(JobKind.BUILD) is not None
