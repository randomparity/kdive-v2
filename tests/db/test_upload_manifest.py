"""Tests for owner-scoped upload-manifest storage (ADR-0048 §4)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import psycopg

from kdive.db.upload_manifest import ManifestEntry, delete_manifest, get_manifest, replace_manifest


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_round_trip(migrated_url: str) -> None:
    """replace_manifest then get_manifest returns the entries, prefix, and a deadline."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10), ManifestEntry("vmlinux", "YmFy", 20)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=owner_id,
                prefix=f"local/runs/{owner_id}/",
                entries=entries,
                ttl=timedelta(hours=1),
            )
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert got.entries == tuple(entries)
        assert got.prefix == f"local/runs/{owner_id}/"
        assert got.deadline is not None

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
            await replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=owner_id,
                prefix=f"local/runs/{owner_id}/",
                entries=first_entries,
                ttl=timedelta(hours=1),
            )
            await replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=owner_id,
                prefix=f"local/runs/{owner_id}/v2/",
                entries=second_entries,
                ttl=timedelta(hours=2),
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
            await replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=owner_id,
                prefix=f"local/runs/{owner_id}/",
                entries=[ManifestEntry("kernel", "Zm9v", 10)],
                ttl=timedelta(hours=1),
            )
            got1 = await get_manifest(conn, "runs", owner_id)
            assert got1 is not None
            first_deadline = got1.deadline
            await replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=owner_id,
                prefix=f"local/runs/{owner_id}/",
                entries=[ManifestEntry("kernel", "Zm9v", 10)],
                ttl=timedelta(hours=5),
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
            await replace_manifest(
                conn,
                owner_kind="runs",
                owner_id=owner_id,
                prefix=f"local/runs/{owner_id}/",
                entries=entries,
                ttl=timedelta(hours=1),
            )
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
