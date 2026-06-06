"""Tests for the reconciler upload reaper (ADR-0048 §6, issue #11).

The reaper prefix-reaps uncommitted objects of pre-finalize owners (a CREATED Run or a
DEFINED System) whose upload manifest is past its deadline, then deletes the manifest
row. It exempts any object with a committed ``artifacts`` row, and the per-owner locked
re-read declines a manifest whose deadline was renewed since the candidate select.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import psycopg

from kdive.db import upload_manifest
from kdive.db.locks import LockScope
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.state import RunState, SystemState
from kdive.reconciler.loop import _reap_one_owner, _repair_abandoned_uploads
from tests.reconciler.conftest import connect, seed_run, seed_system


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


def test_reaps_uncommitted_objects_past_deadline_for_created_run(migrated_url: str) -> None:
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
                ttl=timedelta(seconds=-1),
            )
            store = _FakeStore({prefix: [f"{prefix}kernel", f"{prefix}stray"]})
            count = await _repair_abandoned_uploads(conn, store)
            assert count == 1
            assert sorted(store.deleted) == [f"{prefix}kernel", f"{prefix}stray"]
            assert await upload_manifest.get_manifest(conn, "runs", run_id) is None

    asyncio.run(_run())


def test_exempts_committed_object(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            system_id = await seed_system(conn, system_state=SystemState.DEFINED)
            prefix = f"local/systems/{system_id}/"
            await _insert_artifact_row(
                conn, owner_kind="systems", owner_id=system_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(
                conn,
                owner_kind="systems",
                owner_id=system_id,
                prefix=prefix,
                entries=[ManifestEntry("rootfs", "a", 1)],
                ttl=timedelta(seconds=-1),
            )
            store = _FakeStore({prefix: [f"{prefix}rootfs"]})
            count = await _repair_abandoned_uploads(conn, store)
            assert count == 1
            assert store.deleted == []  # committed object exempt
            assert await upload_manifest.get_manifest(conn, "systems", system_id) is None

    asyncio.run(_run())


def test_skips_owner_not_past_deadline(migrated_url: str) -> None:
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
            assert await _repair_abandoned_uploads(conn, store) == 0
            assert store.deleted == []

    asyncio.run(_run())


def test_skips_finalized_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            system_id = await seed_system(conn)
            run_id = await seed_run(conn, system_id, run_state=RunState.SUCCEEDED)
            prefix = f"local/runs/{run_id}/"
            await upload_manifest.replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=run_id,
                prefix=prefix,
                entries=[ManifestEntry("kernel", "a", 1)],
                ttl=timedelta(seconds=-1),
            )
            store = _FakeStore({prefix: [f"{prefix}kernel"]})
            assert await _repair_abandoned_uploads(conn, store) == 0

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
