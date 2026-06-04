"""systems.* tool + handler tests — handlers called directly with injected pool + provider."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Job, JobKind, System
from kdive.domain.state import AllocationState, SystemState
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


async def _granted_allocation(pool: AsyncConnectionPool, *, cap: int = 2) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, disc, pool="local-libvirt", cost_class="local"
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
    """Records provision/teardown calls; provision returns a domain name or raises."""

    def __init__(self, *, provision_error: bool = False) -> None:
        self.provisioned: list[UUID] = []
        self.torn_down: list[str] = []
        self._provision_error = provision_error

    def provision(self, system_id: UUID, profile: Any) -> str:
        self.provisioned.append(system_id)
        if self._provision_error:
            raise CategorizedError("boom", category=ErrorCategory.PROVISIONING_FAILURE)
        return f"kdive-{system_id}"

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)


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


# --- registration --------------------------------------------------------------------------


def test_register_handlers_binds_provision_and_teardown() -> None:
    registry = HandlerRegistry()
    systems_tools.register_handlers(registry, provisioning=_FakeProvisioning())
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
