"""runs.complete_build + the symmetric source gate (ADR-0048 §4/§6)."""

from __future__ import annotations

import asyncio

from psycopg.rows import dict_row

from kdive.db import upload_manifest
from kdive.db.repositories import RUNS
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import RunState
from kdive.mcp.tools.catalog.artifacts_uploads import create_run_upload
from kdive.mcp.tools.lifecycle.runs.build import RunBuildHandlers
from kdive.providers.component_validation import ComponentSourceCapabilities
from kdive.providers.ports import BuildOutput
from kdive.store.objectstore import HeadResult, PresignedUpload
from tests.mcp.complete_build_support import (
    FakeValidator as _FakeValidator,
)
from tests.mcp.complete_build_support import (
    ctx as _ctx,
)
from tests.mcp.complete_build_support import (
    pool as _pool,
)
from tests.mcp.complete_build_support import (
    seed_external_run as _seed_external_run,
)
from tests.mcp.complete_build_support import (
    seed_external_run_with_manifest as _seed_external_run_with_manifest,
)
from tests.mcp.complete_build_support import (
    seed_run as _seed_run,
)
from tests.mcp.complete_build_support import (
    seed_server_run as _seed_server_run,
)

_BZIMAGE_HEAD = b"\x00" * 0x202 + b"HdrS"
_EXTERNAL_PROFILE_WITH_REQUIREMENTS = {
    "schema_version": 1,
    "source": "external",
    "profile_requirements": {
        "provider": "local-libvirt",
        "name": "console-ready_x86_64",
    },
}
_TEST_COMPONENT_SOURCES = ComponentSourceCapabilities(
    provider="test-provider",
    accepted_component_sources={"config": frozenset({"local"})},
)
_DEFAULT_BUILD_HANDLERS = RunBuildHandlers(_TEST_COMPONENT_SOURCES)


def _build_handlers(validator) -> RunBuildHandlers:
    return RunBuildHandlers(_TEST_COMPONENT_SOURCES, validate_complete_build=validator)


class _UploadStore:
    def presign_put(self, key, *, sha256, size_bytes, sensitivity, retention_class, expires_in):
        _ = (sensitivity, retention_class, expires_in)
        return PresignedUpload(
            url=f"https://store/{key}", required_headers={"x-amz-checksum-sha256": sha256}
        )


class _ValidationStore:
    def __init__(self, blobs: dict[str, bytes], heads: dict[str, HeadResult]) -> None:
        self._blobs = blobs
        self._heads = heads

    def head(self, key: str) -> HeadResult | None:
        return self._heads.get(key)

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        return self._blobs[key][start : start + length]


async def _artifact_keys(pool, run_id) -> set[str]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT object_key FROM artifacts WHERE owner_kind='runs' AND owner_id=%s",
            (run_id,),
        )
        return {row["object_key"] for row in await cur.fetchall()}


def test_complete_build_finalizes_external_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                cmdline="dhash_entries=1",
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
            handlers = _build_handlers(validator)
            r1 = await handlers.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
            r2 = await handlers.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert r1.status == "succeeded" and r2.status == "succeeded"
        assert validator.calls == 1  # the short-read short-circuits the second

    asyncio.run(_run())


def test_complete_build_rejects_server_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_server_run(pool)
            resp = await _DEFAULT_BUILD_HANDLERS.complete_build(
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
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.error_category == ErrorCategory.BUILD_FAILURE.value

    asyncio.run(_run())


def test_build_run_rejects_external_source(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run(pool)
            resp = await _DEFAULT_BUILD_HANDLERS.build_run(
                pool,
                _ctx(),
                str(run_id),
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_malformed_stored_profile_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, {"source": "bogus"})
            resp = await _DEFAULT_BUILD_HANDLERS.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.status == "error"  # a structured failure, not a raised ToolError
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_rejects_run_with_no_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run(pool)
            resp = await _DEFAULT_BUILD_HANDLERS.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_rejects_non_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            async with pool.connection() as conn:
                await conn.execute("UPDATE runs SET state='failed' WHERE id=%s", (run_id,))
            resp = await _DEFAULT_BUILD_HANDLERS.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["current_status"] == RunState.FAILED.value

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
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id="abcd", cmdline="x"
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


def test_complete_build_writes_artifacts_after_effective_config_validation(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _EXTERNAL_PROFILE_WITH_REQUIREMENTS)
            config = b"CONFIG_SERIAL_8250_CONSOLE=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_VIRTIO_PCI=y\n"
            responses = await create_run_upload(
                pool,
                _ctx(),
                run_id=str(run_id),
                artifacts=[
                    {"name": "kernel", "sha256": "ck", "size_bytes": len(_BZIMAGE_HEAD)},
                    {"name": "effective_config", "sha256": "cc", "size_bytes": len(config)},
                ],
                store=_UploadStore(),
            )
            assert {response.status for response in responses.items} == {"upload_ready"}
            assert await _artifact_keys(pool, run_id) == set()
            kernel_key = f"local/runs/{run_id}/kernel"
            config_key = f"local/runs/{run_id}/effective_config"
            store = _ValidationStore(
                {kernel_key: _BZIMAGE_HEAD, config_key: config},
                {
                    kernel_key: HeadResult(len(_BZIMAGE_HEAD), "ck", "e-k"),
                    config_key: HeadResult(len(config), "cc", "e-c"),
                },
            )

            resp = await RunBuildHandlers(
                _TEST_COMPONENT_SOURCES,
                object_store_factory=lambda: store,
            ).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                cmdline="x",
            )
            keys = await _artifact_keys(pool, run_id)

        assert resp.status == "succeeded", resp
        assert keys == {kernel_key, config_key}

    asyncio.run(_run())


def test_complete_build_rejects_missing_effective_config_without_artifacts(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _EXTERNAL_PROFILE_WITH_REQUIREMENTS)
            responses = await create_run_upload(
                pool,
                _ctx(),
                run_id=str(run_id),
                artifacts=[
                    {"name": "kernel", "sha256": "ck", "size_bytes": len(_BZIMAGE_HEAD)},
                ],
                store=_UploadStore(),
            )
            assert {response.status for response in responses.items} == {"upload_ready"}
            kernel_key = f"local/runs/{run_id}/kernel"
            store = _ValidationStore(
                {kernel_key: _BZIMAGE_HEAD},
                {kernel_key: HeadResult(len(_BZIMAGE_HEAD), "ck", "e-k")},
            )

            resp = await RunBuildHandlers(
                _TEST_COMPONENT_SOURCES,
                object_store_factory=lambda: store,
            ).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                cmdline="x",
            )
            keys = await _artifact_keys(pool, run_id)

        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert keys == set()

    asyncio.run(_run())
