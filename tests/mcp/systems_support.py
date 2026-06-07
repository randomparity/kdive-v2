"""Shared support helpers for systems-tool tests and cross-family scenarios."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Budget, Job, JobKind, Quota
from kdive.domain.state import AllocationState
from kdive.jobs import queue
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle import systems as systems_tools
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from kdive.security.rbac import Role
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

TEST_DT = datetime(2026, 1, 1, tzinfo=UTC)

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
                "kind": "path",
                "path": "oci://registry.internal/rootfs/fedora-40@sha256:abc",
            },
            "crashkernel": "256M",
        }
    },
}


def provisioning_profile() -> dict[str, Any]:
    return copy.deepcopy(PROVISIONING_PROFILE)


def upload_profile() -> dict[str, Any]:
    profile = provisioning_profile()
    profile["provider"]["local-libvirt"]["rootfs"] = {"kind": "upload"}
    return profile


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
    conn_pool: AsyncConnectionPool, *, cap: int = 2, systems_quota: int = 1_000_000
) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with conn_pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, disc, pool="local-libvirt", cost_class="local"
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
            {"system_id": system_id},
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{alloc_id}:provision",
        )


async def define_system(
    conn_pool: AsyncConnectionPool,
    request_ctx: RequestContext,
    alloc_id: str,
    profile: dict[str, Any],
):
    return await systems_tools.define_system(
        conn_pool, request_ctx, allocation_id=alloc_id, profile=profile
    )
