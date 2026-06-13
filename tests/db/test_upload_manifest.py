"""Tests for owner-scoped upload-manifest storage (ADR-0048 §4)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg

from kdive.db.upload_manifest import (
    UploadManifestReplaceRequest,
    delete_manifest,
    get_manifest,
    replace_manifest,
)
from kdive.provider_components.uploads import ChunkEntry, ManifestEntry


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def _request(
    owner_id: UUID,
    entries: list[ManifestEntry],
    *,
    prefix: str | None = None,
    ttl: timedelta = timedelta(hours=1),
) -> UploadManifestReplaceRequest:
    return UploadManifestReplaceRequest(
        owner_kind="runs",
        owner_id=owner_id,
        prefix=prefix or f"local/runs/{owner_id}/",
        entries=entries,
        ttl=ttl,
    )


def test_round_trip(migrated_url: str) -> None:
    """replace_manifest then get_manifest returns the entries, prefix, and a deadline."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10), ManifestEntry("vmlinux", "YmFy", 20)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert got.entries == tuple(entries)
        assert got.prefix == f"local/runs/{owner_id}/"
        assert got.deadline is not None

    asyncio.run(_run_test())


def test_round_trips_chunks(migrated_url: str) -> None:
    """A chunked entry persists and reloads its ordered chunk list through the JSONB column."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [
            ManifestEntry(
                "vmlinux",
                "whole",
                10,
                chunks=(ChunkEntry("c0", 6), ChunkEntry("c1", 4)),
            ),
            ManifestEntry("kernel", "Zm9v", 3),
        ]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        by_name = {e.name: e for e in got.entries}
        assert by_name["vmlinux"].chunks == (ChunkEntry("c0", 6), ChunkEntry("c1", 4))
        assert by_name["kernel"].chunks is None

    asyncio.run(_run_test())


def test_full_set_replacement(migrated_url: str) -> None:
    """A second replace_manifest with fewer entries replaces, not merges, the prior set."""

    async def _run_test() -> None:
        owner_id = uuid4()
        first_entries = [
            ManifestEntry("kernel", "Zm9v", 10),
            ManifestEntry("vmlinux", "YmFy", 20),
        ]
        second_entries = [ManifestEntry("kernel", "bmV3", 30)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, first_entries))
            await replace_manifest(
                conn, _request(owner_id, second_entries, prefix=f"local/runs/{owner_id}/v2/")
            )
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert got.entries == tuple(second_entries)
        assert got.prefix == f"local/runs/{owner_id}/v2/"

    asyncio.run(_run_test())


def test_remint_updates_deadline(migrated_url: str) -> None:
    """A re-mint with a longer ttl moves the deadline forward (proves EXCLUDED.deadline)."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)]))
            got1 = await get_manifest(conn, "runs", owner_id)
            assert got1 is not None
            first_deadline = got1.deadline
            await replace_manifest(
                conn,
                _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=timedelta(hours=5)),
            )
            got2 = await get_manifest(conn, "runs", owner_id)
        assert got2 is not None
        assert got2.deadline > first_deadline

    asyncio.run(_run_test())


def test_absent_returns_none(migrated_url: str) -> None:
    """get_manifest returns None when no manifest exists for the owner."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())


def test_delete_removes_row(migrated_url: str) -> None:
    """delete_manifest removes the row; subsequent get_manifest returns None."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            await delete_manifest(conn, "runs", owner_id)
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())


def test_delete_is_idempotent(migrated_url: str) -> None:
    """delete_manifest on an absent owner does not raise; get_manifest stays None."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await delete_manifest(conn, "runs", owner_id)
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())
