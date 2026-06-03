"""Tests for the async connection-pool helper."""

from __future__ import annotations

import asyncio

import pytest

from kdive.db import pool
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_database_url_missing_raises(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_DATABASE_URL", raising=False)
    with pytest.raises(CategorizedError) as exc:
        pool.database_url()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_database_url_returns_env(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://x/y")
    assert pool.database_url() == "postgresql://x/y"


def test_create_pool_is_not_open_until_entered(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://x/y")
    created = pool.create_pool()
    assert created.closed is True


def test_pool_acquires_a_connection(postgres_url: str) -> None:
    async def _check() -> int:
        async with (
            pool.create_pool(postgres_url) as created,
            created.connection() as conn,
        ):
            cur = await conn.execute("SELECT 1")
            row = await cur.fetchone()
            assert row is not None
            return row[0]

    assert asyncio.run(_check()) == 1
