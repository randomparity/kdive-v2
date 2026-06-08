"""systems.* tool + handler tests — handlers called directly with injected pool + provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.components.references import ComponentRef
from kdive.db import upload_manifest
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import (
    Allocation,
    Investigation,
    Job,
    JobKind,
    Run,
    Sensitivity,
    System,
)
from kdive.domain.state import AllocationState, InvestigationState, RunState, SystemState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.systems.admin import SystemAdminHandlers, teardown_system
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers, get_system
from kdive.planes import systems as systems_handlers
from kdive.profiles.provisioning import RootfsSource
from kdive.providers.local_libvirt.materialize import materialize_rootfs_base
from kdive.security.audit import args_digest
from kdive.security.rbac import AuthorizationError, Role
from kdive.store.objectstore import ArtifactWriteRequest, ObjectStore, artifact_key
from tests.mcp.systems_support import (
    SYSTEM_ADMIN_HANDLERS as _SYSTEM_ADMIN_HANDLERS,
)
from tests.mcp.systems_support import (
    SYSTEM_PROVISION_HANDLERS as _SYSTEM_PROVISION_HANDLERS,
)
from tests.mcp.systems_support import (
    TEST_COMPONENT_SOURCES as _TEST_COMPONENT_SOURCES,
)
from tests.mcp.systems_support import (
    TEST_DT as _DT,
)
from tests.mcp.systems_support import (
    FakeProvisioning as _FakeProvisioning,
)
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    define_system as _define,
)
from tests.mcp.systems_support import (
    enqueue_provision as _enqueue_provision,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provisioning_profile as _profile,
)
from tests.mcp.systems_support import (
    upload_profile as _upload_profile,
)


async def _seed_system(pool: AsyncConnectionPool, alloc_id: str, state: SystemState) -> str:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=_profile(),
            ),
        )
    return str(system.id)


def test_get_own_system_returns_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await get_system(pool, _ctx(), sys_id)
        assert resp.object_id == sys_id
        assert resp.status == "ready"

    asyncio.run(_run())


def test_get_system_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            with pytest.raises(AuthorizationError):
                await get_system(pool, _ctx(role=None), sys_id)

    asyncio.run(_run())


def test_get_failed_system_renders_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.FAILED)
            resp = await get_system(pool, _ctx(), sys_id)
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await get_system(pool, _ctx(projects=("other",)), sys_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await get_system(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- shared fakes/helpers for provision/teardown handler + tool tests ---------------------


async def _enqueue_teardown(pool: AsyncConnectionPool, system_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            {"system_id": system_id},
            {"principal": "system:reconciler", "agent_session": None, "project": "proj"},
            f"{system_id}:teardown",
        )


async def _provision(
    pool: AsyncConnectionPool, ctx: RequestContext, alloc_id: str, profile: dict[str, Any]
):
    return await _SYSTEM_PROVISION_HANDLERS.provision_system(
        pool,
        ctx,
        allocation_id=alloc_id,
        profile=profile,
    )


def _noop_rootfs_validator(_: RootfsSource) -> None:
    return None


def _provision_handlers(
    rootfs_validator: Callable[[RootfsSource], None] = _noop_rootfs_validator,
) -> SystemProvisionHandlers:
    return SystemProvisionHandlers(_TEST_COMPONENT_SOURCES, rootfs_validator)


def _admin_handlers(
    rootfs_validator: Callable[[RootfsSource], None] = _noop_rootfs_validator,
) -> SystemAdminHandlers:
    return SystemAdminHandlers(_TEST_COMPONENT_SOURCES, rootfs_validator)


async def _provision_defined(pool: AsyncConnectionPool, ctx: RequestContext, system_id: str):
    return await _SYSTEM_PROVISION_HANDLERS.provision_defined_system(
        pool,
        ctx,
        system_id=system_id,
    )


def _artifact_rootfs_profile() -> dict[str, Any]:
    profile = _profile()
    profile["provider"]["local-libvirt"]["rootfs"] = {
        "kind": "artifact",
        "artifact_id": str(uuid4()),
    }
    return profile


def _local_rootfs_profile(path: Path) -> dict[str, Any]:
    profile = _profile()
    profile["provider"]["local-libvirt"]["rootfs"] = {"kind": "local", "path": str(path)}
    return profile


def _rootfs_validator(allowed_root: Path) -> Callable[[RootfsSource], None]:
    cache_dir = allowed_root.parent / "cache"

    def _validate(rootfs: RootfsSource) -> None:
        materialize_rootfs_base(
            cast(ComponentRef, rootfs),
            allowed_roots=[allowed_root],
            cache_dir=cache_dir,
            project="proj",
            component_store=None,
            object_store=None,
        )

    return _validate


def _failing_rootfs_validator(calls: list[ComponentRef]) -> Callable[[RootfsSource], None]:
    def _validate(rootfs: RootfsSource) -> None:
        calls.append(cast(ComponentRef, rootfs))
        raise AssertionError("rootfs validator must not run before authorization")

    return _validate


# --- systems.provision tool ----------------------------------------------------------------


def test_provision_mints_system_active_allocation_and_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
            assert resp.status == "queued"
            assert resp.data["system_id"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, allocation_id FROM systems")
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{alloc_id}:provision",),
                )
                job_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "provisioning"
        assert str(sys_row["allocation_id"]) == alloc_id
        assert alloc_row is not None and alloc_row["state"] == "active"
        assert job_row is not None and job_row["n"] == 1

    asyncio.run(_run())


def test_provision_retry_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            first = await _provision(pool, _ctx(), alloc_id, _profile())
            second = await _provision(pool, _ctx(), alloc_id, _profile())
            assert first.object_id == second.object_id  # same job
            assert first.data["system_id"] == second.data["system_id"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'granted->active'"
                )
                audit_n = await cur.fetchone()
        assert sys_n is not None and sys_n["n"] == 1  # one System
        assert audit_n is not None and audit_n["n"] == 1  # active flip audited once

    asyncio.run(_run())


def test_provision_terminal_existing_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "torn_down"

    asyncio.run(_run())


def test_provision_non_granted_allocation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASING)
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "releasing"

    asyncio.run(_run())


async def _second_granted_allocation(pool: AsyncConnectionPool) -> str:
    # A second granted allocation on the same registered resource (same project), so two
    # provisions can race the per-project system quota without a host-cap denial.
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT id FROM resources LIMIT 1")
        res_row = await cur.fetchone()
        assert res_row is not None
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res_row["id"],
                state=AllocationState.GRANTED,
            ),
        )
    return str(alloc.id)


def test_provision_at_system_quota_is_quota_exceeded_no_writes(migrated_url: str) -> None:
    # max_concurrent_systems=1: the first provision fills the quota; the second (a
    # distinct granted allocation, same project) is denied quota_exceeded and writes
    # neither a System nor a job, leaving the allocation granted.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            first_alloc = await _granted_allocation(pool, systems_quota=1)
            await _provision(pool, _ctx(), first_alloc, _profile())
            second_alloc = await _second_granted_allocation(pool)
            resp = await _provision(pool, _ctx(), second_alloc, _profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (second_alloc,))
                alloc_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "quota_exceeded"
        assert sys_n is not None and sys_n["n"] == 1  # only the first
        assert job_n is not None and job_n["n"] == 1  # only the first
        assert alloc_row is not None and alloc_row["state"] == "granted"  # untouched

    asyncio.run(_run())


def test_provision_no_quota_row_is_quota_exceeded(migrated_url: str) -> None:
    # Fail-closed: a project with no quota row cannot provision (ADR-0007 §4).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM quotas WHERE project = %s", ("proj",))
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "quota_exceeded"
        assert sys_n is not None and sys_n["n"] == 0

    asyncio.run(_run())


def test_provision_quota_counts_only_non_terminal_systems(migrated_url: str) -> None:
    # A torn_down System does not occupy a quota slot; with quota=1 and one terminal
    # System already present, a fresh provision still succeeds.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            spent_alloc = await _granted_allocation(pool, systems_quota=1)
            await _seed_system(pool, spent_alloc, SystemState.TORN_DOWN)
            fresh_alloc = await _second_granted_allocation(pool)
            resp = await _provision(pool, _ctx(), fresh_alloc, _profile())
        assert resp.status == "queued"

    asyncio.run(_run())


def test_provision_unknown_domain_param_is_config_error_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            bad = _profile()
            bad["provider"]["local-libvirt"]["domain_xml_params"]["bogus"] = "x"
            resp = await _provision(pool, _ctx(), alloc_id, bad)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0  # validated before any write

    asyncio.run(_run())


def test_provision_rejects_unsupported_artifact_rootfs_before_system_and_job(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _provision(pool, _ctx(), alloc_id, _artifact_rootfs_profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0
        assert job_n is not None and job_n["n"] == 0
        assert alloc_row is not None and alloc_row["state"] == "granted"

    asyncio.run(_run())


def test_provision_rejects_local_rootfs_outside_allowed_root_before_system_and_job(
    migrated_url: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _provision_handlers(_rootfs_validator(allowed_root)).provision_system(
                pool,
                _ctx(),
                allocation_id=alloc_id,
                profile=_local_rootfs_profile(outside),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0
        assert job_n is not None and job_n["n"] == 0
        assert alloc_row is not None and alloc_row["state"] == "granted"

    asyncio.run(_run())


def test_provision_without_operator_raises(migrated_url: str) -> None:
    from kdive.security.rbac import AuthorizationError

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            with pytest.raises(AuthorizationError):
                await _provision(pool, _ctx(Role.VIEWER), alloc_id, _profile())

    asyncio.run(_run())


def test_provision_viewer_denied_before_provider_rootfs_validation(
    migrated_url: str, tmp_path: Path
) -> None:
    calls: list[ComponentRef] = []
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            with pytest.raises(AuthorizationError):
                await _provision_handlers(_failing_rootfs_validator(calls)).provision_system(
                    pool,
                    _ctx(Role.VIEWER),
                    allocation_id=alloc_id,
                    profile=_local_rootfs_profile(outside),
                )

    asyncio.run(_run())
    assert calls == []


def test_provision_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _provision(pool, _ctx(), "not-a-uuid", _profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- provision handler ---------------------------------------------------------------------


def test_provision_handler_drives_system_ready(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                result = await systems_handlers.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.provisioned == [UUID(sys_id)]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, domain_name FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "ready"
        assert row["domain_name"] == f"kdive-{sys_id}"

    asyncio.run(_run())


def test_provision_handler_stamps_active_started_at_on_ready(migrated_url: str) -> None:
    """The provisioning->ready edge stamps the allocation's active_started_at (ADR-0007 §3).

    The billing interval opens when the first System reaches ready; a null
    active_started_at would reconcile every active allocation at active_hours = 0.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            # The genuine path flips the allocation granted->active at provision time.
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.ACTIVE)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, _FakeProvisioning())
                alloc = await ALLOCATIONS.get(conn, UUID(alloc_id))
            assert alloc is not None
            assert alloc.active_started_at is not None  # billing interval opened

    asyncio.run(_run())


