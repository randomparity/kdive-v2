"""Smoke test that the disposable-Postgres fixtures connect."""

from __future__ import annotations

import psycopg


def test_pg_conn_connects(pg_conn: psycopg.Connection) -> None:
    row = pg_conn.execute("SELECT 1").fetchone()
    assert row is not None and row[0] == 1
