"""Migration 0027 — build_hosts + build_host_leases schema, CHECKs, FK, and seed (ADR-0099).

build_hosts is the selection seam for remote build-host inventory; build_host_leases
counts in-flight builds per host under the BUILD_HOST advisory lock. The local
fallback row ('worker-local') is seeded with a fixed UUID so code can reference it
without a lookup.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from kdive.db import migrate


def test_worker_local_seed(pg_conn: psycopg.Connection) -> None:
    """The seed row for the local fallback host is present after migration."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT kind, enabled, state FROM build_hosts WHERE name = 'worker-local'"
    ).fetchone()
    assert row is not None
    assert row == ("local", True, "ready")


def test_build_host_leases_fk_restrict(pg_conn: psycopg.Connection) -> None:
    """Inserting a lease row with a non-existent build_host_id raises ForeignKeyViolation."""
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_conn.execute(
            "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
            (str(uuid.uuid4()), str(uuid.uuid4())),
        )


def test_ssh_fields_check_rejects_ssh_with_null_address(pg_conn: psycopg.Connection) -> None:
    """Inserting kind='ssh' with NULL address violates build_hosts_ssh_fields_check."""
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            """
            INSERT INTO build_hosts (name, kind, workspace_root, max_concurrent)
            VALUES ('bad-ssh', 'ssh', '/tmp/build', 4)
            """
        )


def test_ssh_fields_check_accepts_ssh_with_all_fields(pg_conn: psycopg.Connection) -> None:
    """Inserting kind='ssh' with address and ssh_credential_ref set succeeds."""
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        """
        INSERT INTO build_hosts
            (name, kind, address, ssh_credential_ref, workspace_root, max_concurrent)
        VALUES ('ssh-host', 'ssh', '10.0.0.1', 'cred/prod', '/build', 4)
        """
    )
    row = pg_conn.execute(
        "SELECT kind, address FROM build_hosts WHERE name = 'ssh-host'"
    ).fetchone()
    assert row is not None
    assert row == ("ssh", "10.0.0.1")
