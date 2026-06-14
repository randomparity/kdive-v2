"""Provider component registry visibility and upload finalization (ADR-0065)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.provider_component_records import (
    ArtifactComponentRequest,
    ComponentRegistration,
    ComponentUploadIntentRequest,
    ComponentUploadRegistration,
    LinkLocalComponentRequest,
    _component_from_row,
    component_upload_object_key,
    create_artifact_component,
    create_component_upload_intent,
    finalize_component_upload,
    get_visible_component,
    link_local_component,
    list_visible_components,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import HeadResult
from kdive.provider_components.visibility import Visibility


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
    visibility: Visibility = "project",
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


def _component_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "provider": "local-libvirt",
        "component_kind": "rootfs",
        "source": {"kind": "local", "path": "/tmp/rootfs.img"},
        "artifact_id": None,
        "visibility": "project",
        "project": "proj-a",
        "principal": "alice",
        "sha256": None,
    }
    row.update(overrides)
    return row


def test_component_row_rejects_invalid_component_kind() -> None:
    with pytest.raises(CategorizedError) as caught:
        _component_from_row(_component_row(component_kind="firmware"))

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_component_row_rejects_invalid_visibility() -> None:
    with pytest.raises(CategorizedError) as caught:
        _component_from_row(_component_row(visibility="private"))

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_project_component_visible_only_to_same_project(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        path, sha256 = _component_file(tmp_path)
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            component_id = await link_local_component(
                pool,
                LinkLocalComponentRequest(
                    registration=_registration(),
                    path=str(path),
                    sha256=sha256,
                    allowed_roots=[tmp_path],
                ),
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
                LinkLocalComponentRequest(
                    registration=_registration(),
                    path=str(path),
                    sha256=sha256,
                    allowed_roots=[tmp_path],
                ),
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
                LinkLocalComponentRequest(
                    registration=_registration(visibility="host-policy"),
                    path=str(path),
                    sha256=sha256,
                    allowed_roots=[tmp_path],
                ),
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
                    LinkLocalComponentRequest(
                        registration=_registration(),
                        path=str(outside),
                        sha256=sha256,
                        allowed_roots=[root],
                    ),
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
                    LinkLocalComponentRequest(
                        registration=_registration(),
                        path=str(path),
                        sha256="not-a-sha",
                        allowed_roots=[tmp_path],
                    ),
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
                ArtifactComponentRequest(
                    registration=_registration(),
                    artifact_id=artifact_id,
                    sha256="sha256:" + "1" * 64,
                ),
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
                    ArtifactComponentRequest(
                        registration=_registration(),
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
                ComponentUploadIntentRequest(
                    registration=ComponentUploadRegistration(
                        tenant="proj-a",
                        provider="local-libvirt",
                        component_kind="rootfs",
                        visibility="project",
                        project="proj-a",
                        principal="alice",
                    ),
                    sha256="sha256:" + "2" * 64,
                    size_bytes=42,
                    ttl=timedelta(hours=1),
                ),
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
                ComponentUploadIntentRequest(
                    registration=ComponentUploadRegistration(
                        tenant="proj-a",
                        provider="local-libvirt",
                        component_kind="rootfs",
                        visibility="project",
                        project="proj-a",
                        principal="alice",
                    ),
                    sha256="sha256:" + "7" * 64,
                    size_bytes=42,
                    ttl=timedelta(seconds=-1),
                ),
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
                ComponentUploadIntentRequest(
                    registration=ComponentUploadRegistration(
                        tenant="proj-a",
                        provider="local-libvirt",
                        component_kind="rootfs",
                        visibility="project",
                        project="proj-a",
                        principal="alice",
                    ),
                    sha256="sha256:" + "8" * 64,
                    size_bytes=42,
                    ttl=timedelta(hours=1),
                ),
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
                ComponentUploadIntentRequest(
                    registration=ComponentUploadRegistration(
                        tenant="local",
                        provider="local-libvirt",
                        component_kind="rootfs",
                        visibility="project",
                        project="proj-a",
                        principal="alice",
                    ),
                    sha256="sha256:" + "3" * 64,
                    size_bytes=42,
                    ttl=timedelta(hours=1),
                ),
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
                ComponentUploadIntentRequest(
                    registration=ComponentUploadRegistration(
                        tenant="local",
                        provider="local-libvirt",
                        component_kind="rootfs",
                        visibility="project",
                        project="proj-a",
                        principal="alice",
                    ),
                    sha256="sha256:" + digest.hex(),
                    size_bytes=42,
                    ttl=timedelta(hours=1),
                ),
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
                ComponentUploadIntentRequest(
                    registration=ComponentUploadRegistration(
                        tenant="local",
                        provider="local-libvirt",
                        component_kind="rootfs",
                        visibility="project",
                        project="proj-a",
                        principal="alice",
                    ),
                    sha256="sha256:" + "5" * 64,
                    size_bytes=42,
                    ttl=timedelta(hours=1),
                ),
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
