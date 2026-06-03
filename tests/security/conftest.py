"""Shared fixtures for the security tests.

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py`` so the audit
suite runs against the same per-test migrated schema (testcontainers Postgres).
"""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]
