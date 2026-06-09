"""runs.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import (
    Allocation,
    Investigation,
    Job,
    JobKind,
    Resource,
    ResourceKind,
    Run,
    System,
)
from kdive.domain.pcie import PCIeClaim
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.jobs.handlers import runs as runs_handlers
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.runs import common as runs_common
from kdive.mcp.tools.lifecycle.runs.build import RunBuildHandlers
from kdive.mcp.tools.lifecycle.runs.create import (
    RunCreateRequest,
    RunReuseRequirementInput,
    create_run,
)
from kdive.mcp.tools.lifecycle.runs.steps import boot_run, install_run
from kdive.mcp.tools.lifecycle.runs.view import get_run
from kdive.providers.component_validation import ComponentSourceCapabilities
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.runs import steps as run_steps
from tests.db_waits import wait_until_any_backend_waiting

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org#v6.9",
    "config": {"kind": "local", "path": "/configs/kdump.config"},
}


def _profile() -> dict[str, Any]:
    return copy.deepcopy(_PROFILE)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


def _run_model(
    state: RunState,
    *,
    failure: ErrorCategory | None = None,
    expected_boot_failure: dict[str, str] | None = None,
) -> Run:
    return Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        investigation_id=uuid4(),
        system_id=uuid4(),
        state=state,
        build_profile=_profile(),
        expected_boot_failure=expected_boot_failure,
        failure_category=failure,
    )


def _job_model(state: JobState = JobState.QUEUED) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.BUILD,
        payload={"run_id": str(uuid4())},
        state=state,
        max_attempts=3,
        authorizing={"principal": "user-1", "agent_session": "s", "project": "proj"},
        dedup_key="run-build",
    )


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
    requested_vcpus: int | None = None,
    requested_memory_gb: int | None = None,
    requested_disk_gb: int | None = None,
    pcie_claim: list[PCIeClaim] | None = None,
    lease_expiry: datetime | None = None,
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
                requested_vcpus=requested_vcpus,
                requested_memory_gb=requested_memory_gb,
                requested_disk_gb=requested_disk_gb,
                pcie_claim=pcie_claim or [],
                lease_expiry=lease_expiry,
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
                else _profile_dump(),
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


def test_envelope_for_run_failed_uses_run_failure_category() -> None:
    resp = runs_common.envelope_for_run(
        _run_model(RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE)
    )

    assert resp.status == "error"
    assert resp.error_category == "build_failure"
    assert resp.data == {"current_status": "failed"}


def test_envelope_for_run_failed_defaults_to_infrastructure_failure() -> None:
    resp = runs_common.envelope_for_run(_run_model(RunState.FAILED))

    assert resp.status == "error"
    assert resp.error_category == "infrastructure_failure"


def test_envelope_for_run_expected_boot_failure_detail_is_structured() -> None:
    expected = {
        "pattern": "__d_lookup|Oops",
        "kind": "console_crash",
        "description": "known crash",
    }
    resp = runs_common.envelope_for_run(
        _run_model(RunState.CREATED, expected_boot_failure=expected),
        required_cmdline="panic_on_oops=1",
    )

    assert resp.status == "created"
    assert resp.suggested_next_actions == ["runs.get", "runs.build"]
    assert resp.data["required_cmdline"] == "panic_on_oops=1"
    assert resp.data["expected_boot_failure"] == "console_crash"
    assert resp.data["expected_boot_failure_detail"] == expected


@pytest.mark.parametrize(
    ("state", "actions"),
    [
        (RunState.CREATED, ["runs.get", "runs.build"]),
        (RunState.RUNNING, ["runs.get", "runs.build"]),
        (RunState.SUCCEEDED, ["runs.get"]),
        (RunState.CANCELED, ["runs.get"]),
    ],
)
def test_envelope_for_run_suggests_build_only_before_terminal_states(
    state: RunState, actions: list[str]
) -> None:
    resp = runs_common.envelope_for_run(_run_model(state))

    assert resp.status == state.value
    assert resp.suggested_next_actions == actions
    assert resp.data == {"project": "proj"}


def test_run_job_envelope_adds_run_id_to_standard_job_envelope() -> None:
    run_id = uuid4()
    job = _job_model()

    resp = runs_common.run_job_envelope(job, run_id)

    assert resp.object_id == str(job.id)
    assert resp.status == "queued"
    assert resp.suggested_next_actions == ["jobs.wait", "jobs.cancel"]
    assert resp.data == {"kind": "build", "run_id": str(run_id)}


def test_get_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "created"
        assert resp.suggested_next_actions == ["runs.get", "runs.build"]

    asyncio.run(_run())


def test_get_run_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            with pytest.raises(AuthorizationError):
                await get_run(pool, _ctx(role=None), run_id)

    asyncio.run(_run())


def test_get_failed_run_renders_failure_category(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.FAILED, failure=ErrorCategory.BUILD_FAILURE
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "build_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_get_failed_run_null_category_defaults_infra(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.FAILED, failure=None)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "error" and resp.error_category == "infrastructure_failure"

    asyncio.run(_run())


def test_get_canceled_run_is_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CANCELED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await get_run(pool, _ctx(projects=("other",)), run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await get_run(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_run_exposes_expected_boot_failure(migrated_url: str) -> None:
    expected = {"kind": "console_crash", "pattern": "__d_lookup|Oops"}

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET expected_boot_failure = %s WHERE id = %s",
                    (Jsonb(expected), run_id),
                )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["expected_boot_failure"] == "console_crash"
        assert resp.data["expected_boot_failure_detail"] == expected

    asyncio.run(_run())


async def _create(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    inv_id: str,
    sys_id: str,
    *,
    profile=None,
    reuse_requirement: RunReuseRequirementInput | None = None,
):
    return await create_run(
        pool,
        ctx,
        RunCreateRequest(
            investigation_id=inv_id,
            system_id=sys_id,
            build_profile=profile or _profile(),
            reuse_requirement=reuse_requirement,
        ),
    )


def test_create_first_run_flips_investigation_active(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
            assert resp.status == "created"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, build_profile FROM runs WHERE id = %s", (resp.object_id,)
                )
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
        assert run_row["build_profile"]["source"] == "server"
        assert inv_row is not None and inv_row["state"] == "active"
        assert inv_row["last_run_at"] is not None
        assert flip is not None and flip["n"] == 1

    asyncio.run(_run())


def test_create_rejects_empty_build_profile(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            # Call create_run directly: the _create helper's `profile or _profile()` would
            # coalesce a falsy {} away, so it cannot exercise the empty-profile path.
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(investigation_id=inv_id, system_id=sys_id, build_profile={}),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM runs")
                row = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert row is not None and row["n"] == 0

    asyncio.run(_run())


def test_create_run_persists_expected_boot_failure(migrated_url: str) -> None:
    expected = {
        "kind": "console_crash",
        "pattern": "__d_lookup|Oops",
        "description": "dcache crash",
    }

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile=_profile(),
                    expected_boot_failure=expected,
                ),
            )
            assert resp.status == "created"
            assert resp.data["expected_boot_failure"] == "console_crash"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT expected_boot_failure FROM runs WHERE id = %s",
                    (resp.object_id,),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["expected_boot_failure"] == expected

    asyncio.run(_run())


def test_create_run_rejects_bad_expected_boot_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(
                    investigation_id=inv_id,
                    system_id=sys_id,
                    build_profile=_profile(),
                    expected_boot_failure={"kind": "console_crash", "pattern": ""},
                ),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM runs")
                row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert row is not None and row["n"] == 0

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
            resp = await create_run(
                pool,
                ctx,
                RunCreateRequest(
                    investigation_id=other_inv,
                    system_id=sys_id,
                    build_profile=_profile(),
                ),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_create_non_dict_build_profile_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            bad: Any = "nope"
            resp = await create_run(
                pool,
                _ctx(),
                RunCreateRequest(investigation_id=inv_id, system_id=sys_id, build_profile=bad),
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
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not task.done()  # blocked on the held INVESTIGATION lock
                resp = await task
            assert resp.status == "created"

    asyncio.run(_run())


# --- runs.create: system reuse (#166, ADR-0070) --------------------------------------


async def _provision_job_count(pool: AsyncConnectionPool) -> int:
    return await _count(
        pool, "SELECT count(*) AS n FROM jobs WHERE kind = %s", (JobKind.PROVISION.value,)
    )


async def _non_terminal_run_count(pool: AsyncConnectionPool, sys_id: str) -> int:
    return await _count(
        pool,
        "SELECT count(*) AS n FROM runs WHERE system_id = %s AND state = ANY(%s)",
        (sys_id, [RunState.CREATED.value, RunState.RUNNING.value]),
    )


def test_reuse_attach_runs_without_a_provision_job(migrated_url: str) -> None:
    # Attaching a Run to a matching ready System enqueues NO provision job (provisioning
    # was always separate); the Run is created and can proceed to build/install/boot.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(pool, _ctx(), inv_id, sys_id)
            assert resp.status == "created"
            provision_jobs = await _provision_job_count(pool)
        assert provision_jobs == 0

    asyncio.run(_run())


def test_reuse_optional_assertion_satisfied_creates(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=4, memory_gb=8, disk_gb=40),
            )
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_rejects_non_positive_sizing_requirement(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=0),
            )
            n = await _count(pool, "SELECT count(*) AS n FROM runs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["field"] == "vcpus"
        assert n == 0

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("req_vcpus", "req_memory_gb", "req_disk_gb", "label"),
    [
        (16, None, None, "vcpu_short"),
        (None, 64, None, "memory_short"),
        (None, None, 500, "disk_short"),
    ],
)
def test_reuse_assertion_miss_is_config_error_no_run(
    migrated_url: str,
    req_vcpus: int | None,
    req_memory_gb: int | None,
    req_disk_gb: int | None,
    label: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, requested_vcpus=8, requested_memory_gb=16, requested_disk_gb=100
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(
                    vcpus=req_vcpus,
                    memory_gb=req_memory_gb,
                    disk_gb=req_disk_gb,
                ),
            )
            n = await _count(pool, "SELECT count(*) AS n FROM runs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert n == 0  # the Run is not created on an assertion miss

    asyncio.run(_run())


def test_reuse_pcie_assertion_contained_creates(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                pcie_claim=[{"bdf": "0000:01:00.0", "vendor_id": "8086", "device_id": "1572"}],
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=["8086:1572"]),
            )
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_pcie_assertion_missing_device_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                pcie_claim=[{"bdf": "0000:01:00.0", "vendor_id": "8086", "device_id": "1572"}],
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=["10de:1eb8"]),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reuse_pcie_class_spec_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                pcie_claim=[{"bdf": "0000:01:00.0", "vendor_id": "8086", "device_id": "1572"}],
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=["class=02"]),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reuse_empty_pcie_list_is_a_no_op(migrated_url: str) -> None:
    # require_pcie=[] is "provided but asserts nothing" — must not force a failing match.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)  # no pcie_claim at all
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(pcie=[]),
            )
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_omitted_assertion_creates_with_only_preconditions(migrated_url: str) -> None:
    # No require_* at all (self-provisioned attach) — only the 3 preconditions apply.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "created"

    asyncio.run(_run())


def test_reuse_full_custom_profile_only_sizing_assertion(migrated_url: str) -> None:
    # Full-custom System: allocation requested_* NULL, size lives only in the profile.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, provisioning_profile=_profile_dump_sized(vcpu=8, memory_mb=16384, disk_gb=100)
            )
            ok = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=4, memory_gb=8, disk_gb=40),
            )
        assert ok.status == "created"

    asyncio.run(_run())


def test_reuse_full_custom_profile_only_sizing_miss_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool, provisioning_profile=_profile_dump_sized(vcpu=2, memory_mb=2048, disk_gb=10)
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=8),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reuse_terminal_allocation_is_stale_handle(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, alloc_state=AllocationState.EXPIRED)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"
        assert resp.data["current_status"] == "expired"

    asyncio.run(_run())


def test_reuse_lapsed_lease_active_allocation_is_stale_handle(migrated_url: str) -> None:
    # ACTIVE allocation whose lease window already elapsed (the orphan-reaping window,
    # ADR-0070): seed a PAST lease_expiry deterministically — do not sleep.
    async def _run() -> None:
        past = datetime(2020, 1, 1, tzinfo=UTC)
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(pool, alloc_state=AllocationState.ACTIVE, lease_expiry=past)
            resp = await _create(pool, _ctx(), inv_id, sys_id)
        assert resp.status == "error" and resp.error_category == "stale_handle"

    asyncio.run(_run())


def test_reuse_precondition_beats_assertion_miss(migrated_url: str) -> None:
    # A System that BOTH fails an assertion (too small) AND has a terminal alloc returns
    # the precondition error (stale_handle), not the sizing error — no sizing leak.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool)
            sys_id = await _seed_system(
                pool,
                alloc_state=AllocationState.EXPIRED,
                requested_vcpus=2,
                requested_memory_gb=2,
                requested_disk_gb=10,
            )
            resp = await _create(
                pool,
                _ctx(),
                inv_id,
                sys_id,
                reuse_requirement=RunReuseRequirementInput(vcpus=99),
            )
        assert resp.status == "error" and resp.error_category == "stale_handle"

    asyncio.run(_run())


def test_reuse_system_with_live_run_is_transport_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id)
            assert first.status == "created"  # holds the System (non-terminal Run)
            second = await _create(pool, _ctx(), inv_id, sys_id)
        assert second.status == "error" and second.error_category == "transport_conflict"

    asyncio.run(_run())


def test_reuse_concurrent_creates_one_wins_other_transport_conflict(migrated_url: str) -> None:
    # Two concurrent runs.create on ONE System: the per-System/per-Allocation lock
    # serializes them, so exactly one is created and the other is transport_conflict.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            r1, r2 = await asyncio.gather(
                _create(pool, _ctx(), inv_id, sys_id),
                _create(pool, _ctx(), inv_id, sys_id),
            )
            statuses = sorted([r1.status, r2.status])
            categories = {r.error_category for r in (r1, r2) if r.status == "error"}
            n_runs = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE system_id = %s", (sys_id,)
            )
        assert statuses == ["created", "error"]
        assert categories == {"transport_conflict"}
        assert n_runs == 1

    asyncio.run(_run())


def test_reuse_does_not_deadlock_against_release_under_lock_order(migrated_url: str) -> None:
    # The corrected lock order is ALLOCATION -> SYSTEM -> INVESTIGATION (ALLOCATION first,
    # per the global PROJECT<RESOURCE<ALLOCATION<SYSTEM order). allocations.release holds
    # PROJECT->ALLOCATION; an external holder of the ALLOCATION lock must block create_run
    # at its FIRST lock (ALLOCATION) — proving create_run takes ALLOCATION before SYSTEM,
    # so it cannot form a SYSTEM<->ALLOCATION cycle with release / the reconciler sweep.
    import psycopg

    from kdive.db.locks import LockScope, advisory_xact_lock

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT allocation_id FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
            assert row is not None
            alloc_id = row["allocation_id"]
            async with await psycopg.AsyncConnection.connect(migrated_url) as holder:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.ALLOCATION, alloc_id),
                ):
                    task = asyncio.create_task(_create(pool, _ctx(), inv_id, sys_id))
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not task.done()  # blocked on the held ALLOCATION lock (acquired first)
                resp = await task
            assert resp.status == "created"

    asyncio.run(_run())


# --- runs.build (synchronous admission) ----------------------------------------------

_VALID_BUILD: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org#v6.9",
    "config": {"kind": "local", "path": "/configs/kdump.config"},
}
_TEST_COMPONENT_SOURCES = ComponentSourceCapabilities(
    provider="test-provider",
    accepted_component_sources={"config": frozenset({"local"})},
)
_BUILD_HANDLERS = RunBuildHandlers(_TEST_COMPONENT_SOURCES)


async def _build(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await _BUILD_HANDLERS.build_run(pool, ctx, run_id)


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


def test_build_rejects_unsupported_artifact_config_before_state_change(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            profile = {
                **copy.deepcopy(_VALID_BUILD),
                "config": {
                    "kind": "artifact",
                    "artifact_id": "00000000-0000-0000-0000-000000000001",
                    "sha256": "sha256:" + "1" * 64,
                },
            }
            run_id = await _seed_run(pool, state=RunState.CREATED, build_profile=profile)

            resp = await _BUILD_HANDLERS.build_run(
                pool,
                _ctx(Role.OPERATOR),
                run_id,
            )

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
                run_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind='build' AND dedup_key=%s",
                    (f"{run_id}:build",),
                )
                jobs = await cur.fetchone()

        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert run_row is not None and run_row["state"] == "created"
        assert jobs is not None and jobs["n"] == 0

    asyncio.run(_run())


def test_build_rejects_local_config_outside_provider_roots_before_state_change(
    migrated_url: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.config"
    outside.write_text("CONFIG_CRASH_DUMP=y\n", encoding="utf-8")
    calls: list[Any] = []

    def _reject_config(config: Any) -> None:
        calls.append(config)
        raise CategorizedError(
            "config is outside provider roots",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            profile = {
                **copy.deepcopy(_VALID_BUILD),
                "config": {"kind": "local", "path": str(outside)},
            }
            run_id = await _seed_run(pool, state=RunState.CREATED, build_profile=profile)

            resp = await RunBuildHandlers(
                _TEST_COMPONENT_SOURCES,
                config_validator=_reject_config,
            ).build_run(
                pool,
                _ctx(Role.OPERATOR),
                run_id,
            )

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
                run_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind='build' AND dedup_key=%s",
                    (f"{run_id}:build",),
                )
                jobs = await cur.fetchone()

        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert run_row is not None and run_row["state"] == "created"
        assert jobs is not None and jobs["n"] == 0

    asyncio.run(_run())
    assert len(calls) == 1


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

from kdive.jobs import queue  # noqa: E402
from kdive.jobs.models import HandlerRegistry  # noqa: E402
from kdive.jobs.payloads import BuildPayload, RunPayload  # noqa: E402
from kdive.providers.ports import BuildOutput  # noqa: E402


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


class _MissingBuildOutputBuilder:
    """Raises the typed failure used when an expected build artifact is absent after make."""

    def build(self, run_id: UUID, profile: Any) -> BuildOutput:
        raise CategorizedError(
            "bzImage is missing or unreadable",
            category=ErrorCategory.BUILD_FAILURE,
            details={"output": "bzImage"},
        )


async def _enqueue_build_job(pool: AsyncConnectionPool, run_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.BUILD,
            BuildPayload(run_id=run_id),
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


async def _build_job_for(conn: AsyncConnection, run_id: str) -> Job:
    """Fetch the enqueued build job by its dedup key (no dequeue — no attempt charge)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE dedup_key=%s", (f"{run_id}:build",))
        row = await cur.fetchone()
    assert row is not None
    return Job.model_validate(row)


