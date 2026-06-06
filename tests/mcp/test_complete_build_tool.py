"""runs.complete_build + the symmetric source gate (ADR-0048 §4/§6)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db import upload_manifest
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.db.upload_manifest import ManifestEntry
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
from kdive.providers.local_libvirt.build import BuildOutput, ValidatedUpload
from kdive.security.rbac import Role
from kdive.store.objectstore import HeadResult

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(pool: AsyncConnectionPool) -> UUID:
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
                project="proj",
                resource_id=res.id,
                state=AllocationState.ACTIVE,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile={"schema_version": 1},
            ),
        )
    return system.id


async def _seed_investigation(pool: AsyncConnectionPool) -> UUID:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                title="seeded",
                state=InvestigationState.ACTIVE,
            ),
        )
    return inv.id


async def _seed_run(pool: AsyncConnectionPool, build_profile: dict[str, Any]) -> UUID:
    inv_id = await _seed_investigation(pool)
    sys_id = await _seed_system(pool)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv_id,
                system_id=sys_id,
                state=RunState.CREATED,
                build_profile=build_profile,
            ),
        )
    return run.id


async def _seed_external_run(pool: AsyncConnectionPool) -> UUID:
    """A CREATED external Run with no upload manifest."""
    return await _seed_run(pool, {"schema_version": 1, "source": "external"})


async def _seed_external_run_with_manifest(
    pool: AsyncConnectionPool, entries: list[ManifestEntry] | None = None
) -> UUID:
    """A CREATED external Run plus a persisted upload manifest."""
    run_id = await _seed_external_run(pool)
    async with pool.connection() as conn:
        await upload_manifest.replace_manifest(
            conn,
            owner_kind="runs",
            owner_id=run_id,
            prefix=f"local/runs/{run_id}/",
            entries=entries or [ManifestEntry("kernel", "c", 1)],
            ttl=timedelta(hours=1),
        )
    return run_id


async def _seed_server_run(pool: AsyncConnectionPool) -> UUID:
    """A CREATED Run with a server build profile."""
    return await _seed_run(pool, {"schema_version": 1, "kernel_source_ref": "x", "config_ref": "c"})


class _FakeValidator:
    def __init__(self, output: BuildOutput | Exception) -> None:
        self._output = output
        self.calls = 0

    def validate(self, run_id, manifest, keys, declared_build_id) -> ValidatedUpload:
        self.calls += 1
        if isinstance(self._output, Exception):
            raise self._output
        heads = {name: HeadResult(size_bytes=1, checksum_sha256="c", etag="e") for name in keys}
        return ValidatedUpload(output=self._output, heads=heads)


def test_complete_build_finalizes_external_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await runs_tools.complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                cmdline="console=ttyS0 dhash_entries=1",
                validator=validator,
            )
            assert resp.status == "succeeded"
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert run is not None and run.state is RunState.SUCCEEDED
        assert run.kernel_ref is not None and run.kernel_ref.endswith("/kernel")

    asyncio.run(_run())


def test_complete_build_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            r1 = await runs_tools.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x", validator=validator
            )
            r2 = await runs_tools.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x", validator=validator
            )
        assert r1.status == "succeeded" and r2.status == "succeeded"
        assert validator.calls == 1  # the short-read short-circuits the second

    asyncio.run(_run())


def test_complete_build_rejects_server_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_server_run(pool)
            resp = await runs_tools.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_maps_validation_build_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(
                CategorizedError("bad", category=ErrorCategory.BUILD_FAILURE)
            )
            resp = await runs_tools.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x", validator=validator
            )
        assert resp.error_category == ErrorCategory.BUILD_FAILURE.value

    asyncio.run(_run())


def test_build_run_rejects_external_source(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run(pool)
            resp = await runs_tools.build_run(pool, _ctx(), str(run_id))
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_writes_artifact_rows_and_deletes_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            entries = [
                ManifestEntry("kernel", "c", 1),
                ManifestEntry("vmlinux", "c", 1),
                ManifestEntry("initrd", "c", 1),
            ]
            run_id = await _seed_external_run_with_manifest(pool, entries=entries)
            kernel_key = f"local/runs/{run_id}/kernel"
            vmlinux_key = f"local/runs/{run_id}/vmlinux"
            validator = _FakeValidator(BuildOutput(kernel_key, vmlinux_key, "abcd"))
            resp = await runs_tools.complete_build(
                pool, _ctx(), str(run_id), build_id="abcd", cmdline="x", validator=validator
            )
            assert resp.status == "succeeded"
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT object_key FROM artifacts WHERE owner_kind='runs' AND owner_id=%s",
                        (run_id,),
                    )
                    rows = await cur.fetchall()
                manifest = await upload_manifest.get_manifest(conn, "runs", run_id)
        keys = {r["object_key"] for r in rows}
        assert keys == {kernel_key, vmlinux_key, f"local/runs/{run_id}/initrd"}
        assert manifest is None

    asyncio.run(_run())
