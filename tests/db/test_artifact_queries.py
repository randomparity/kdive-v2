"""Tests for shared artifact lookup queries."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg

from kdive.db.artifact_queries import raw_vmcore_key


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _artifact(
    conn: psycopg.AsyncConnection,
    owner_id: UUID,
    object_key: str,
    *,
    sensitivity: str = "sensitive",
) -> None:
    await conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES ('systems', %s, %s, 'etag', %s, 'vmcore')",
        (owner_id, object_key, sensitivity),
    )


def test_raw_vmcore_key_returns_only_matching_unredacted_system_key(migrated_url: str) -> None:
    async def _run() -> None:
        system_id = uuid4()
        other_system_id = uuid4()
        raw_key = f"local/systems/{system_id}/vmcore-host_dump"
        async with await _connect(migrated_url) as conn:
            await _artifact(conn, system_id, raw_key)
            await _artifact(conn, system_id, f"{raw_key}-redacted", sensitivity="redacted")
            await _artifact(conn, system_id, f"local/systems/{system_id}/console-log")
            await _artifact(conn, other_system_id, f"local/systems/{other_system_id}/vmcore-kdump")

            assert await raw_vmcore_key(conn, system_id) == raw_key
            assert await raw_vmcore_key(conn, uuid4()) is None

    asyncio.run(_run())