def test_build_run_records_cmdline_in_the_build_ledger(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            env = await _BUILD_HANDLERS.build_run(
                pool,
                _ctx(Role.OPERATOR),
                run_id,
                cmdline="dhash_entries=1",
            )
            assert env.status != "error"
            async with pool.connection() as conn:
                job = await _build_job_for(conn, run_id)
                await runs_handlers.build_handler(conn, job, _FakeBuilder())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT result FROM run_steps WHERE run_id=%s AND step='build'", (run_id,)
                )
                row = await cur.fetchone()
            assert row is not None and row["result"]["cmdline"] == "dhash_entries=1"

    asyncio.run(_run())


def test_build_run_rejects_a_cmdline_that_overrides_platform_args(migrated_url: str) -> None:
    # The agent's debug cmdline must not carry root=/console=/crashkernel= — the platform injects
    # them (ADR-0061), and a duplicate would win on the kernel's last-occurrence rule.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            resp = await _BUILD_HANDLERS.build_run(
                pool,
                _ctx(Role.OPERATOR),
                run_id,
                cmdline="root=/dev/sda1 dhash_entries=1",
            )
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "cmdline_overrides_platform_args"
        assert njobs == 0

    asyncio.run(_run())


def test_build_run_without_cmdline_records_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(
                pool, state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD)
            )
            await _BUILD_HANDLERS.build_run(
                pool,
                _ctx(Role.OPERATOR),
                run_id,
            )
            async with pool.connection() as conn:
                job = await _build_job_for(conn, run_id)
                await runs_handlers.build_handler(conn, job, _FakeBuilder())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT result FROM run_steps WHERE run_id=%s AND step='build'", (run_id,)
                )
                row = await cur.fetchone()
            assert row is not None and "cmdline" not in row["result"]

    asyncio.run(_run())


