"""Tests for the forward-only migration runner (ADR-0015)."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

import psycopg
import pytest

from kdive.db import migrate
from kdive.domain import errors, state

# Each lifecycle/category CHECK constraint and the enum it must mirror (ADR-0015).
CHECK_ENUMS = [
    ("resources_status_check", state.ResourceStatus),
    ("allocations_state_check", state.AllocationState),
    ("systems_state_check", state.SystemState),
    ("investigations_state_check", state.InvestigationState),
    ("runs_state_check", state.RunState),
    ("debug_sessions_state_check", state.DebugSessionState),
    ("jobs_state_check", state.JobState),
    ("runs_failure_category_check", errors.ErrorCategory),
    ("jobs_error_category_check", errors.ErrorCategory),
]

OBJECT_TABLES = {
    "resources",
    "allocations",
    "systems",
    "investigations",
    "runs",
    "run_steps",
    "debug_sessions",
    "jobs",
    "artifacts",
    "audit_log",
}


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _unique_constraints(conn: psycopg.Connection, table: str) -> set[frozenset[str]]:
    rows = conn.execute(
        """
        SELECT kcu.column_name, tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'UNIQUE'
          AND tc.table_schema = 'public' AND tc.table_name = %s
        """,
        (table,),
    ).fetchall()
    by_constraint: dict[str, set[str]] = {}
    for column, constraint in rows:
        by_constraint.setdefault(constraint, set()).add(column)
    return {frozenset(cols) for cols in by_constraint.values()}


def test_creates_all_tables(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    tables = _tables(pg_conn)
    assert tables >= OBJECT_TABLES
    assert "schema_migrations" in tables


def test_rerun_is_a_noop(pg_conn: psycopg.Connection) -> None:
    first = migrate.apply_migrations(pg_conn)
    second = migrate.apply_migrations(pg_conn)
    assert first == ["0001"]
    assert second == []


def test_unique_constraints_present(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    assert frozenset({"run_id", "step"}) in _unique_constraints(pg_conn, "run_steps")
    assert frozenset({"dedup_key"}) in _unique_constraints(pg_conn, "jobs")


def test_dedup_key_not_null(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'jobs' AND column_name = 'dedup_key'"
    ).fetchone()
    assert row is not None and row[0] == "NO"


def test_state_check_rejects_unknown_value(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO resources (kind, pool, cost_class, status, host_uri) "
        "VALUES ('local-libvirt', 'p', 'c', 'available', 'qemu:///system')"
    )
    res = pg_conn.execute("SELECT id FROM resources").fetchone()
    assert res is not None
    resource_id = res[0]
    pg_conn.execute(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, 'granted', 'alice', 'proj')",
        (resource_id,),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO allocations (resource_id, state, principal, project) "
            "VALUES (%s, 'bogus', 'alice', 'proj')",
            (resource_id,),
        )


def test_checksum_mismatch_raises(pg_conn: psycopg.Connection, monkeypatch) -> None:
    migrate.apply_migrations(pg_conn)
    real = migrate.discover_migrations()

    def tampered() -> list[migrate.Migration]:
        m = real[0]
        return [migrate.Migration(m.version, m.filename, m.sql + "\n-- edit", "deadbeef")]

    monkeypatch.setattr(migrate, "discover_migrations", tampered)
    with pytest.raises(migrate.MigrationError, match="checksum"):
        migrate.apply_migrations(pg_conn)


def test_bad_filename_rejected(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="filename"):
        migrate.discover_migrations(tmp_path)


def test_duplicate_version_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 2;")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.discover_migrations(tmp_path)


def test_missing_schema_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(migrate.MigrationError, match="does not exist"):
        migrate.discover_migrations(tmp_path / "nope")


def test_apply_requires_idle_connection(postgres_url: str) -> None:
    with psycopg.connect(postgres_url) as conn:  # autocommit=False
        conn.execute("SELECT 1")  # opens a server-side transaction
        with pytest.raises(migrate.MigrationError, match="open transaction"):
            migrate.apply_migrations(conn)
        conn.rollback()


def test_applied_file_missing_raises(pg_conn: psycopg.Connection, monkeypatch) -> None:
    migrate.apply_migrations(pg_conn)  # records 0001 from the real dir
    monkeypatch.setattr(migrate, "discover_migrations", lambda: [])
    with pytest.raises(migrate.MigrationError, match="missing"):
        migrate.apply_migrations(pg_conn)


def test_checksum_matches_file_bytes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    found = pg_conn.execute(
        "SELECT version, checksum FROM schema_migrations WHERE version = '0001'"
    ).fetchone()
    assert found is not None
    version, checksum = found
    sql_path = migrate.SCHEMA_DIR / "0001_init.sql"
    expected = hashlib.sha256(sql_path.read_bytes()).hexdigest()
    assert version == "0001" and checksum == expected


@pytest.mark.parametrize("constraint, enum", CHECK_ENUMS)
def test_check_constraint_covers_every_enum_value(
    pg_conn: psycopg.Connection, constraint: str, enum: type[StrEnum]
) -> None:
    """Every Python enum value must appear in its SQL CHECK (ties SQL to the model)."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        (constraint,),
    ).fetchone()
    assert row is not None, f"constraint {constraint} not found in the schema"
    definition = row[0]
    missing = [m.value for m in enum if f"'{m.value}'" not in definition]
    assert not missing, f"{constraint} is missing enum values {missing}"


def test_advisory_lock_serializes_migrators(pg_conn: psycopg.Connection, postgres_url: str) -> None:
    """A second migrator blocks on the migration advisory lock until the first frees it."""
    with psycopg.connect(postgres_url) as holder:  # autocommit=False: holds the xact lock
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (migrate._LOCK_CLASS_MIGRATION, migrate._LOCK_OBJID),
        )
        pg_conn.execute("SET lock_timeout = '500ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            migrate.apply_migrations(pg_conn)
        holder.rollback()  # release the lock
    pg_conn.execute("SET lock_timeout = '0'")
    assert migrate.apply_migrations(pg_conn) == ["0001"]
