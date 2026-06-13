"""Concurrent complete_build serializes to one ledger row (ADR-0048 §6).

Two simultaneous complete_build calls on the same Run must collapse to exactly
one run_steps 'build' row and one created → succeeded transition.  The per-Run
advisory lock + ON CONFLICT DO NOTHING + WHERE state='created' UPDATE fence
provide the guarantee; this test proves it against a live Postgres instance.

Validation happens before the lock is acquired, so both racers may call the
validator (calls==1 or calls==2 are both acceptable).  Only one racer finalizes.
"""

from __future__ import annotations

import asyncio

from kdive.db.repositories import RUNS
from kdive.domain.state import RunState
from kdive.mcp.tools.lifecycle.runs.build import RunBuildHandlers
from kdive.provider_components.build_results import BuildOutput
from kdive.provider_components.validation import ComponentSourceCapabilities
from tests.mcp.complete_build_support import (
    FakeValidator,
    ctx,
    pool,
    seed_external_run_with_manifest,
)

_TEST_COMPONENT_SOURCES = ComponentSourceCapabilities(
    provider="test-provider",
    accepted_component_sources={"config": frozenset({"local"})},
)


class _CountingValidator:
    """Wraps the fake validator and tracks total invocations."""

    def __init__(self, output: BuildOutput) -> None:
        self._inner = FakeValidator(output)
        self.calls = 0

    def __call__(self, manifest, keys, declared_build_id, profile_requirements):
        self.calls += 1
        return self._inner(manifest, keys, declared_build_id, profile_requirements)


def test_concurrent_complete_build_yields_one_ledger_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _CountingValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            handlers = RunBuildHandlers(
                _TEST_COMPONENT_SOURCES,
                validate_complete_build=validator,
            )
            results = await asyncio.gather(
                handlers.complete_build(conn_pool, ctx(), str(run_id), build_id=None, cmdline="c"),
                handlers.complete_build(conn_pool, ctx(), str(run_id), build_id=None, cmdline="c"),
            )
            assert all(r.status == "succeeded" for r in results), (
                f"Expected both results to succeed, got: {[r.status for r in results]}"
            )
            assert validator.calls in (1, 2), (
                "validator must run at least once and at most once per racer: "
                f"both may validate before the lock, or the second may hit the "
                f"idempotent short-read, but got {validator.calls} calls"
            )
            async with conn_pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM run_steps WHERE run_id = %s AND step = 'build'",
                    (run_id,),
                )
                row = await cur.fetchone()
                assert row is not None and row[0] == 1, (
                    f"Expected exactly 1 build ledger row, got: {row[0] if row else None}"
                )
                run = await RUNS.get(conn, run_id)
            assert run is not None and run.state is RunState.SUCCEEDED, (
                f"Expected run state SUCCEEDED, got: {run.state if run else None}"
            )

    asyncio.run(_run())


# --- Chunked lane: a loser whose chunks the winner deleted still reports success ---------

import psycopg  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from kdive.domain.errors import CategorizedError, ErrorCategory  # noqa: E402
from kdive.domain.models import Sensitivity  # noqa: E402
from kdive.provider_components.artifacts import HeadResult  # noqa: E402
from kdive.provider_components.uploads import ChunkEntry, ManifestEntry  # noqa: E402
from kdive.services.runs.steps import BuildStepResult  # noqa: E402

_CHUNKED = ManifestEntry("kernel", "whole", 8, chunks=(ChunkEntry("c0", 5), ChunkEntry("c1", 3)))


class _LoserStore:
    """Reassembly fails mid-copy after a concurrent winner finalized the Run.

    The first ``upload_part_copy`` simulates that race: it flips the Run to SUCCEEDED and writes
    the build ledger row through a side connection (the winner), then raises as if the chunk
    objects were already deleted out from under this in-flight copy.
    """

    def __init__(self, url: str, run_id: str) -> None:
        self._url = url
        self._run_id = run_id

    def head(self, key: str) -> HeadResult | None:
        if key.endswith(".part0001"):
            return HeadResult(5, "c0", "e")
        if key.endswith(".part0002"):
            return HeadResult(3, "c1", "e")
        return None

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        return b""

    def create_multipart_upload(self, key, *, sensitivity: Sensitivity, retention_class) -> str:
        return "uid"

    def upload_part_copy(self, key, upload_id, *, part_number, source_key) -> str:
        result = BuildStepResult(
            kernel_ref=f"local/runs/{self._run_id}/kernel",
            debuginfo_ref="",
            initrd_ref=None,
            build_id="",
            cmdline="c",
        )
        with psycopg.connect(self._url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO run_steps (run_id, step, state, result) "
                "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
                (self._run_id, Jsonb(result.dump())),
            )
            conn.execute(
                "UPDATE runs SET state = 'succeeded' WHERE id = %s AND state = 'created'",
                (self._run_id,),
            )
        raise CategorizedError("chunk gone", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    def complete_multipart_upload(self, key, upload_id, parts) -> str:
        return "final"

    def abort_multipart_upload(self, key, upload_id) -> None:
        pass

    def delete(self, key: str) -> None:
        pass


def test_chunked_loser_returns_success_when_winner_already_finalized(migrated_url: str) -> None:
    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool, entries=[_CHUNKED])
            store = _LoserStore(migrated_url, str(run_id))
            handlers = RunBuildHandlers(
                _TEST_COMPONENT_SOURCES,
                validate_complete_build=FakeValidator(
                    BuildOutput(f"local/runs/{run_id}/kernel", "", "")
                ),
                object_store_factory=lambda: store,
            )
            resp = await handlers.complete_build(
                conn_pool, ctx(), str(run_id), build_id=None, cmdline="c"
            )
            async with conn_pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        # The loser maps its mid-copy failure to the winner's recorded success, not an error.
        assert resp.status == "succeeded"
        assert run is not None and run.state is RunState.SUCCEEDED

    asyncio.run(_run())
