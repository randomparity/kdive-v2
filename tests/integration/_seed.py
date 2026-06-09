"""DB seeding helpers for the non-gated walking-skeleton exit-criterion tests (#26).

Each helper inserts the minimal real rows a handler-level criterion test needs, so the test
itself asserts only the criterion (gate refusal / idempotent replay / redaction) and not the
provisioning preamble. These mirror `tests/mcp/_seed.py` but are scoped to this module so the
integration suite imports one place.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    BUDGETS,
    INVESTIGATIONS,
    QUOTAS,
    RUNS,
    SYSTEMS,
)
from kdive.domain.models import (
    Allocation,
    Budget,
    Investigation,
    Quota,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.services.resources.discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)

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

BUILD_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org#v6.9",
    "config": {"kind": "local", "path": "/configs/kdump.config"},
}


def provisioning_profile(
    *,
    destructive_ops: list[str] | None = None,
    vcpu: int | None = None,
    memory_mb: int | None = None,
    disk_gb: int | None = None,
) -> dict[str, Any]:
    """A provisioning profile, optionally opting the named destructive ops in (the gate factor).

    ``vcpu`` / ``memory_mb`` / ``disk_gb`` override the default sizing so a test can align the
    profile with a shape-sized allocation's resolved snapshot (ADR-0067 reconciliation: a
    restated size must equal the resolved tuple, else it is a conflict).
    """
    data = copy.deepcopy(PROVISIONING_PROFILE)
    if destructive_ops is not None:
        data["provider"]["local-libvirt"]["destructive_ops"] = destructive_ops
    if vcpu is not None:
        data["vcpu"] = vcpu
    if memory_mb is not None:
        data["memory_mb"] = memory_mb
    if disk_gb is not None:
        data["disk_gb"] = disk_gb
    return data


async def register_resource(
    pool: AsyncConnectionPool,
    *,
    host_uri: str = "qemu:///system",
    cost_class: str = "local",
    concurrent_allocation_cap: int = 2,
) -> str:
    """Register one local-libvirt Resource (the M1 admission target); return its id.

    The fake host advertises ``vcpus=8`` / ``memory_mb=16384`` (the billable size ceiling)
    and the given ``concurrent_allocation_cap`` (the per-host capacity cap). Registration is
    idempotent by ``host_uri``, so a test wanting two distinct hosts (e.g. to set different
    caps) passes distinct ``host_uri`` values.
    """
    disc = LocalLibvirtDiscovery(
        host_uri=host_uri,
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=concurrent_allocation_cap,
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class=cost_class
        )
    return str(res.id)


async def seed_project_limits(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    limit_kcu: Decimal | str | int = 1000,
    max_allocations: int = 10,
    max_systems: int = 10,
) -> None:
    """Seed the ``budgets`` + ``quotas`` rows the M1 admission gate now requires (ADR-0007 §4).

    Fail-closed is literal: a project with no budget row is denied (`allocation_denied`) and
    one with no quota row is denied (`quota_exceeded`). Every M1 e2e test that expects a grant
    seeds both rows up front, mirroring a deployment's explicit admin set-up.
    """
    async with pool.connection() as conn:
        await BUDGETS.upsert(
            conn,
            Budget(
                project=project,
                limit_kcu=Decimal(str(limit_kcu)),
                spent_kcu=Decimal(0),
                updated_at=_DT,
            ),
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project=project,
                max_concurrent_allocations=max_allocations,
                max_concurrent_systems=max_systems,
                updated_at=_DT,
            ),
        )


async def seed_granted_allocation(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    capability_scope: dict[str, Any] | None = None,
) -> str:
    """Register the local-libvirt Resource and insert a `granted` Allocation; return its id."""
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=2,
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
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
                state=AllocationState.GRANTED,
                capability_scope=capability_scope or {},
            ),
        )
    return str(alloc.id)


async def seed_system(
    pool: AsyncConnectionPool,
    allocation_id: str,
    state: SystemState,
    *,
    project: str = "proj",
    destructive_ops: list[str] | None = None,
    domain_name: str | None = None,
) -> str:
    """Insert a System owned by ``allocation_id`` in ``state``; return its id."""
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=UUID(allocation_id),
                state=state,
                provisioning_profile=provisioning_profile(destructive_ops=destructive_ops),
                domain_name=domain_name,
            ),
        )
    return str(system.id)


async def seed_running_run(
    pool: AsyncConnectionPool,
    system_id: str,
    *,
    project: str = "proj",
    build_profile: dict[str, Any] | None = None,
) -> str:
    """Insert an `active` Investigation + a `running` Run on ``system_id``; return the Run id."""
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="walking-skeleton",
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
                project=project,
                investigation_id=inv.id,
                system_id=UUID(system_id),
                state=RunState.RUNNING,
                build_profile=build_profile or copy.deepcopy(BUILD_PROFILE),
            ),
        )
    return str(run.id)


async def seed_crashed_system_with_run(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    debuginfo_ref: str = "proj/runs/r/vmlinux",
    build_id: str = "deadbeef",
) -> tuple[str, str]:
    """Seed a `crashed` System + a `succeeded` Run with a recorded build step (for capture).

    Returns ``(system_id, run_id)``. The Run carries ``debuginfo_ref`` and a `run_steps`
    `build` row recording ``build_id`` so `postmortem.crash` can resolve provenance.
    """
    alloc_id = await seed_granted_allocation(pool, project=project)
    sys_id = await seed_system(
        pool, alloc_id, SystemState.CRASHED, project=project, domain_name="kdive-x"
    )
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="walking-skeleton",
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
                project=project,
                investigation_id=inv.id,
                system_id=UUID(sys_id),
                state=RunState.SUCCEEDED,
                build_profile=copy.deepcopy(BUILD_PROFILE),
                debuginfo_ref=debuginfo_ref,
            ),
        )
        result = {"build_id": build_id, "kernel_ref": "k", "debuginfo_ref": debuginfo_ref}
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s)",
            (run.id, Jsonb(result)),
        )
    return sys_id, str(run.id)