def test_provision_handler_does_not_restamp_active_started_at(migrated_url: str) -> None:
    """A second System reaching ready (or a re-run) leaves the original interval start.

    active_started_at is first-write-wins so the interval anchors on the first ready,
    never sliding forward when another System on the same allocation provisions later.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            anchor = datetime(2026, 1, 1, 12, tzinfo=UTC)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.ACTIVE)
                await conn.execute(
                    "UPDATE allocations SET active_started_at = %s WHERE id = %s",
                    (anchor, UUID(alloc_id)),
                )
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, _FakeProvisioning())
                alloc = await ALLOCATIONS.get(conn, UUID(alloc_id))
            assert alloc is not None
            assert alloc.active_started_at == anchor  # unchanged by the second ready

    asyncio.run(_run())


def test_provision_handler_retry_on_ready_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, prov)
            assert prov.provisioned == []  # already up; provider not called again

    asyncio.run(_run())


def test_provision_handler_provider_failure_sets_system_failed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning(provision_error=True)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await systems_handlers.provision_handler(conn, job, prov)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, domain_name FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "failed"
        assert row["domain_name"] is None

    asyncio.run(_run())


def test_provision_handler_failure_when_already_terminal_preserves_category(
    migrated_url: str,
) -> None:
    # The provider fails AND a concurrent teardown already drove the System torn_down. The
    # failed-branch transition is illegal (torn_down->failed), but the handler tolerates that
    # and re-raises the original PROVISIONING_FAILURE (not the masking IllegalTransition).
    class _FailAfterTerminal(_FakeProvisioning):
        def __init__(self, url: str) -> None:
            super().__init__(provision_error=True)
            self._url = url

        def provision(self, system_id: UUID, profile: Any) -> str:
            with psycopg.connect(self._url, autocommit=True) as c:
                c.execute("UPDATE systems SET state = 'torn_down' WHERE id = %s", (system_id,))
            return super().provision(system_id, profile)  # raises PROVISIONING_FAILURE

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FailAfterTerminal(migrated_url)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await systems_handlers.provision_handler(conn, job, prov)
            assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"  # left terminal, not failed

    asyncio.run(_run())


def test_provision_handler_terminal_system_reaps_without_provisioning(migrated_url: str) -> None:
    # A provision job whose System is already terminal on entry does not re-provision, but it
    # idempotently reaps the deterministic domain — the durable retry point for a compensation
    # that failed on an earlier run (NULL domain_name -> deterministic name).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                result = await systems_handlers.provision_handler(conn, job, prov)
        assert result == sys_id
        assert prov.provisioned == []  # not re-provisioned
        assert prov.torn_down == [f"kdive-{sys_id}"]  # but the domain is idempotently reaped

    asyncio.run(_run())


def test_provision_handler_failed_compensation_retries_reap_on_requeue(migrated_url: str) -> None:
    # provision creates the domain, a concurrent teardown drives torn_down, and the finalize
    # compensation teardown fails transiently (handler raises -> job requeues). The requeue must
    # re-attempt the reap from the terminal-entry path rather than leaking the created domain.
    class _RacingThenTeardownFails(_FakeProvisioning):
        def __init__(self, url: str) -> None:
            super().__init__()
            self._url = url
            self._fail_next_teardown = True

        def provision(self, system_id: UUID, profile: Any) -> str:
            name = super().provision(system_id, profile)
            with psycopg.connect(self._url, autocommit=True) as c:
                c.execute("UPDATE systems SET state = 'torn_down' WHERE id = %s", (system_id,))
            return name

        def teardown(self, domain_name: str) -> None:
            if self._fail_next_teardown:
                self._fail_next_teardown = False
                raise CategorizedError("transient", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
            super().teardown(domain_name)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _RacingThenTeardownFails(migrated_url)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):  # finalize compensation teardown failed
                    await systems_handlers.provision_handler(conn, job, prov)
            assert prov.torn_down == []  # nothing reaped yet — the domain is still leaked
            async with pool.connection() as conn:  # requeue
                result = await systems_handlers.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.provisioned == [UUID(sys_id)]  # provider NOT re-invoked on the requeue
            assert prov.torn_down == [f"kdive-{sys_id}"]  # the created domain is finally reaped

    asyncio.run(_run())


def test_provision_handler_missing_row_is_infra_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            job = await _enqueue_provision(pool, str(uuid4()), alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await systems_handlers.provision_handler(conn, job, prov)
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(_run())


def test_provision_handler_superseded_midflight_tears_down_created_domain(
    migrated_url: str,
) -> None:
    class _RacingProvisioning(_FakeProvisioning):
        """Drives the System torn_down before returning — a deterministic mid-flight race."""

        def __init__(self, url: str) -> None:
            super().__init__()
            self._url = url

        def provision(self, system_id: UUID, profile: Any) -> str:
            name = super().provision(system_id, profile)
            with psycopg.connect(self._url, autocommit=True) as c:
                c.execute("UPDATE systems SET state = 'torn_down' WHERE id = %s", (system_id,))
            return name

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _RacingProvisioning(migrated_url)
            async with pool.connection() as conn:
                result = await systems_handlers.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.torn_down == [f"kdive-{sys_id}"]  # the created domain was cleaned up
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"  # never resurrected to ready

    asyncio.run(_run())


def test_provision_handler_concurrent_same_job_ready_does_not_tear_down(migrated_url: str) -> None:
    # A lease lapse double-run: another worker already finalized this provision to `ready`.
    # The finalize must NOT tear down the (live) domain — `ready` is not a teardown.
    class _RacingToReady(_FakeProvisioning):
        def __init__(self, url: str) -> None:
            super().__init__()
            self._url = url

        def provision(self, system_id: UUID, profile: Any) -> str:
            name = super().provision(system_id, profile)
            with psycopg.connect(self._url, autocommit=True) as c:
                c.execute(
                    "UPDATE systems SET state = 'ready', domain_name = %s WHERE id = %s",
                    (name, system_id),
                )
            return name

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _RacingToReady(migrated_url)
            async with pool.connection() as conn:
                result = await systems_handlers.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.torn_down == []  # the live domain was left alone
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "ready"

    asyncio.run(_run())


# --- upload-rootfs artifacts commit (ADR-0048 §6) ------------------------------------------
#
# These drive provision_handler with a directly-seeded PROVISIONING upload profile to unit-
# test the worker-side provisioning->ready commit in isolation. The full lane is reachable
# end-to-end via systems.define + artifacts.create_system_upload + systems.provision_defined
# (#111); see
# tests/integration/test_systems_define_upload_provision.py for that reachability proof.


async def _seed_system_with_profile(
    pool: AsyncConnectionPool, alloc_id: str, state: SystemState, profile: dict[str, Any]
) -> str:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=profile,
            ),
        )
    return str(system.id)


def test_provision_handler_commits_uploaded_rootfs_artifact(
    migrated_url: str, minio_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An upload-kind rootfs whose object is present: the provisioning->ready transition
    # writes one systems-owned write-once artifacts row and deletes the upload manifest
    # (so the reaper exempts the object).
    monkeypatch.setattr(systems_handlers, "object_store_from_env", lambda: minio_store)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system_with_profile(
                pool, alloc_id, SystemState.PROVISIONING, _upload_profile()
            )
            key = artifact_key("local", "systems", sys_id, "rootfs")
            minio_store.put_artifact(
                ArtifactWriteRequest(
                    tenant="local",
                    owner_kind="systems",
                    owner_id=sys_id,
                    name="rootfs",
                    data=b"rootfs-image-bytes",
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class="rootfs",
                )
            )
            async with pool.connection() as conn:
                await upload_manifest.replace_manifest(
                    conn,
                    owner_kind="systems",
                    owner_id=UUID(sys_id),
                    prefix=f"local/systems/{sys_id}/",
                    entries=[upload_manifest.ManifestEntry("rootfs", "sha256:x", 18)],
                    ttl=timedelta(hours=1),
                )
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, _FakeProvisioning())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT owner_kind, object_key, sensitivity, retention_class "
                    "FROM artifacts WHERE owner_id = %s",
                    (sys_id,),
                )
                art_rows = await cur.fetchall()
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "systems", UUID(sys_id))
        assert sys_row is not None and sys_row["state"] == "ready"
        assert len(art_rows) == 1  # exactly one write-once row
        assert art_rows[0]["owner_kind"] == "systems"
        assert art_rows[0]["object_key"] == key
        assert art_rows[0]["sensitivity"] == "sensitive"
        assert art_rows[0]["retention_class"] == "rootfs"
        assert manifest is None  # the upload manifest was deleted (reaper exempts the object)

    asyncio.run(_run())


def test_provision_handler_absent_uploaded_rootfs_fails_config_error(
    migrated_url: str, minio_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An upload-kind rootfs whose object was never uploaded: the commit raises
    # configuration_error inside the ready transition, which rolls back — the System stays
    # provisioning (a retry re-checks) and no artifacts row is written.
    monkeypatch.setattr(systems_handlers, "object_store_from_env", lambda: minio_store)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system_with_profile(
                pool, alloc_id, SystemState.PROVISIONING, _upload_profile()
            )
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await systems_handlers.provision_handler(conn, job, prov)
            assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
            # The System rolls back to provisioning (not terminal), so the terminal-teardown
            # compensation deliberately does NOT fire — the started domain is left in place for
            # an idempotent retry.
            assert prov.torn_down == []
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM artifacts WHERE owner_id = %s", (sys_id,)
                )
                art_n = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "provisioning"  # rolled back
        assert art_n is not None and art_n["n"] == 0

    asyncio.run(_run())


# --- systems.teardown tool + handler -------------------------------------------------------


async def _teardown(pool: AsyncConnectionPool, ctx: RequestContext, system_id: str):
    return await teardown_system(pool, ctx, system_id)


def _teardown_profile() -> dict[str, Any]:
    p = _profile()
    p["provider"]["local-libvirt"]["destructive_ops"] = ["teardown"]
    return p


async def _scoped_teardown_allocation(pool: AsyncConnectionPool) -> str:
    alloc_id = await _granted_allocation(pool)
    async with pool.connection() as conn:
        await conn.execute(
            'UPDATE allocations SET capability_scope = \'{"destructive_ops": ["teardown"]}\' '
            "WHERE id = %s",
            (alloc_id,),
        )
    return alloc_id


async def _seed_teardown_system(
    pool: AsyncConnectionPool,
    alloc_id: str,
    state: SystemState,
    *,
    profile: dict[str, Any] | None = None,
) -> str:
    sys_id = await _seed_system(pool, alloc_id, state)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE systems SET provisioning_profile = %s WHERE id = %s",
            (Jsonb(profile or _teardown_profile()), sys_id),
        )
    return sys_id


def test_teardown_tool_enqueues_job(migrated_url: str) -> None:
    # teardown is a destructive-administration op: admin-only plus scope/profile gate.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_teardown_allocation(pool)
            sys_id = await _seed_teardown_system(pool, alloc_id, SystemState.READY)
            resp = await _teardown(pool, _ctx(Role.ADMIN), sys_id)
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s", (f"{sys_id}:teardown",)
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_teardown_tool_already_torn_down_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_teardown_allocation(pool)
            sys_id = await _seed_teardown_system(pool, alloc_id, SystemState.TORN_DOWN)
            resp = await _teardown(pool, _ctx(Role.ADMIN), sys_id)
            assert resp.status == "torn_down"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs")
                row = await cur.fetchone()
        assert row is not None and row["n"] == 0

    asyncio.run(_run())


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
def test_teardown_tool_below_admin_denied(migrated_url: str, role: Role) -> None:
    # teardown is admin-only: both viewer AND operator are refused (ADR-0037 §2).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_teardown_allocation(pool)
            sys_id = await _seed_teardown_system(pool, alloc_id, SystemState.READY)
            resp = await _teardown(pool, _ctx(role), sys_id)
            # The denied op enqueued no teardown job.
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'teardown'")
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT transition FROM audit_log WHERE transition = 'teardown:denied'"
                )
                audit_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row is not None and row["n"] == 0
        assert audit_row is not None

    asyncio.run(_run())


def test_teardown_tool_without_scope_denied_and_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_teardown_system(pool, alloc_id, SystemState.READY)
            resp = await _teardown(pool, _ctx(Role.ADMIN), sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT args_digest FROM audit_log WHERE transition = 'teardown:denied'"
                )
                audit_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert audit_row is not None
        assert audit_row["args_digest"] == args_digest(
            {"system_id": sys_id, "missing": ["capability_scope"]}
        )

    asyncio.run(_run())


def test_teardown_tool_without_profile_opt_in_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_teardown_allocation(pool)
            sys_id = await _seed_teardown_system(
                pool, alloc_id, SystemState.READY, profile=_profile()
            )
            resp = await _teardown(pool, _ctx(Role.ADMIN), sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT args_digest FROM audit_log WHERE transition = 'teardown:denied'"
                )
                audit_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert audit_row is not None
        assert audit_row["args_digest"] == args_digest(
            {"system_id": sys_id, "missing": ["profile_opt_in"]}
        )

    asyncio.run(_run())


def test_teardown_handler_destroys_and_sets_torn_down(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET domain_name = %s WHERE id = %s", (f"kdive-{sys_id}", sys_id)
                )
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_handlers.teardown_handler(conn, job, prov)
            assert prov.torn_down == [f"kdive-{sys_id}"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"

    asyncio.run(_run())


def test_teardown_handler_provisioning_system_one_transition(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_handlers.teardown_handler(conn, job, prov)
            assert prov.torn_down == [f"kdive-{sys_id}"]  # NULL domain_name -> deterministic name
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"  # provisioning->torn_down (one edge)

    asyncio.run(_run())


def test_teardown_handler_already_torn_down_reattempts_destroy_no_transition(
    migrated_url: str,
) -> None:
    # A re-run on an already-torn_down System makes no state change but STILL attempts the
    # idempotent destroy — so a teardown that failed after committing ->torn_down self-heals
    # on retry rather than leaking the domain.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_handlers.teardown_handler(conn, job, prov)
            assert prov.torn_down == [f"kdive-{sys_id}"]  # idempotent destroy re-attempted
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE object_id = %s "
                    "AND transition LIKE '%%->torn_down'",
                    (sys_id,),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 0  # no transition audited (already torn_down)

    asyncio.run(_run())


# --- systems.reprovision tool + handler ----------------------------------------------------


def _active_allocation_profile() -> dict[str, Any]:
    """A profile that opts reprovision in (the gate's opt-in factor)."""
    p = _profile()
    p["provider"]["local-libvirt"]["destructive_ops"] = ["reprovision"]
    return p


async def _scoped_active_allocation(pool: AsyncConnectionPool) -> str:
    """A granted->active allocation whose capability scope grants reprovision."""
    alloc_id = await _granted_allocation(pool)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE allocations SET state = 'active', "
            'capability_scope = \'{"destructive_ops": ["reprovision"]}\' WHERE id = %s',
            (alloc_id,),
        )
    return alloc_id


