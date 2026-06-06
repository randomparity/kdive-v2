"""Shared DB seeding helpers for the retrieve-plane MCP tool tests (#24).

`seed_crashed_system` inserts a granted Allocation + a `crashed` System; `seed_run_on_system`
inserts an Investigation + a `succeeded` Run carrying a `debuginfo_ref`, plus a `run_steps`
`build` row whose `result` jsonb records the build-id. Both `test_artifacts_tools.py` and
`test_vmcore_tools.py` import from here, so neither test module imports the other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.models import Allocation, Investigation, Run, System
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
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
            "rootfs": {
                "kind": "path",
                "path": "oci://registry.internal/rootfs/fedora-40@sha256:abc",
            },
            "crashkernel": "256M",
        }
    },
}


async def seed_crashed_system(pool: AsyncConnectionPool, *, project: str = "proj") -> str:
    """Insert a granted Allocation and a `crashed` System; return the System id."""
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=2,
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
                principal="u",
                project=project,
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="u",
                project=project,
                allocation_id=alloc.id,
                state=SystemState.CRASHED,
                provisioning_profile=_PROFILE,
                domain_name="kdive-x",
            ),
        )
    return str(system.id)


async def seed_run_on_system(
    pool: AsyncConnectionPool,
    sys_id: str,
    *,
    debuginfo_ref: str | None,
    build_id: str | None,
    project: str = "proj",
) -> str:
    """Insert an Investigation + a `succeeded` Run on the System, plus a `build` step row.

    The Run carries ``debuginfo_ref``; the ``run_steps`` build row records ``build_id`` in its
    ``result`` jsonb (the postmortem reads it for provenance). A ``None`` ``build_id`` omits
    the build step row (the "Run not built" case).
    """
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="u",
                project=project,
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
                principal="u",
                project=project,
                investigation_id=inv.id,
                system_id=UUID(sys_id),
                state=RunState.SUCCEEDED,
                build_profile={},
                debuginfo_ref=debuginfo_ref,
            ),
        )
        if build_id is not None:
            result = {"build_id": build_id, "kernel_ref": "k", "debuginfo_ref": debuginfo_ref}
            await conn.execute(
                "INSERT INTO run_steps (run_id, step, state, result) "
                "VALUES (%s, 'build', 'succeeded', %s)",
                (run.id, Jsonb(result)),
            )
    return str(run.id)
