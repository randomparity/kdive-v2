"""Tests for the forward-only migration runner (ADR-0015)."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

import psycopg
import pytest

from kdive.db import migrate
from kdive.domain import errors, models, state

# Each lifecycle/category CHECK constraint and the enum it must mirror (ADR-0015).
CHECK_ENUMS = [
    ("resources_status_check", state.ResourceStatus),
    ("allocations_state_check", state.AllocationState),
    ("systems_state_check", state.SystemState),
    ("investigations_state_check", state.InvestigationState),
    ("runs_state_check", state.RunState),
    ("debug_sessions_state_check", state.DebugSessionState),
    ("jobs_state_check", state.JobState),
    ("jobs_kind_check", models.JobKind),
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

# Tables migration 0002 adds (M1 accounting/admission data layer).
M1_TABLES = {
    "cost_class_coefficients",
    "budgets",
    "quotas",
    "ledger",
    "idempotency_keys",
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
    assert first == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"]
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


def test_runs_expected_boot_failure_column(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    columns = _columns(pg_conn, "runs")
    assert columns["expected_boot_failure"] == "jsonb"


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


def _columns(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: dtype for name, dtype in rows}


def _indexed_columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (i.indkey)
        WHERE t.relname = %s
        """,
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def _nullable(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: is_nullable for name, is_nullable in rows}


def test_migration_0005_creates_platform_audit_log(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    assert "platform_audit_log" in _tables(pg_conn)


def test_platform_audit_log_platform_role_is_nullable(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    nullable = _nullable(pg_conn, "platform_audit_log")
    # The audited granted-set member read carries no platform role (ADR-0043 §4).
    assert nullable.get("platform_role") == "YES"
    assert nullable.get("agent_session") == "YES"
    # Identity/attribution columns are NOT NULL — a row without them is unaccountable.
    for col in ("principal", "tool", "scope", "args_digest"):
        assert nullable.get(col) == "NO", col


def test_platform_audit_log_accepts_null_platform_role_row(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO platform_audit_log (principal, tool, scope, args_digest) "
        "VALUES ('alice', 'accounting.report', 'granted-set:a,b', 'deadbeef')"
    )
    row = pg_conn.execute(
        "SELECT platform_role, agent_session FROM platform_audit_log WHERE principal = 'alice'"
    ).fetchone()
    assert row == (None, None)


def test_migration_0002_creates_accounting_tables(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    assert _tables(pg_conn) >= M1_TABLES


def test_seed_coefficient_row_present(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT coeff FROM cost_class_coefficients WHERE cost_class = 'local'"
    ).fetchone()
    assert row is not None and float(row[0]) == 1.0


def test_budgets_spent_kcu_defaults_to_zero(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute("INSERT INTO budgets (project, limit_kcu) VALUES ('p', 100)")
    row = pg_conn.execute("SELECT spent_kcu FROM budgets WHERE project = 'p'").fetchone()
    assert row is not None and float(row[0]) == 0.0


def test_idempotency_keys_primary_key_is_principal_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    rows = pg_conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = 'public' AND tc.table_name = 'idempotency_keys'
        ORDER BY kcu.ordinal_position
        """
    ).fetchall()
    assert [r[0] for r in rows] == ["principal", "key"]


def test_idempotency_keys_dedup_same_principal_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
        "VALUES ('k1', 'alice', 'p', 'request', '{}'::jsonb)"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_conn.execute(
            "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
            "VALUES ('k1', 'alice', 'p', 'request', '{}'::jsonb)"
        )


def test_idempotency_key_is_principal_scoped_not_global(pg_conn: psycopg.Connection) -> None:
    # The same client-chosen key under two principals must coexist (no cross-tenant
    # collision) — the reason the PK is (principal, key), not (key).
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
        "VALUES ('shared', 'alice', 'p', 'request', '{}'::jsonb)"
    )
    pg_conn.execute(
        "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
        "VALUES ('shared', 'bob', 'p', 'request', '{}'::jsonb)"
    )
    row = pg_conn.execute("SELECT count(*) FROM idempotency_keys WHERE key = 'shared'").fetchone()
    assert row is not None and row[0] == 2


def test_ledger_event_type_check_and_indexes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    indexed = _indexed_columns(pg_conn, "ledger")
    assert {"project", "allocation_id"} <= indexed
    res = pg_conn.execute(
        "INSERT INTO resources (kind, pool, cost_class, status, host_uri) "
        "VALUES ('local-libvirt', 'p', 'local', 'available', 'qemu:///system') RETURNING id"
    ).fetchone()
    assert res is not None
    alloc = pg_conn.execute(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, 'granted', 'alice', 'p') RETURNING id",
        (res[0],),
    ).fetchone()
    assert alloc is not None
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO ledger (project, allocation_id, resource_id, cost_class, "
            "event_type, kcu_delta) VALUES ('p', %s, %s, 'local', 'bogus', 1)",
            (alloc[0], res[0]),
        )


def test_allocations_gain_m1_size_and_billing_columns(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "allocations")
    assert cols.get("requested_vcpus") == "integer"
    assert cols.get("requested_memory_gb") == "integer"
    assert cols.get("active_started_at") == "timestamp with time zone"
    assert cols.get("active_ended_at") == "timestamp with time zone"


def test_widened_state_checks_accept_new_values(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    res = pg_conn.execute(
        "INSERT INTO resources (kind, pool, cost_class, status, host_uri) "
        "VALUES ('local-libvirt', 'p', 'local', 'available', 'qemu:///system') RETURNING id"
    ).fetchone()
    assert res is not None
    alloc = pg_conn.execute(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, 'expired', 'alice', 'p') RETURNING id",
        (res[0],),
    ).fetchone()
    assert alloc is not None  # 'expired' accepted by the widened CHECK
    sysm = pg_conn.execute(
        "INSERT INTO systems (allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, 'reprovisioning', '{}'::jsonb, 'alice', 'p') RETURNING id",
        (alloc[0],),
    ).fetchone()
    assert sysm is not None  # 'reprovisioning' accepted by the widened CHECK


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
    assert migrate.apply_migrations(pg_conn) == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
    ]