async def _seed_ready_system(pool: AsyncConnectionPool, alloc_id: str) -> str:
    sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE systems SET provisioning_profile = %s, domain_name = %s WHERE id = %s",
            (Jsonb(_active_allocation_profile()), f"kdive-{sys_id}", sys_id),
        )
    return sys_id


async def _seed_run(pool: AsyncConnectionPool, sys_id: str, state: RunState) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                title="t",
                state=InvestigationState.ACTIVE,
            ),
        )
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv.id,
                system_id=UUID(sys_id),
                state=state,
                build_profile={},
            ),
        )
    return str(run.id)


async def _reprovision(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: str, profile: dict[str, Any]
):
    return await _SYSTEM_ADMIN_HANDLERS.reprovision_system(
        pool,
        ctx,
        system_id=system_id,
        profile=profile,
    )


def test_reprovision_transitions_ready_to_reprovisioning_and_enqueues_job(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            new_profile = _active_allocation_profile()
            new_profile["vcpu"] = 8
            resp = await _reprovision(pool, _ctx(), sys_id, new_profile)
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, provisioning_profile FROM systems WHERE id = %s", (sys_id,)
                )
                sys_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM allocations")
                alloc_n = await cur.fetchone()
                await cur.execute(
                    "SELECT kind FROM jobs WHERE payload->>'system_id' = %s", (sys_id,)
                )
                job_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "reprovisioning"
        assert sys_row["provisioning_profile"]["vcpu"] == 8  # new profile applied to the row
        assert sys_n is not None and sys_n["n"] == 1  # no new System row
        assert alloc_n is not None and alloc_n["n"] == 1  # no new Allocation row
        assert job_row is not None and job_row["kind"] == "reprovision"

    asyncio.run(_run())


