"""Shared support helpers for systems-tool tests and cross-family scenarios."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Budget, Job, JobKind, Quota, ResourceKind
from kdive.domain.state import AllocationState
from kdive.jobs import queue
from kdive.jobs.payloads import SystemPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.systems.admin import SystemAdminHandlers
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime
from kdive.security.authz.rbac import Role
from kdive.services.resources.discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

TEST_DT = datetime(2026, 1, 1, tzinfo=UTC)
TEST_PROFILE_POLICY = LocalLibvirtProfilePolicy()
TEST_COMPONENT_SOURCES = ComponentSourceCapabilities(
    provider="test-provider",
    accepted_component_sources={
        "rootfs": frozenset({"catalog", "local"}),
        "config": frozenset({"local"}),
    },
)
SYSTEM_PROVISION_HANDLERS = SystemProvisionHandlers(
    TEST_PROFILE_POLICY,
    TEST_COMPONENT_SOURCES,
    lambda _: None,
)
SYSTEM_ADMIN_HANDLERS = SystemAdminHandlers(
    TEST_PROFILE_POLICY,
    TEST_COMPONENT_SOURCES,
    lambda _: None,
)

PROVISIONING_PROFILE: dict[str, Any] = {
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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def provisioning_profile() -> dict[str, Any]:
    return copy.deepcopy(PROVISIONING_PROFILE)


def fault_inject_profile() -> dict[str, Any]:
    profile = provisioning_profile()
    profile["provider"] = {"fault-inject": {"capture_method": "host_dump"}}
    return profile


def upload_profile() -> dict[str, Any]:
    profile = provisioning_profile()
    profile["provider"]["local-libvirt"]["rootfs"] = {"kind": "upload"}
    return profile


def provider_resolver(
    *,
    provisioner: object | None = None,
    builder: object | None = None,
    installer: object | None = None,
    booter: object | None = None,
    controller: object | None = None,
    retriever: object | None = None,
    profile_policy: object | None = None,
) -> ProviderResolver:
    """Return a local-libvirt resolver with optional fake runtime ports."""
    unused_port = cast(Any, object())
    runtime = ProviderRuntime(
        profile_policy=cast(
            Any, profile_policy if profile_policy is not None else TEST_PROFILE_POLICY
        ),
        provisioner=cast(Any, provisioner if provisioner is not None else unused_port),
        builder=cast(Any, builder if builder is not None else unused_port),
        installer=cast(Any, installer if installer is not None else unused_port),
        booter=cast(Any, booter if booter is not None else unused_port),
        connector=unused_port,
        controller=cast(Any, controller if controller is not None else unused_port),
        retriever=cast(Any, retriever if retriever is not None else unused_port),
        crash_postmortem=unused_port,
        vmcore_introspector=unused_port,
        live_introspector=unused_port,
        component_sources=TEST_COMPONENT_SOURCES,
        rootfs_validator=lambda _: None,
    )
    return ProviderResolver({ResourceKind.LOCAL_LIBVIRT: runtime})


def ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    conn_pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await conn_pool.open()
    try:
        yield conn_pool
    finally:
        await conn_pool.close()


async def granted_allocation(
    conn_pool: AsyncConnectionPool,
    *,
    cap: int = 2,
    systems_quota: int = 1_000_000,
    requested_vcpus: int | None = None,
    requested_memory_gb: int | None = None,
    requested_disk_gb: int | None = None,
    shape: str | None = None,
) -> str:
    """Seed a granted Allocation; pass the ``requested_*``/``shape`` snapshot for a shape-sized
    allocation, or leave them ``None`` for the no-snapshot (full-custom/legacy) lane."""
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with conn_pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=1_000_000,
                max_concurrent_systems=systems_quota,
                updated_at=TEST_DT,
            ),
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj",
                limit_kcu=Decimal("1000000"),
                spent_kcu=Decimal(0),
                updated_at=TEST_DT,
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
                requested_vcpus=requested_vcpus,
                requested_memory_gb=requested_memory_gb,
                requested_disk_gb=requested_disk_gb,
                shape=shape,
            ),
        )
    return str(alloc.id)


class FakeProvisioning:
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


async def enqueue_provision(conn_pool: AsyncConnectionPool, system_id: str, alloc_id: str) -> Job:
    async with conn_pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.PROVISION,
            SystemPayload(system_id=system_id),
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{alloc_id}:provision",
        )


async def define_system(
    conn_pool: AsyncConnectionPool,
    request_ctx: RequestContext,
    alloc_id: str,
    profile: dict[str, Any],
):
    return await SYSTEM_PROVISION_HANDLERS.define_system(
        conn_pool,
        request_ctx,
        allocation_id=alloc_id,
        profile=profile,
    )