def test_build_handler_drives_run_succeeded_sets_refs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            builder = _FakeBuilder()
            async with pool.connection() as conn:
                result = await runs_handlers.build_handler(conn, job, builder)
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
                await runs_handlers.build_handler(conn, job, builder)
            # Re-dispatch the same job: the ledger short-circuits the rebuild.
            async with pool.connection() as conn:
                await runs_handlers.build_handler(conn, job, builder)
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
                    await runs_handlers.build_handler(conn, job, builder)
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


def test_build_handler_missing_output_records_build_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.build_handler(conn, job, _MissingBuildOutputBuilder())
            assert caught.value.category is ErrorCategory.BUILD_FAILURE
            assert caught.value.details == {"output": "bzImage"}
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, failure_category FROM runs WHERE id=%s", (run_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "failed"
        assert row["failure_category"] == "build_failure"

    asyncio.run(_run())


def test_build_handler_config_failure_sets_run_failed_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            builder = _FakeBuilder(error=ErrorCategory.CONFIGURATION_ERROR)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_handlers.build_handler(conn, job, builder)
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
                result = await runs_handlers.build_handler(conn, job, builder)
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
                    await runs_handlers.build_handler(conn, job, crashing)
            # The failure drove the Run terminal (failed); a real lease-lapse crash would not.
            # Reset to running to model a crash that left no ledger row but the Run still running.
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET state='running', failure_category=NULL WHERE id=%s", (run_id,)
                )
            ok = _FakeBuilder()
            async with pool.connection() as conn:
                await runs_handlers.build_handler(conn, job, ok)
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
    runs_handlers.register_handlers(
        registry, builder=_FakeBuilder(), installer=_FakeInstaller(), booter=_FakeBooter()
    )
    assert registry.get(JobKind.BUILD) is not None


