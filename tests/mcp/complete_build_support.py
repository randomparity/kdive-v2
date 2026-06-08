"""Shared support helpers for complete-build tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db import upload_manifest
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.models import Allocation, Investigation, Resource, ResourceKind, Run, System
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.mcp.auth import RequestContext
from kdive.providers.ports import BuildOutput, ValidatedUpload
from kdive.security.authz.rbac import Role
from kdive.store.objectstore import HeadResult

TEST_DT = datetime(2026, 1, 1, tzinfo=UTC)


def ctx() -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
    )


@asynccontextmanager
async def pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    conn_pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await conn_pool.open()
    try:
        yield conn_pool
    finally:
        await conn_pool.close()


async def seed_system(conn_pool: AsyncConnectionPool) -> UUID:
    async with conn_pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
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
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.ACTIVE,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile={"schema_version": 1},
            ),
        )
    return system.id


async def seed_investigation(conn_pool: AsyncConnectionPool) -> UUID:
    async with conn_pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                title="seeded",
                state=InvestigationState.ACTIVE,
            ),
        )
    return inv.id


async def seed_run(conn_pool: AsyncConnectionPool, build_profile: dict[str, Any]) -> UUID:
    inv_id = await seed_investigation(conn_pool)
    sys_id = await seed_system(conn_pool)
    async with conn_pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv_id,
                system_id=sys_id,
                state=RunState.CREATED,
                build_profile=build_profile,
            ),
        )
    return run.id


async def seed_external_run(conn_pool: AsyncConnectionPool) -> UUID:
    """A CREATED external Run with no upload manifest."""
    return await seed_run(conn_pool, {"schema_version": 1, "source": "external"})


async def seed_external_run_with_manifest(
    conn_pool: AsyncConnectionPool, entries: list[ManifestEntry] | None = None
) -> UUID:
    """A CREATED external Run plus a persisted upload manifest."""
    run_id = await seed_external_run(conn_pool)
    async with conn_pool.connection() as conn:
        await upload_manifest.replace_manifest(
            conn,
            owner_kind="runs",
            owner_id=run_id,
            prefix=f"local/runs/{run_id}/",
            entries=entries or [ManifestEntry("kernel", "c", 1)],
            ttl=timedelta(hours=1),
        )
    return run_id


async def seed_server_run(conn_pool: AsyncConnectionPool) -> UUID:
    """A CREATED Run with a server build profile."""
    return await seed_run(
        conn_pool,
        {
            "schema_version": 1,
            "kernel_source_ref": "x",
            "config": {"kind": "local", "path": "/configs/c"},
        },
    )


class FakeValidator:
    def __init__(self, output: BuildOutput | Exception) -> None:
        self._output = output
        self.calls = 0

    def validate(
        self,
        run_id,
        manifest,
        keys,
        declared_build_id,
        profile_requirements=None,
    ) -> ValidatedUpload:
        self.calls += 1
        if isinstance(self._output, Exception):
            raise self._output
        heads = {name: HeadResult(size_bytes=1, checksum_sha256="c", etag="e") for name in keys}
        return ValidatedUpload(output=self._output, heads=heads)
