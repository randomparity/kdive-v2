"""runs.complete_build + the symmetric source gate (ADR-0048 §4/§6)."""

from __future__ import annotations

import asyncio

from psycopg.rows import dict_row

from kdive.db import upload_manifest
from kdive.db.repositories import RUNS
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import RunState
from kdive.mcp.tools.lifecycle import runs as runs_tools
from kdive.providers.ports import BuildOutput
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
                cmdline="dhash_entries=1",
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


def test_complete_build_malformed_stored_profile_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, {"source": "bogus"})
            resp = await runs_tools.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.status == "error"  # a structured failure, not a raised ToolError
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_rejects_run_with_no_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run(pool)
            resp = await runs_tools.complete_build(
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
            resp = await runs_tools.complete_build(
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