def test_register_handlers_requires_resolver_or_run_ports() -> None:
    registry = HandlerRegistry()
    with pytest.raises(RuntimeError, match="resolver or explicit run ports"):
        runs_handlers.register_handlers(registry)


# --- runs.install / runs.boot (install + boot plane, #19) ----------------------------

from kdive.domain.capture import CaptureMethod  # noqa: E402
from kdive.providers.ports import Booter, Installer  # noqa: E402

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
    await _seed_build_ledger(
        pool, run_id, cmdline=(build_profile or _SUCCEEDED_BUILD).get("cmdline")
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
    await _seed_build_ledger(pool, str(run.id), cmdline=_SUCCEEDED_BUILD.get("cmdline"))
    return str(run.id)


async def _record_install_step(pool: AsyncConnectionPool, run_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'install', 'succeeded', '{}'::jsonb)",
            (run_id,),
        )


async def _set_expected_boot_failure(
    pool: AsyncConnectionPool, run_id: str, pattern: str = "__d_lookup|Oops"
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET expected_boot_failure=%s WHERE id=%s",
            (Jsonb({"kind": "console_crash", "pattern": pattern}), run_id),
        )


async def _seed_build_ledger(
    pool: AsyncConnectionPool, run_id: str, *, cmdline: str | None
) -> None:
    """Record a (run_id, 'build') ledger row, optionally carrying the resolved cmdline."""
    result: dict[str, Any] = {
        "kernel_ref": f"local/runs/{run_id}/kernel",
        "debuginfo_ref": f"local/runs/{run_id}/vmlinux",
        "build_id": "abcdef0123456789",
    }
    if cmdline is not None:
        result["cmdline"] = cmdline
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run_id, Jsonb(result)),
        )


