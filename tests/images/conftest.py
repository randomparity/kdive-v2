"""Shared fixtures for the images tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so the catalog
resolver and seed tests run against the same per-test migrated schema.
"""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]