def test_reprovision_same_profile_dedups(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            p = _active_allocation_profile()
            first = await _reprovision(pool, _ctx(), sys_id, p)
            second = await _reprovision(pool, _ctx(), sys_id, p)
            assert first.object_id == second.object_id  # same job (dedup on digest)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'reprovision'")
                n = await cur.fetchone()
        assert n is not None and n["n"] == 1  # one reprovision job

    asyncio.run(_run())


def test_reprovision_different_profile_is_new_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            first = await _reprovision(pool, _ctx(), sys_id, _active_allocation_profile())
            # Drive back to ready so a second reprovision is admissible.
            async with pool.connection() as conn:
                await conn.execute("UPDATE systems SET state = 'ready' WHERE id = %s", (sys_id,))
            other = _active_allocation_profile()
            other["memory_mb"] = 8192
            second = await _reprovision(pool, _ctx(), sys_id, other)
            assert first.object_id != second.object_id  # distinct job (distinct digest)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'reprovision'")
                n = await cur.fetchone()
        assert n is not None and n["n"] == 2

    asyncio.run(_run())


def test_reprovision_under_live_run_is_stale_handle(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            await _seed_run(pool, sys_id, RunState.RUNNING)
            resp = await _reprovision(pool, _ctx(), sys_id, _active_allocation_profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "stale_handle"
        assert sys_row is not None and sys_row["state"] == "ready"  # no transition
        assert job_n is not None and job_n["n"] == 0  # no job enqueued

    asyncio.run(_run())


def test_reprovision_with_terminal_run_is_admissible(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            await _seed_run(pool, sys_id, RunState.SUCCEEDED)  # terminal -> does not block
            resp = await _reprovision(pool, _ctx(), sys_id, _active_allocation_profile())
        assert resp.status == "queued"

    asyncio.run(_run())


def test_reprovision_non_ready_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            resp = await _reprovision(pool, _ctx(), sys_id, _active_allocation_profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "provisioning"

    asyncio.run(_run())


def test_reprovision_operator_may_invoke(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            resp = await _reprovision(
                pool, _ctx(Role.OPERATOR), sys_id, _active_allocation_profile()
            )
        assert resp.status == "queued"

    asyncio.run(_run())


def test_reprovision_viewer_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            resp = await _reprovision(pool, _ctx(Role.VIEWER), sys_id, _active_allocation_profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'reprovision:denied'"
                )
                audit_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert sys_row is not None and sys_row["state"] == "ready"  # untouched
        assert audit_n is not None and audit_n["n"] == 1  # the denial is audited

    asyncio.run(_run())


def test_reprovision_viewer_denied_before_provider_rootfs_validation(
    migrated_url: str, tmp_path: Path
) -> None:
    calls: list[ComponentRef] = []
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            profile = _local_rootfs_profile(outside)
            profile["provider"]["local-libvirt"]["destructive_ops"] = ["reprovision"]
            resp = await _admin_handlers(_failing_rootfs_validator(calls)).reprovision_system(
                pool,
                _ctx(Role.VIEWER),
                system_id=sys_id,
                profile=profile,
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert sys_row is not None and sys_row["state"] == "ready"
        assert job_n is not None and job_n["n"] == 0

    asyncio.run(_run())
    assert calls == []


def test_reprovision_without_scope_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)  # no destructive_ops in scope
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE allocations SET state = 'active' WHERE id = %s", (alloc_id,)
                )
            sys_id = await _seed_ready_system(pool, alloc_id)
            resp = await _reprovision(
                pool, _ctx(Role.OPERATOR), sys_id, _active_allocation_profile()
            )
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


def test_reprovision_without_profile_opt_in_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            no_opt_in = _profile()  # no destructive_ops -> opt-in factor fails
            resp = await _reprovision(pool, _ctx(Role.OPERATOR), sys_id, no_opt_in)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


def test_reprovision_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            resp = await _reprovision(
                pool, _ctx(projects=("other",)), sys_id, _active_allocation_profile()
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reprovision_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _reprovision(pool, _ctx(), "not-a-uuid", _active_allocation_profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_reprovision_bad_profile_is_config_error_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            bad = _active_allocation_profile()
            bad["provider"]["local-libvirt"]["domain_xml_params"]["bogus"] = "x"
            resp = await _reprovision(pool, _ctx(), sys_id, bad)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert job_n is not None and job_n["n"] == 0

    asyncio.run(_run())


def test_reprovision_rejects_unsupported_artifact_rootfs_before_mutating_ready_system(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            resp = await _reprovision(pool, _ctx(), sys_id, _artifact_rootfs_profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, provisioning_profile FROM systems WHERE id = %s", (sys_id,)
                )
                sys_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_row is not None and sys_row["state"] == "ready"
        assert sys_row["provisioning_profile"]["provider"]["local-libvirt"]["rootfs"]["kind"] == (
            "local"
        )
        assert job_n is not None and job_n["n"] == 0

    asyncio.run(_run())


def test_reprovision_rejects_local_rootfs_outside_allowed_root_before_mutating_ready_system(
    migrated_url: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            profile = _local_rootfs_profile(outside)
            profile["provider"]["local-libvirt"]["destructive_ops"] = ["reprovision"]
            resp = await _admin_handlers(_rootfs_validator(allowed_root)).reprovision_system(
                pool,
                _ctx(),
                system_id=sys_id,
                profile=profile,
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, provisioning_profile FROM systems WHERE id = %s", (sys_id,)
                )
                sys_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs")
                job_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_row is not None and sys_row["state"] == "ready"
        assert (
            sys_row["provisioning_profile"]["provider"]["local-libvirt"]["rootfs"]["path"]
            == "/var/lib/kdive/rootfs/fedora-40.qcow2"
        )
        assert job_n is not None and job_n["n"] == 0

    asyncio.run(_run())


async def _enqueue_reprovision(pool: AsyncConnectionPool, system_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.REPROVISION,
            {"system_id": system_id, "profile_digest": "deadbeef"},
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{system_id}:reprovision:deadbeef",
        )


def test_reprovision_handler_drives_reprovisioning_to_ready(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET state = 'reprovisioning' WHERE id = %s", (sys_id,)
                )
            job = await _enqueue_reprovision(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                result = await systems_handlers.reprovision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.reprovisioned == [UUID(sys_id)]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, target_fingerprint FROM systems WHERE id = %s", (sys_id,)
                )
                row = await cur.fetchone()
        assert row is not None and row["state"] == "ready"
        assert row["target_fingerprint"]  # the profile digest is recorded

    asyncio.run(_run())


def test_reprovision_handler_provider_failure_sets_failed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET state = 'reprovisioning' WHERE id = %s", (sys_id,)
                )
            job = await _enqueue_reprovision(pool, sys_id)
            prov = _FakeProvisioning(reprovision_error=True)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await systems_handlers.reprovision_handler(conn, job, prov)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "failed"  # interrupted -> failed

    asyncio.run(_run())


def test_reprovision_handler_retry_on_ready_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)  # already ready (finalized)
            job = await _enqueue_reprovision(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_handlers.reprovision_handler(conn, job, prov)
            assert prov.reprovisioned == []  # not re-applied to a finalized System

    asyncio.run(_run())


def test_reprovision_handler_superseded_midflight_tears_down_domain(
    migrated_url: str,
) -> None:
    class _RacingProvisioning(_FakeProvisioning):
        """Drives the System torn_down before returning — a deterministic mid-flight race."""

        def __init__(self, url: str) -> None:
            super().__init__()
            self._url = url

        def reprovision(self, system_id: UUID, profile: Any) -> str:
            name = super().reprovision(system_id, profile)
            with psycopg.connect(self._url, autocommit=True) as c:
                c.execute("UPDATE systems SET state = 'torn_down' WHERE id = %s", (system_id,))
            return name

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _scoped_active_allocation(pool)
            sys_id = await _seed_ready_system(pool, alloc_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET state = 'reprovisioning' WHERE id = %s", (sys_id,)
                )
            job = await _enqueue_reprovision(pool, sys_id)
            prov = _RacingProvisioning(migrated_url)
            async with pool.connection() as conn:
                result = await systems_handlers.reprovision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.torn_down == [f"kdive-{sys_id}"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"

    asyncio.run(_run())


# --- registration --------------------------------------------------------------------------


def test_register_handlers_binds_provision_teardown_and_reprovision() -> None:
    registry = HandlerRegistry()
    systems_handlers.register_handlers(registry, provisioning=_FakeProvisioning())
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
    assert registry.get(JobKind.REPROVISION) is not None


def test_register_handlers_requires_provider_runtime_or_provisioning() -> None:
    registry = HandlerRegistry()
    with pytest.raises(RuntimeError, match="provider runtime or provisioning"):
        systems_handlers.register_handlers(registry)


def test_reprovision_rejects_upload_rootfs(migrated_url: str) -> None:
    # A ready System has no upload window; an upload-kind reprovision is a fail-fast
    # configuration_error (#111).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            profile = _upload_profile()
            profile["provider"]["local-libvirt"]["destructive_ops"] = ["reprovision"]
            resp = await _SYSTEM_ADMIN_HANDLERS.reprovision_system(
                pool,
                _ctx(),
                system_id=sys_id,
                profile=profile,
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- systems.define ------------------------------------------------------------------------


def test_define_inserts_defined_system_and_activates_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
            assert resp.status == "defined"
            assert resp.suggested_next_actions == [
                "artifacts.create_system_upload",
                "systems.provision_defined",
            ]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, allocation_id FROM systems")
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition IN "
                    "('->defined', 'granted->active')"
                )
                audit_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "defined"
        assert str(sys_row["allocation_id"]) == alloc_id
        assert alloc_row is not None and alloc_row["state"] == "active"
        assert audit_row is not None and audit_row["n"] == 2

    asyncio.run(_run())


def test_define_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            first = await _define(pool, _ctx(), alloc_id, _upload_profile())
            second = await _define(pool, _ctx(), alloc_id, _upload_profile())
            assert first.object_id == second.object_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
        assert sys_n is not None and sys_n["n"] == 1  # one System
        assert alloc_row is not None and alloc_row["state"] == "active"  # not re-flipped

    asyncio.run(_run())


def test_define_non_granted_allocation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASING)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "releasing"

    asyncio.run(_run())


def test_define_existing_non_defined_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "ready"
        assert sys_n is not None and sys_n["n"] == 1  # no second System minted

    asyncio.run(_run())


def test_define_over_quota_is_quota_exceeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool, systems_quota=0)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
        assert resp.status == "error"
        assert resp.error_category == "quota_exceeded"

    asyncio.run(_run())


def test_define_requires_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            with pytest.raises(AuthorizationError):
                await _define(pool, _ctx(role=None), alloc_id, _upload_profile())

    asyncio.run(_run())


def test_define_viewer_denied_before_provider_rootfs_validation(
    migrated_url: str, tmp_path: Path
) -> None:
    calls: list[ComponentRef] = []
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            with pytest.raises(AuthorizationError):
                await _provision_handlers(_failing_rootfs_validator(calls)).define_system(
                    pool,
                    _ctx(Role.VIEWER),
                    allocation_id=alloc_id,
                    profile=_local_rootfs_profile(outside),
                )

    asyncio.run(_run())
    assert calls == []


def test_define_foreign_allocation_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _define(pool, _ctx(projects=("other",)), alloc_id, _upload_profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_define_rejects_unsupported_artifact_rootfs_without_opening_upload_window(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _define(pool, _ctx(), alloc_id, _artifact_rootfs_profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0
        assert alloc_row is not None and alloc_row["state"] == "granted"

    asyncio.run(_run())


def test_define_rejects_local_rootfs_outside_allowed_root_without_opening_upload_window(
    migrated_url: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _provision_handlers(_rootfs_validator(allowed_root)).define_system(
                pool,
                _ctx(),
                allocation_id=alloc_id,
                profile=_local_rootfs_profile(outside),
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0
        assert alloc_row is not None and alloc_row["state"] == "granted"

    asyncio.run(_run())


# --- systems.provision_defined admits a DEFINED System -------------------------------------


def test_provision_defined_admits_defined_system(migrated_url: str) -> None:
    # systems.provision_defined(system_id) drives an existing DEFINED System
    # defined -> provisioning and enqueues its provision job (#111).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            resp = await _provision_defined(pool, _ctx(), sys_id)
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'defined->provisioning'"
                )
                audit_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "provisioning"
        assert alloc_row is not None and alloc_row["state"] == "active"  # untouched (set at define)
        assert audit_row is not None and audit_row["n"] == 1

    asyncio.run(_run())


def test_provision_defined_refuses_released_allocation(migrated_url: str) -> None:
    # A DEFINED System whose lease was released (but not yet reaped) must not be admitted to
    # provisioning — symmetric with the create lane's granted check (#111 review).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASING)
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASED)
            resp = await _provision_defined(pool, _ctx(), sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{alloc_id}:provision",),
                )
                job_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "released"
        assert sys_row is not None and sys_row["state"] == "defined"  # not advanced
        assert job_row is not None and job_row["n"] == 0  # no provision job enqueued

    asyncio.run(_run())


def test_provision_defined_revalidates_stored_profile_against_provider(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.ACTIVE)
            sys_id = await _seed_system_with_profile(
                pool,
                alloc_id,
                SystemState.DEFINED,
                _artifact_rootfs_profile(),
            )
            resp = await _provision_defined(pool, _ctx(), sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{alloc_id}:provision",),
                )
                job_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_row is not None and sys_row["state"] == "defined"
        assert job_row is not None and job_row["n"] == 0

    asyncio.run(_run())


def test_provision_defined_revalidates_stored_local_rootfs_against_provider_roots(
    migrated_url: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.ACTIVE)
            sys_id = await _seed_system_with_profile(
                pool,
                alloc_id,
                SystemState.DEFINED,
                _local_rootfs_profile(outside),
            )
            resp = await _provision_handlers(
                _rootfs_validator(allowed_root)
            ).provision_defined_system(
                pool,
                _ctx(),
                system_id=sys_id,
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{alloc_id}:provision",),
                )
                job_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_row is not None and sys_row["state"] == "defined"
        assert job_row is not None and job_row["n"] == 0

    asyncio.run(_run())


def test_provision_defined_viewer_denied_before_provider_rootfs_validation(
    migrated_url: str, tmp_path: Path
) -> None:
    calls: list[ComponentRef] = []
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"rootfs")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.ACTIVE)
            sys_id = await _seed_system_with_profile(
                pool,
                alloc_id,
                SystemState.DEFINED,
                _local_rootfs_profile(outside),
            )
            with pytest.raises(AuthorizationError):
                await _provision_handlers(
                    _failing_rootfs_validator(calls)
                ).provision_defined_system(
                    pool,
                    _ctx(Role.VIEWER),
                    system_id=sys_id,
                )

    asyncio.run(_run())
    assert calls == []


def test_provision_create_lane_rejects_upload(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _SYSTEM_PROVISION_HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_upload_profile()
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0  # fail fast, no System inserted

    asyncio.run(_run())


def test_provision_create_lane_refuses_defined_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.object_id == sys_id
        assert resp.data["reason"] == "use_systems.provision_defined"

    asyncio.run(_run())


# --- teardown of a DEFINED System (defined -> torn_down, #111) ------------------------------


def test_teardown_handler_drives_defined_system_to_torn_down(migrated_url: str) -> None:
    # An abandoned DEFINED System (no domain) is terminable via defined -> torn_down (#111).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_handlers.teardown_handler(conn, job, prov)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "torn_down"
        assert prov.torn_down == [f"kdive-{sys_id}"]  # best-effort destroy of the absent domain

    asyncio.run(_run())


def test_reconciler_gc_tears_down_defined_orphan(migrated_url: str) -> None:
    # Releasing the allocation orphans its DEFINED System; the reconciler enqueues a teardown
    # the handler can now complete (defined -> torn_down), freeing the quota slot (#111).
    from kdive.reconciler.loop import _repair_orphaned_systems

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASING)
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASED)
                enqueued = await _repair_orphaned_systems(conn)
            assert enqueued == 1
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s", (f"{sys_id}:teardown",)
                )
                job_n = await cur.fetchone()
        assert job_n is not None and job_n["n"] == 1

    asyncio.run(_run())
