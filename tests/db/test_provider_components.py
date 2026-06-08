"""Provider component registry visibility and upload finalization (ADR-0065)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.db.provider_components import (
    ArtifactComponentRequest,
    ComponentRegistration,
    ComponentUploadIntentRequest,
    ComponentUploadRegistration,
    LinkLocalComponentRequest,
    component_upload_object_key,
    create_artifact_component,
    create_component_upload_intent,
    finalize_component_upload,
    get_visible_component,
    link_local_component,
    list_visible_components,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import HeadResult

type TestVisibility = Literal["public", "project", "host-policy"]


class _ObjectStore:
    def __init__(self, heads: dict[str, HeadResult]) -> None:
        self.heads = heads

    def head(self, key: str) -> HeadResult | None:
        return self.heads.get(key)


def _component_file(tmp_path: Path, name: str = "component.img") -> tuple[Path, str]:
    path = tmp_path / name
    content = b"component bytes"
    path.write_bytes(content)
    return path, f"sha256:{hashlib.sha256(content).hexdigest()}"


def _registration(
    *,
    visibility: TestVisibility = "project",
    project: str | None = "proj-a",
    principal: str = "alice",
) -> ComponentRegistration:
    return ComponentRegistration(
        provider="local-libvirt",
        component_kind="rootfs",
        visibility=visibility,
        project=project,
        principal=principal,
    )


def _local_request(
    path: Path,
    sha256: str,
    allowed_roots: list[Path],
    *,
    visibility: TestVisibility = "project",
    project: str | None = "proj-a",
) -> LinkLocalComponentRequest:
    return LinkLocalComponentRequest(
        registration=_registration(visibility=visibility, project=project),
        path=str(path),
        sha256=sha256,
        allowed_roots=allowed_roots,
    )


def _artifact_request(
    *,
    artifact_id: UUID,
    sha256: str,
    visibility: TestVisibility = "project",
    project: str | None = "proj-a",
) -> ArtifactComponentRequest:
    return ArtifactComponentRequest(
        registration=_registration(visibility=visibility, project=project),
        artifact_id=artifact_id,
        sha256=sha256,
    )


def _upload_request(
    *,
    tenant: str = "proj-a",
    sha256: str,
    size_bytes: int = 42,
    ttl: timedelta = timedelta(hours=1),
) -> ComponentUploadIntentRequest:
    return ComponentUploadIntentRequest(
        registration=ComponentUploadRegistration(
            tenant=tenant,
            provider="local-libvirt",
            component_kind="rootfs",
            visibility="project",
            project="proj-a",
            principal="alice",
        ),
        sha256=sha256,
        size_bytes=size_bytes,
        ttl=ttl,
    )


def test_project_component_visible_only_to_same_project(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        path, sha256 = _component_file(tmp_path)
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            component_id = await link_local_component(
                pool,
                _local_request(path, sha256, [tmp_path]),
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


def test_get_visible_component_respects_project_visibility(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        path, sha256 = _component_file(tmp_path)
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            component_id = await link_local_component(
                pool,
                _local_request(path, sha256, [tmp_path]),
            )

            denied = await get_visible_component(pool, component_id, project="proj-b")
            allowed = await get_visible_component(pool, component_id, project="proj-a")

        assert denied is None
        assert allowed is not None
        assert allowed.source.kind == "local"

    asyncio.run(_run())


def test_host_policy_component_hidden_from_project_lookup(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        path, sha256 = _component_file(tmp_path)
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            component_id = await link_local_component(
                pool,
                _local_request(path, sha256, [tmp_path], visibility="host-policy"),
            )

            listed = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-a"
            )
            fetched = await get_visible_component(pool, component_id, project="proj-a")

        assert listed == []
        assert fetched is None

    asyncio.run(_run())


def test_link_local_component_rejects_path_outside_allowed_roots(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        outside = tmp_path / "outside.img"
        outside.write_bytes(b"outside")
        root = tmp_path / "allowed"
        root.mkdir()
        sha256 = f"sha256:{hashlib.sha256(b'outside').hexdigest()}"
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            try:
                await link_local_component(
                    pool,
                    _local_request(outside, sha256, [root]),
                )
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
            else:
                raise AssertionError("outside local component path should be rejected")

            visible = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-a"
            )

        assert visible == []

    asyncio.run(_run())


def test_link_local_component_rejects_bad_sha_without_poisoning_listing(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        path, _sha256 = _component_file(tmp_path)
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            try:
                await link_local_component(
                    pool,
                    _local_request(path, "not-a-sha", [tmp_path]),
                )
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
            else:
                raise AssertionError("bad local component sha should be rejected")

            visible = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-a"
            )

        assert visible == []

    asyncio.run(_run())


def test_artifact_component_visible_only_to_same_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            artifact_id = UUID("00000000-0000-0000-0000-000000000001")
            component_id = await create_artifact_component(
                pool,
                _artifact_request(artifact_id=artifact_id, sha256="sha256:" + "1" * 64),
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


def test_create_artifact_component_rejects_bad_sha(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            try:
                await create_artifact_component(
                    pool,
                    _artifact_request(
                        artifact_id=UUID("00000000-0000-0000-0000-000000000001"),
                        sha256="not-a-sha",
                    ),
                )
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
            else:
                raise AssertionError("bad artifact component sha should be rejected")

            visible = await list_visible_components(
                pool, provider="local-libvirt", component_kind="rootfs", project="proj-a"
            )

        assert visible == []

    asyncio.run(_run())


def test_component_upload_finalization_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            upload_id, key = await create_component_upload_intent(
                pool,
                _upload_request(sha256="sha256:" + "2" * 64),
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
            async with pool.connection() as conn:
                row = await conn.execute(
                    "SELECT component_uploads.component_id, provider_components.artifact_id, "
                    "provider_components.source "
                    "FROM component_uploads "
                    "JOIN provider_components "
                    "ON provider_components.id = component_uploads.component_id "
                    "WHERE component_uploads.id = %s",
                    (upload_id,),
                )
                finalized = await row.fetchone()

        assert first == second
        assert finalized is not None
        assert finalized[0] == first
        assert finalized[1] is None
        assert finalized[2]["kind"] == "component-upload"
        assert finalized[2]["upload_id"] == str(upload_id)

    asyncio.run(_run())


def test_expired_component_upload_cannot_finalize(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            upload_id, key = await create_component_upload_intent(
                pool,
                _upload_request(sha256="sha256:" + "7" * 64, ttl=timedelta(seconds=-1)),
            )
            store = _ObjectStore(
                {key: HeadResult(size_bytes=42, checksum_sha256="sha256:" + "7" * 64, etag="e")}
            )

            try:
                await finalize_component_upload(pool, upload_id, object_store=store)
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
            else:
                raise AssertionError("expired upload should reject finalization")

            count = await _provider_component_count(pool)

        assert count == 0

    asyncio.run(_run())


def test_failed_component_upload_cannot_finalize(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            upload_id, key = await create_component_upload_intent(
                pool,
                _upload_request(sha256="sha256:" + "8" * 64),
            )
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE component_uploads SET state = 'failed' WHERE id = %s",
                    (upload_id,),
                )
            store = _ObjectStore(
                {key: HeadResult(size_bytes=42, checksum_sha256="sha256:" + "8" * 64, etag="e")}
            )

            try:
                await finalize_component_upload(pool, upload_id, object_store=store)
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
            else:
                raise AssertionError("failed upload should reject finalization")

            count = await _provider_component_count(pool)

        assert count == 0

    asyncio.run(_run())


def test_component_upload_finalization_uses_persisted_tenant(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            upload_id, key = await create_component_upload_intent(
                pool,
                _upload_request(tenant="local", sha256="sha256:" + "3" * 64),
            )
            assert key == component_upload_object_key(
                tenant="local",
                provider="local-libvirt",
                component_kind="rootfs",
                upload_id=upload_id,
            )
            wrong_key = component_upload_object_key(
                tenant="proj-a",
                provider="local-libvirt",
                component_kind="rootfs",
                upload_id=upload_id,
            )
            store = _ObjectStore(
                {key: HeadResult(size_bytes=42, checksum_sha256="sha256:" + "3" * 64, etag="e")}
            )

            component_id = await finalize_component_upload(pool, upload_id, object_store=store)

        assert component_id != upload_id
        assert wrong_key not in store.heads

    asyncio.run(_run())


def test_component_upload_finalization_accepts_s3_base64_sha256(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            digest = bytes.fromhex("4" * 64)
            upload_id, key = await create_component_upload_intent(
                pool,
                _upload_request(tenant="local", sha256="sha256:" + digest.hex()),
            )
            store = _ObjectStore(
                {
                    key: HeadResult(
                        size_bytes=42,
                        checksum_sha256=base64.b64encode(digest).decode("ascii"),
                        etag="e",
                    )
                }
            )

            component_id = await finalize_component_upload(pool, upload_id, object_store=store)
            component = await get_visible_component(pool, component_id, project="proj-a")

        assert component is not None
        assert component.sha256 == "sha256:" + "4" * 64

    asyncio.run(_run())


def test_component_upload_finalization_rejects_s3_checksum_mismatch(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            upload_id, key = await create_component_upload_intent(
                pool,
                _upload_request(tenant="local", sha256="sha256:" + "5" * 64),
            )
            wrong_digest = bytes.fromhex("6" * 64)
            store = _ObjectStore(
                {
                    key: HeadResult(
                        size_bytes=42,
                        checksum_sha256=base64.b64encode(wrong_digest).decode("ascii"),
                        etag="e",
                    )
                }
            )

            try:
                await finalize_component_upload(pool, upload_id, object_store=store)
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
            else:
                raise AssertionError("checksum mismatch should reject finalization")

    asyncio.run(_run())


async def _provider_component_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        row = await conn.execute("SELECT count(*) FROM provider_components")
        found = await row.fetchone()
    assert found is not None
    return found[0]
