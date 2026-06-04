"""systems.* tool + handler tests — handlers called directly with injected pool + provider."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, INVESTIGATIONS, QUOTAS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import (
    Allocation,
    Budget,
    Investigation,
    Job,
    JobKind,
    Quota,
    Run,
    System,
)
from kdive.domain.state import AllocationState, InvestigationState, RunState, SystemState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import systems as systems_tools
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from kdive.security.rbac import Role
from tests.providers.local_libvirt.conftest import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc",
            "crashkernel": "256M",
        }
    },
}


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


async def _granted_allocation(
    pool: AsyncConnectionPool, *, cap: int = 2, systems_quota: int = 1_000_000
) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, disc, pool="local-libvirt", cost_class="local"
        )
        # systems.provision enforces a per-project max_concurrent_systems (ADR-0007 §4);
        # seed a generous quota + budget so the existing provision paths are not denied.
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=1_000_000,
                max_concurrent_systems=systems_quota,
                updated_at=_DT,
            ),
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj", limit_kcu=Decimal("1000000"), spent_kcu=Decimal(0), updated_at=_DT
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
    return str(alloc.id)


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
            resp = await systems_tools.get_system(pool, _ctx(), sys_id)
        assert resp.object_id == sys_id
        assert resp.status == "ready"

    asyncio.run(_run())


def test_get_failed_system_renders_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.FAILED)
            resp = await systems_tools.get_system(pool, _ctx(), sys_id)
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await systems_tools.get_system(pool, _ctx(projects=("other",)), sys_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await systems_tools.get_system(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- shared fakes/helpers for provision/teardown handler + tool tests ---------------------


class _FakeProvisioning:
    """Records provision/teardown/reprovision calls; provision returns a name or raises."""

    def __init__(self, *, provision_error: bool = False, reprovision_error: bool = False) -> None:
        self.provisioned: list[UUID] = []
        self.torn_down: list[str] = []
        self.reprovisioned: list[UUID] = []
        self._provision_error = provision_error
        self._reprovision_error = reprovision_error

    def provision(self, system_id: UUID, profile: Any) -> str:
        self.provisioned.append(system_id)
        if self._provision_error:
            raise CategorizedError("boom", category=ErrorCategory.PROVISIONING_FAILURE)
        return f"kdive-{system_id}"

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)

    def reprovision(self, system_id: UUID, profile: Any) -> str:
        self.reprovisioned.append(system_id)
        if self._reprovision_error:
            raise CategorizedError("boom", category=ErrorCategory.PROVISIONING_FAILURE)
        return f"kdive-{system_id}"


async def _enqueue_provision(pool: AsyncConnectionPool, system_id: str, alloc_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.PROVISION,
            {"system_id": system_id},
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{alloc_id}:provision",
        )


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
    return await systems_tools.provision_system(pool, ctx, allocation_id=alloc_id, profile=profile)


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


def test_provision_without_operator_raises(migrated_url: str) -> None:
    from kdive.security.rbac import AuthorizationError

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            with pytest.raises(AuthorizationError):
                await _provision(pool, _ctx(Role.VIEWER), alloc_id, _profile())

    asyncio.run(_run())


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
                result = await systems_tools.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.provisioned == [UUID(sys_id)]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, domain_name FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "ready"
        assert row["domain_name"] == f"kdive-{sys_id}"

    asyncio.run(_run())


def test_provision_handler_retry_on_ready_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_tools.provision_handler(conn, job, prov)
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
                    await systems_tools.provision_handler(conn, job, prov)
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
                    await systems_tools.provision_handler(conn, job, prov)
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
                result = await systems_tools.provision_handler(conn, job, prov)
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
                    await systems_tools.provision_handler(conn, job, prov)
            assert prov.torn_down == []  # nothing reaped yet — the domain is still leaked
            async with pool.connection() as conn:  # requeue
                result = await systems_tools.provision_handler(conn, job, prov)
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
                    await systems_tools.provision_handler(conn, job, prov)
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
                result = await systems_tools.provision_handler(conn, job, prov)
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
                result = await systems_tools.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.torn_down == []  # the live domain was left alone
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "ready"

    asyncio.run(_run())


# --- systems.teardown tool + handler -------------------------------------------------------


async def _teardown(pool: AsyncConnectionPool, ctx: RequestContext, system_id: str):
    return await systems_tools.teardown_system(pool, ctx, system_id)


def test_teardown_tool_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _teardown(pool, _ctx(), sys_id)
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
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            resp = await _teardown(pool, _ctx(), sys_id)
            assert resp.status == "torn_down"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs")
                row = await cur.fetchone()
        assert row is not None and row["n"] == 0

    asyncio.run(_run())


def test_teardown_tool_without_operator_raises(migrated_url: str) -> None:
    from kdive.security.rbac import AuthorizationError

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            with pytest.raises(AuthorizationError):
                await _teardown(pool, _ctx(Role.VIEWER), sys_id)

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
                await systems_tools.teardown_handler(conn, job, prov)
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
                await systems_tools.teardown_handler(conn, job, prov)
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
                await systems_tools.teardown_handler(conn, job, prov)
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
    return await systems_tools.reprovision_system(pool, ctx, system_id=system_id, profile=profile)


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


async def _enqueue_reprovision(pool: AsyncConnectionPool, system_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.REPROVISION,
            {"system_id": system_id},
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
                result = await systems_tools.reprovision_handler(conn, job, prov)
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
                    await systems_tools.reprovision_handler(conn, job, prov)
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
                await systems_tools.reprovision_handler(conn, job, prov)
            assert prov.reprovisioned == []  # not re-applied to a finalized System

    asyncio.run(_run())


# --- registration --------------------------------------------------------------------------


def test_register_handlers_binds_provision_teardown_and_reprovision() -> None:
    registry = HandlerRegistry()
    systems_tools.register_handlers(registry, provisioning=_FakeProvisioning())
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
    assert registry.get(JobKind.REPROVISION) is not None
