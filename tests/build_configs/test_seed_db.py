"""DB-backed tests for migration 0025 and the build-config seed (ADR-0096).

These tests apply the real migrations against a disposable Postgres container and
exercise the SQL DDL (table shape, ON CONFLICT upsert) that the unit fake-conn tests
cannot reach. Requires Docker; skipped automatically when Docker is unavailable
(same gating as the existing tests/db/ suite).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import cast

import psycopg
import pytest

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH, seed_build_configs
from kdive.db import migrate
from kdive.store.objectstore import ObjectStore

# Re-use the disposable-Postgres fixtures from the db test suite.
from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]


def _columns(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: dtype for name, dtype in rows}


# ---------------------------------------------------------------------------
# Migration shape tests
# ---------------------------------------------------------------------------


def test_migration_0025_creates_build_config_catalog(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "build_config_catalog")
    assert cols.get("name") == "text"
    assert cols.get("object_key") == "text"
    assert cols.get("sha256") == "text"
    assert cols.get("description") == "text"
    assert cols.get("updated_at") == "timestamp with time zone"


def test_build_config_catalog_name_is_primary_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO build_config_catalog (name, object_key, sha256, description) "
        "VALUES ('kdump', 'system/build-configs/kdump/kdump.config', 'abc', 'test')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_conn.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description) "
            "VALUES ('kdump', 'system/build-configs/kdump/kdump.config', 'abc', 'test')"
        )


# ---------------------------------------------------------------------------
# Seed integration tests (fake object store, real DB)
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal store double that records put calls without hitting S3."""

    def __init__(self) -> None:
        self.put_keys: list[str] = []

    def put_artifact(self, request: object) -> object:
        import kdive.provider_components.artifacts as _art
        from kdive.domain.models import Sensitivity

        req = cast(_art.ArtifactWriteRequest, request)
        key = req.key()
        self.put_keys.append(key)
        return _art.StoredArtifact(
            key=key,
            etag="fake-etag",
            sensitivity=Sensitivity.REDACTED,
            retention_class="build-config",
        )


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_seed_inserts_row_with_correct_fields(migrated_url: str) -> None:
    store = _FakeStore()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            count = await seed_build_configs(conn, cast(ObjectStore, store))
        assert count == 1
        assert store.put_keys == ["system/build-configs/kdump/kdump.config"]
        expected_sha = hashlib.sha256(KDUMP_FRAGMENT_PATH.read_bytes()).hexdigest()
        with psycopg.connect(migrated_url, autocommit=True) as sync_conn:
            row = sync_conn.execute(
                "SELECT name, object_key, sha256 FROM build_config_catalog WHERE name = 'kdump'"
            ).fetchone()
        assert row is not None
        name, object_key, sha256 = row
        assert name == "kdump"
        assert object_key == "system/build-configs/kdump/kdump.config"
        assert sha256 == expected_sha

    asyncio.run(_run())


def test_seed_is_idempotent_against_real_db(migrated_url: str) -> None:
    store = _FakeStore()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await seed_build_configs(conn, cast(ObjectStore, store))
            second = await seed_build_configs(conn, cast(ObjectStore, store))
        assert first == 1
        assert second == 0
        # Only one put — the second run was a no-op.
        assert len(store.put_keys) == 1
        with psycopg.connect(migrated_url, autocommit=True) as sync_conn:
            row = sync_conn.execute("SELECT count(*) FROM build_config_catalog").fetchone()
        assert row is not None and row[0] == 1

    asyncio.run(_run())


def test_seed_upserts_on_sha_change(migrated_url: str) -> None:
    """When the stored sha differs, the seed overwrites in place (no orphaned row)."""
    store = _FakeStore()

    async def _run() -> None:
        # Seed a stale row directly.
        async with await _connect(migrated_url) as conn:
            await conn.execute(
                "INSERT INTO build_config_catalog (name, object_key, sha256, description) "
                "VALUES ('kdump', 'system/build-configs/kdump/kdump.config', 'stale', 'old')"
            )
            count = await seed_build_configs(conn, cast(ObjectStore, store))
        assert count == 1
        with psycopg.connect(migrated_url, autocommit=True) as sync_conn:
            rows = sync_conn.execute("SELECT count(*) FROM build_config_catalog").fetchone()
        assert rows is not None and rows[0] == 1  # upsert, not insert-duplicate

    asyncio.run(_run())
