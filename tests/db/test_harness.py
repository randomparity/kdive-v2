"""Smoke test that the disposable-Postgres fixtures connect."""

from __future__ import annotations

import asyncio

import psycopg


def test_pg_conn_connects(pg_conn: psycopg.Connection) -> None:
    row = pg_conn.execute("SELECT 1").fetchone()
    assert row is not None and row[0] == 1


def test_migrated_url_has_schema(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            cur = await conn.execute("SELECT to_regclass('public.runs')")
            row = await cur.fetchone()
            assert row is not None and row[0] is not None

    asyncio.run(_run())
