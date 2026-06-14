"""Fixtures for the inventory tests.

Re-exports the disposable-Postgres fixtures (ADR-0015) so the CLI reconcile tests run
against a freshly-migrated schema, the same shapes the integration suite uses.
"""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
