"""Tests for the reconciler upload reaper (ADR-0048 §6, issue #11).

The reaper prefix-reaps uncommitted objects of pre-finalize owners (a CREATED Run or a
DEFINED System) whose upload manifest is past its deadline, then deletes the manifest
row. It exempts any object with a committed ``artifacts`` row, and the per-owner locked
re-read declines a manifest whose deadline was renewed since the candidate select.

The ``_repair_abandoned_uploads`` tests run the repair through a real non-autocommit
``AsyncConnectionPool`` via ``run_repair`` (mirroring ``test_loop.py``), so the
candidate-select transaction-nesting hazard is exercised; seeding and assertions use
separate autocommit ``connect`` connections.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db import upload_manifest
from kdive.db.locks import LockScope
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.state import RunState
from kdive.reconciler.loop import _reap_one_owner, _repair_abandoned_uploads
from tests.mcp.systems_support import (
    SYSTEM_PROVISION_HANDLERS as _SYSTEM_PROVISION_HANDLERS,
)
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    upload_profile as _upload_profile,
)
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system


class _FakeStore:
    def __init__(self, objects: dict[str, list[str]]) -> None:
        self._objects = objects  # prefix -> [keys]
        self.deleted: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        return list(self._objects.get(prefix, []))

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _insert_artifact_row(
    conn: psycopg.AsyncConnection, *, owner_kind: str, owner_id: UUID, object_key: str
) -> None:
    """Insert a minimal committed ``artifacts`` row (id/timestamps defaulted)."""
    await conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "    retention_class) VALUES (%s, %s, %s, %s, %s, %s)",
        (owner_kind, owner_id, object_key, "etag-1", "sensitive", "default"),
    )


def _reap(store: _FakeStore):
    return lambda conn: _repair_abandoned_uploads(conn, store)


async def _defined_system_via_define(url: str) -> UUID:
    """Produce a DEFINED upload-kind System through systems.define (the real producer, #111).

    The reaper tests need a System that *stays* ``defined`` (so its uncommitted upload object
    is reapable); this exercises the producer instead of a seeded fixture.
    """
    async with AsyncConnectionPool(url, min_size=1, max_size=2) as pool:
        alloc_id = await _granted_allocation(pool)
        resp = await _SYSTEM_PROVISION_HANDLERS.define_system(
            pool, _ctx(), allocation_id=alloc_id, profile=_upload_profile()
        )
    return UUID(resp.object_id)


def test_reaps_uncommitted_objects_past_deadline_for_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix = f"local/runs/{run_id}/"
            await upload_manifest.replace_manifest(
                seed,
                owner_kind="runs",
                owner_id=run_id,
                prefix=prefix,
                entries=[ManifestEntry("kernel", "a", 1)],
                ttl=timedelta(seconds=-1),
            )
        store = _FakeStore({prefix: [f"{prefix}kernel", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}kernel", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaps_uncommitted_objects_past_deadline_for_defined_system(migrated_url: str) -> None:
    async def _run() -> None:
        system_id = await _defined_system_via_define(migrated_url)
        prefix = f"local/systems/{system_id}/"
        async with await connect(migrated_url) as seed:
            await upload_manifest.replace_manifest(
                seed,
                owner_kind="systems",
                owner_id=system_id,
                prefix=prefix,
                entries=[ManifestEntry("rootfs", "a", 1)],
                ttl=timedelta(seconds=-1),
            )
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}rootfs", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "systems", system_id) is None

    asyncio.run(_run())


def test_exempts_committed_object(migrated_url: str) -> None:
    async def _run() -> None:
        system_id = await _defined_system_via_define(migrated_url)
        prefix = f"local/systems/{system_id}/"
        async with await connect(migrated_url) as seed:
            await _insert_artifact_row(
                seed, owner_kind="systems", owner_id=system_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(
                seed,
                owner_kind="systems",
                owner_id=system_id,
                prefix=prefix,
                entries=[ManifestEntry("rootfs", "a", 1)],
                ttl=timedelta(seconds=-1),
            )
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == []  # committed object exempt
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "systems", system_id) is None

    asyncio.run(_run())


def test_skips_owner_not_past_deadline(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix = f"local/runs/{run_id}/"
            await upload_manifest.replace_manifest(
                seed,
                owner_kind="runs",
                owner_id=run_id,
                prefix=prefix,
                entries=[ManifestEntry("kernel", "a", 1)],
                ttl=timedelta(hours=1),
            )
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is not None

    asyncio.run(_run())


def test_skips_finalized_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.SUCCEEDED)
            prefix = f"local/runs/{run_id}/"
            await upload_manifest.replace_manifest(
                seed,
                owner_kind="runs",
                owner_id=run_id,
                prefix=prefix,
                entries=[ManifestEntry("kernel", "a", 1)],
                ttl=timedelta(seconds=-1),
            )
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 0
        assert store.deleted == []  # finalized owner's objects untouched
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is not None

    asyncio.run(_run())


def test_reap_one_owner_declines_renewed_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            system_id = await seed_system(conn)
            run_id = await seed_run(conn, system_id, run_state=RunState.CREATED)
            prefix = f"local/runs/{run_id}/"
            await upload_manifest.replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=run_id,
                prefix=prefix,
                entries=[ManifestEntry("kernel", "a", 1)],
                ttl=timedelta(hours=1),
            )
            store = _FakeStore({prefix: [f"{prefix}kernel"]})
            assert await _reap_one_owner(conn, store, "runs", run_id, LockScope.RUN) is False
            assert store.deleted == []

    asyncio.run(_run())
