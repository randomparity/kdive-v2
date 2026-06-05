"""artifacts.create_upload — presign + manifest persistence (ADR-0048 §4)."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db import upload_manifest
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import (
    Allocation,
    Investigation,
    Resource,
    ResourceKind,
    Run,
    Sensitivity,
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
from kdive.mcp.tools import artifacts as artifacts_tools
from kdive.security.rbac import AuthorizationError, Role
from kdive.store.objectstore import PresignedUpload

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_EXTERNAL_PROFILE: dict[str, Any] = {"schema_version": 1, "source": "external"}
_SERVER_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "x",
    "config_ref": "c",
}


class _FakeStore:
    """A presign-only store fake; records the keys + sizes it was asked to sign."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def presign_put(self, key, *, sha256, size_bytes, sensitivity, retention_class, expires_in):
        self.calls.append((key, sha256, size_bytes))
        assert sensitivity is Sensitivity.SENSITIVE and retention_class == "build"
        return PresignedUpload(
            url=f"https://store/{key}", required_headers={"x-amz-checksum-sha256": sha256}
        )


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


async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    state: SystemState = SystemState.READY,
) -> str:
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
                project=project,
                allocation_id=alloc.id,
                state=state,
                provisioning_profile={"schema_version": 1},
            ),
        )
    return str(system.id)


async def _seed_created_run(
    pool: AsyncConnectionPool,
    *,
    build_profile: dict[str, Any],
    project: str = "proj",
) -> str:
    """Insert an Investigation + System + a CREATED Run carrying ``build_profile``."""
    sys_id = await _seed_system(pool, project=project)
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
                state=InvestigationState.OPEN,
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
                state=RunState.CREATED,
                build_profile=copy.deepcopy(build_profile),
            ),
        )
    return str(run.id)


def test_create_upload_mints_presigned_puts_and_persists_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_created_run(pool, build_profile=_EXTERNAL_PROFILE)
            store = _FakeStore()
            responses = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="run",
                owner_id=run_id,
                artifacts=[
                    {"name": "kernel", "sha256": "aaa", "size_bytes": 100},
                    {"name": "vmlinux", "sha256": "bbb", "size_bytes": 200},
                ],
                store=store,
            )
            assert [r.object_id for r in responses] == [
                f"local/runs/{run_id}/kernel",
                f"local/runs/{run_id}/vmlinux",
            ]
            assert responses[0].refs["upload_url"].startswith("https://store/")
            assert responses[0].suggested_next_actions == ["runs.complete_build"]
            assert responses[0].data["name"] == "kernel"
            signed_keys = {c[0] for c in store.calls}
            assert signed_keys == {
                f"local/runs/{run_id}/kernel",
                f"local/runs/{run_id}/vmlinux",
            }
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "runs", UUID(run_id))
            assert manifest is not None
            assert {e.name for e in manifest.entries} == {"kernel", "vmlinux"}

    asyncio.run(_run())


def test_create_upload_rejects_non_external_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_created_run(pool, build_profile=_SERVER_PROFILE)
            store = _FakeStore()
            out = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="run",
                owner_id=run_id,
                artifacts=[{"name": "kernel", "sha256": "aaa", "size_bytes": 100}],
                store=store,
            )
        assert len(out) == 1
        assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert store.calls == []

    asyncio.run(_run())


def test_create_upload_rejects_unknown_artifact_name_for_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_created_run(pool, build_profile=_EXTERNAL_PROFILE)
            store = _FakeStore()
            out = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="run",
                owner_id=run_id,
                artifacts=[{"name": "rootfs", "sha256": "aaa", "size_bytes": 100}],
                store=store,
            )
        assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert store.calls == []

    asyncio.run(_run())


def test_create_upload_rejects_oversize_before_minting(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_created_run(pool, build_profile=_EXTERNAL_PROFILE)
            store = _FakeStore()
            out = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="run",
                owner_id=run_id,
                artifacts=[{"name": "kernel", "sha256": "aaa", "size_bytes": 10**13}],
                store=store,
            )
        assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert store.calls == []

    asyncio.run(_run())


def test_create_upload_requires_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_created_run(pool, build_profile=_EXTERNAL_PROFILE)
            with pytest.raises(AuthorizationError):
                await artifacts_tools.create_upload(
                    pool,
                    _ctx(role=Role.VIEWER),
                    owner_kind="run",
                    owner_id=run_id,
                    artifacts=[{"name": "kernel", "sha256": "aaa", "size_bytes": 100}],
                    store=_FakeStore(),
                )

    asyncio.run(_run())


def test_create_upload_for_defined_system_mints_rootfs_and_persists(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool, state=SystemState.DEFINED)
            store = _FakeStore()
            responses = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="system",
                owner_id=sys_id,
                artifacts=[{"name": "rootfs", "sha256": "aaa", "size_bytes": 100}],
                store=store,
            )
            assert [r.object_id for r in responses] == [f"local/systems/{sys_id}/rootfs"]
            assert responses[0].suggested_next_actions == ["systems.provision"]
            assert {c[0] for c in store.calls} == {f"local/systems/{sys_id}/rootfs"}
            async with pool.connection() as conn:
                manifest = await upload_manifest.get_manifest(conn, "systems", UUID(sys_id))
            assert manifest is not None
            assert {e.name for e in manifest.entries} == {"rootfs"}

    asyncio.run(_run())


def test_create_upload_rejects_non_rootfs_name_for_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool, state=SystemState.DEFINED)
            store = _FakeStore()
            out = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="system",
                owner_id=sys_id,
                artifacts=[{"name": "kernel", "sha256": "aaa", "size_bytes": 100}],
                store=store,
            )
        assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert store.calls == []

    asyncio.run(_run())


def test_create_upload_rejects_empty_artifacts(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_created_run(pool, build_profile=_EXTERNAL_PROFILE)
            store = _FakeStore()
            out = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="run",
                owner_id=run_id,
                artifacts=[],
                store=store,
            )
        assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert store.calls == []

    asyncio.run(_run())