async def _install(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await install_run(pool, ctx, run_id)


async def _boot(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> Any:
    return await boot_run(pool, ctx, run_id)


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


def test_cmdline_default_is_kdump_reserving_for_kdump(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool, build_profile={"schema_version": 1})
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await run_steps.cmdline_for(conn, run, CaptureMethod.KDUMP)
            assert "crashkernel=" in cmdline
            assert "root=/dev/vda" in cmdline  # the platform injects the root device

    asyncio.run(_run())


def test_cmdline_default_omits_crashkernel_for_non_kdump(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool, build_profile={"schema_version": 1})
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await run_steps.cmdline_for(conn, run, CaptureMethod.CONSOLE)
            assert "crashkernel=" not in cmdline
            assert "root=/dev/vda" in cmdline

    asyncio.run(_run())


def test_cmdline_appends_ledger_debug_args_after_the_required_base(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={"schema_version": 1, "cmdline": "dhash_entries=1"},
            )
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await run_steps.cmdline_for(conn, run, CaptureMethod.KDUMP)
            # The platform-required args lead; the agent's debug args are appended after them.
            assert cmdline == "console=ttyS0 root=/dev/vda crashkernel=256M dhash_entries=1"

    asyncio.run(_run())


def test_install_nonkdump_system_admits_cmdline_without_crashkernel(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"}
            )  # bare System (default seed profile) => method console
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())


def test_install_kdump_system_admits_without_agent_crashkernel(migrated_url: str) -> None:
    # The platform injects crashkernel for a kdump System (ADR-0061), so the agent need not
    # supply it — a build whose cmdline carries only debug args still admits.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())


def test_runs_get_advertises_the_system_required_cmdline(migrated_url: str) -> None:
    # The agent reads the platform-required args off runs.get and appends its debug args without
    # clobbering root=/console (ADR-0061).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, provisioning_profile=_profile_dump(crashkernel="256M")
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["required_cmdline"] == "console=ttyS0 root=/dev/vda crashkernel=256M"

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
            RunPayload(run_id=run_id),
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
                result = await runs_handlers.install_handler(conn, job, installer)
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
                await runs_handlers.install_handler(conn, job, installer)
            async with pool.connection() as conn:
                await runs_handlers.install_handler(conn, job, installer)
        assert len(installer.calls) == 1  # built once

    asyncio.run(_run())


class _SlowInstaller:
    """An installer blocked by the test while the first dispatch owns the step claim."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.entered = threading.Event()
        self.release = threading.Event()

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
        self.calls.append(run_id)
        self.entered.set()
        assert self.release.wait(timeout=5), "test did not release the installer"


def test_install_handler_concurrent_dispatch_invokes_once(migrated_url: str) -> None:
    # Two concurrent dispatches of the SAME install job (the queue's at-least-once delivery)
    # on distinct connections: the run_steps running claim serializes them, so the installer
    # runs once and exactly one ledger row is written.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _SlowInstaller()

            async def _dispatch() -> None:
                async with pool.connection() as conn:
                    await runs_handlers.install_handler(conn, job, installer)

            first = asyncio.create_task(_dispatch())
            assert await asyncio.to_thread(installer.entered.wait, 5)
            second = asyncio.create_task(_dispatch())
            installer.release.set()
            await asyncio.gather(first, second)
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
        assert len(installer.calls) == 1  # the running claim prevents a double redefine
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
                    await runs_handlers.install_handler(conn, job, installer)
            assert caught.value.category is ErrorCategory.INSTALL_FAILURE
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
            nstate = await _count(
                pool, "SELECT count(*) AS n FROM runs WHERE id=%s AND state='succeeded'", (run_id,)
            )
        assert nsteps == 0  # no install ledger row on failure (the build row is expected)
        assert nstate == 1  # Run still succeeded

    asyncio.run(_run())


def test_install_handler_cleanup_failure_preserves_provider_category(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail_cleanup(*_args: object) -> None:
        raise RuntimeError("cleanup failed")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller(error=ErrorCategory.INSTALL_FAILURE)
            monkeypatch.setattr(runs_handlers, "abandon_run_step", _fail_cleanup)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.install_handler(conn, job, installer)

        assert caught.value.category is ErrorCategory.INSTALL_FAILURE

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
                    await runs_handlers.install_handler(conn, job, installer)
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
                result = await runs_handlers.boot_handler(conn, job, booter)
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
                await runs_handlers.boot_handler(conn, job, booter)
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(conn, job, booter)
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
                    await runs_handlers.boot_handler(conn, job, booter)
            assert caught.value.category is category
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert nsteps == 0  # no ledger row on failure

    asyncio.run(_run())


def test_boot_handler_cleanup_failure_preserves_provider_category(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail_cleanup(*_args: object) -> None:
        raise RuntimeError("cleanup failed")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.BOOT_TIMEOUT)
            monkeypatch.setattr(runs_handlers, "abandon_run_step", _fail_cleanup)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.boot_handler(conn, job, booter)

        assert caught.value.category is ErrorCategory.BOOT_TIMEOUT

    asyncio.run(_run())


def test_register_handlers_binds_install_and_boot() -> None:
    registry = HandlerRegistry()
    runs_handlers.register_handlers(
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
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            (tmp_path / f"{sid}.log").write_bytes(b"[    0.0] KDIVE-BUSYBOX-READY\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()  # clean success, no error
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(conn, job, booter)
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
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

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
                    await runs_handlers.boot_handler(conn, job, booter)
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s",
                ("%/console",),
            )
        assert n == 1

    asyncio.run(_run())


def test_boot_handler_records_expected_crash_observed(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id)
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            (tmp_path / f"{sid}.log").write_bytes(b"Kernel panic\nRIP: __d_lookup+0x1\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.READINESS_FAILURE)
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(conn, job, booter)
            assert result == run_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, result FROM run_steps WHERE run_id=%s AND step='boot'",
                    (run_id,),
                )
                step = await cur.fetchone()
                await cur.execute("SELECT state FROM systems WHERE id=%s", (sid,))
                system = await cur.fetchone()
        assert step is not None
        assert step["state"] == "succeeded"
        assert step["result"]["boot_outcome"] == "expected_crash_observed"
        assert step["result"]["expectation_matched"] is True
        assert step["result"]["evidence_kind"] == "console"
        assert step["result"]["evidence_artifact_id"]
        assert system is not None
        assert system["state"] == "ready"

    asyncio.run(_run())


def test_expected_crash_observed_system_can_host_next_run(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id)
            await _record_install_step(pool, run_id)
            sys_id = await _system_id_of(pool, run_id)
            (tmp_path / f"{sys_id}.log").write_bytes(b"Kernel panic\nRIP: __d_lookup+0x1\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.READINESS_FAILURE)
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(conn, job, booter)

            inv_id = await _seed_investigation(pool)
            resp = await _create(pool, _ctx(), inv_id, sys_id)

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id=%s", (sys_id,))
                system = await cur.fetchone()
        assert resp.status == "created"
        assert system is not None
        assert system["state"] == "ready"

    asyncio.run(_run())


def test_boot_handler_expected_crash_requires_matching_console(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id, pattern="__d_lookup")
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            (tmp_path / f"{sid}.log").write_bytes(b"Kernel panic\nRIP: other_symbol\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.READINESS_FAILURE)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.boot_handler(conn, job, booter)
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert caught.value.category is ErrorCategory.READINESS_FAILURE
        assert nsteps == 0

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
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()  # clean success, but no console file was written
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(conn, job, booter)
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
    from kdive.mcp.tools.catalog.artifacts_reads import artifacts_list

    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            system_id = await _system_id_of(pool, run_id)
            (tmp_path / f"{system_id}.log").write_bytes(b"[    0.0] KDIVE-BUSYBOX-READY\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter()  # clean success
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(conn, job, booter)
            assert result == run_id

            # artifacts_list must return the console as a redacted artifact envelope.
            listed = await artifacts_list(pool, _ctx(), system_id=system_id)

        items = listed.items
        assert len(items) == 1
        console = items[0]
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
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # First boot of the System registers the console row at the first object's etag.
            run1 = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run1)
            sid = await _system_id_of(pool, run1)
            (tmp_path / f"{sid}.log").write_bytes(b"[    0.0] FIRST-BOOT-MARKER ready\n")
            job1 = await _enqueue_job(pool, JobKind.BOOT, run1, "boot")
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(conn, job1, _FakeBooter())

            # Second boot of the SAME System (new Run): the host console log is overwritten
            # with different content, so put_artifact rewrites the object at the same key.
            run2 = await _seed_succeeded_run_on_system(pool, sid)
            await _record_install_step(pool, run2)
            (tmp_path / f"{sid}.log").write_bytes(b"[    0.0] SECOND-BOOT-MARKER oops\n")
            job2 = await _enqueue_job(pool, JobKind.BOOT, run2, "boot")
            async with pool.connection() as conn:
                await runs_handlers.boot_handler(conn, job2, _FakeBooter())

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

    section: dict[str, Any] = {"rootfs": {"kind": "local", "path": "/img"}}
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


def _profile_dump_sized(*, vcpu: int, memory_mb: int, disk_gb: int) -> dict[str, Any]:
    """A real provisioning-profile dump with explicit sizing (the full-custom reuse case)."""
    from kdive.profiles.provisioning import ProvisioningProfile

    return ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": vcpu,
            "memory_mb": memory_mb,
            "disk_gb": disk_gb,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": "/img"}}},
        }
    ).model_dump(by_alias=True)


def test_install_method_kdump_when_crashkernel_set() -> None:
    system = _system_with_profile(_profile_dump(crashkernel="256M"))
    assert run_steps.install_method_for(system) is CaptureMethod.KDUMP


def test_install_method_gdbstub_when_flag_set() -> None:
    system = _system_with_profile(_profile_dump(debug={"gdbstub": True}))
    assert run_steps.install_method_for(system) is CaptureMethod.GDBSTUB


def test_install_method_host_dump_when_preserve_on_crash() -> None:
    system = _system_with_profile(_profile_dump(debug={"preserve_on_crash": True}))
    assert run_steps.install_method_for(system) is CaptureMethod.HOST_DUMP


def test_install_method_console_for_bare_system() -> None:
    system = _system_with_profile(_profile_dump())
    assert run_steps.install_method_for(system) is CaptureMethod.CONSOLE


def test_install_method_rejects_partial_profile() -> None:
    system = _system_with_profile({"schema_version": 1})
    with pytest.raises(CategorizedError) as exc:
        run_steps.install_method_for(system)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_install_method_rejects_attribute_spelling() -> None:
    system = _system_with_profile({"provider": {"local_libvirt": {"crashkernel": "256M"}}})
    with pytest.raises(CategorizedError) as exc:
        run_steps.install_method_for(system)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


async def _record_build_ledger(
    pool: AsyncConnectionPool, run_id: str, result: dict[str, Any]
) -> None:
    # Upsert: a succeeded-run seed (`_seed_build_ledger`) already inserts a build row, so a test
    # that needs a specific build result (e.g. an initrd_ref) overwrites it rather than no-op'ing.
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) "
            "ON CONFLICT (run_id, step) DO UPDATE SET result = EXCLUDED.result",
            (run_id, Jsonb(result)),
        )


def test_install_handler_forwards_console_method_for_bare_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)  # bare System => console
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(conn, job, installer)
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
                await runs_handlers.install_handler(conn, job, installer)
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
                await runs_handlers.install_handler(conn, job, installer)
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
                await runs_handlers.install_handler(conn, job, installer)
        assert installer.calls[0][5] is None

    asyncio.run(_run())


def test_install_handler_forwards_ledger_cmdline_to_installer(migrated_url: str) -> None:
    """The dhash_entries=1 trigger recorded in the build ledger reaches install() (#128)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "dhash_entries=1"}
            )  # bare System => console method; the debug arg is appended to the required base
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(conn, job, installer)
        assert installer.calls[0][3] == "console=ttyS0 root=/dev/vda dhash_entries=1"

    asyncio.run(_run())


def test_install_handler_forwards_default_cmdline_when_ledger_has_none(migrated_url: str) -> None:
    """A succeeded run with no ledger cmdline installs the method default, not a stale value."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile=copy.deepcopy(_VALID_BUILD),  # no cmdline key
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_handlers.install_handler(conn, job, installer)
        assert installer.calls[0][3] == "console=ttyS0 root=/dev/vda"  # required base only

    asyncio.run(_run())


@pytest.mark.parametrize(
    "cmdline",
    ["dhash_entries=1 panic_on_oops=1", "panic_on_oops=1"],
)
def test_install_debug_args_pass_boundary(migrated_url: str, cmdline: str) -> None:
    # The platform injects console/root; agent-supplied debug args carry no crashkernel= and
    # a bare (console) System admits them through runs.install.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": cmdline}
            )
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())
