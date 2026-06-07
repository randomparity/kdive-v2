"""Provider component registry visibility and upload finalization (ADR-0065)."""

from __future__ import annotations

import asyncio
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.db.provider_components import (
    component_upload_object_key,
    create_artifact_component,
    create_component_upload_intent,
    finalize_component_upload,
    get_visible_component,
    link_local_component,
    list_visible_components,
)
from kdive.store.objectstore import HeadResult


class _ObjectStore:
    def __init__(self, heads: dict[str, HeadResult]) -> None:
        self.heads = heads

    def head(self, key: str) -> HeadResult | None:
        return self.heads.get(key)


def test_project_component_visible_only_to_same_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            component_id = await link_local_component(
                pool,
                provider="local-libvirt",
                component_kind="rootfs",
                path="/var/lib/kdive/rootfs/local/base.qcow2",
                sha256="sha256:" + "0" * 64,
                visibility="project",
                project="proj-a",
                principal="alice",
            )

            same_project = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-a"
            )
            other_project = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-b"
            )

        assert [component.id for component in same_project] == [component_id]
        assert other_project == []

    asyncio.run(_run())


def test_get_visible_component_respects_project_visibility(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            component_id = await link_local_component(
                pool,
                provider="local-libvirt",
                component_kind="rootfs",
                path="/var/lib/kdive/rootfs/local/base.qcow2",
                sha256="sha256:" + "0" * 64,
                visibility="project",
                project="proj-a",
                principal="alice",
            )

            denied = await get_visible_component(pool, component_id, project="proj-b")
            allowed = await get_visible_component(pool, component_id, project="proj-a")

        assert denied is None
        assert allowed is not None
        assert allowed.source.kind == "local"

    asyncio.run(_run())


def test_artifact_component_visible_only_to_same_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            artifact_id = UUID("00000000-0000-0000-0000-000000000001")
            component_id = await create_artifact_component(
                pool,
                provider="local-libvirt",
                component_kind="rootfs",
                artifact_id=artifact_id,
                sha256="sha256:" + "1" * 64,
                visibility="project",
                project="proj-a",
                principal="alice",
            )

            same_project = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-a"
            )
            other_project = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-b"
            )

        assert [component.id for component in same_project] == [component_id]
        assert other_project == []

    asyncio.run(_run())


def test_component_upload_finalization_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            upload_id, key = await create_component_upload_intent(
                pool,
                tenant="proj-a",
                provider="local-libvirt",
                component_kind="rootfs",
                sha256="sha256:" + "2" * 64,
                size_bytes=42,
                visibility="project",
                project="proj-a",
                principal="alice",
            )
            assert key == component_upload_object_key(
                tenant="proj-a",
                provider="local-libvirt",
                component_kind="rootfs",
                upload_id=upload_id,
            )
            head = HeadResult(size_bytes=42, checksum_sha256="sha256:" + "2" * 64, etag="e")
            store = _ObjectStore({key: head})

            first = await finalize_component_upload(pool, upload_id, object_store=store)
            second = await finalize_component_upload(pool, upload_id, object_store=store)

        assert first == second

    asyncio.run(_run())
