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
    assert first == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
        "0010",
        "0011",
        "0012",
        "0013",
        "0014",
        "0015",
        "0016",
        "0017",
        "0018",
    ]
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


def test_migration_0012_makes_audit_object_columns_nullable(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    nullable = _nullable(pg_conn, "audit_log")
    # Denial rows are object-agnostic (ADR-0062 §5), so the object columns relax to NULL.
    assert nullable.get("object_kind") == "YES"
    assert nullable.get("object_id") == "YES"
    assert nullable.get("reason") == "YES"
    # project stays NOT NULL — a member-over-reach denial always carries a resolvable one.
    assert nullable.get("project") == "NO"
    for col in ("principal", "tool", "transition", "args_digest"):
        assert nullable.get(col) == "NO", col


def test_migration_0012_check_rejects_non_denied_row_with_null_object(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO audit_log "
            "(principal, project, tool, transition, args_digest) "
            "VALUES ('alice', 'proj', 'systems.teardown', 'ready->torn_down', 'd')"
        )


def test_migration_0012_check_accepts_bare_denied_row_without_object(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO audit_log "
        "(principal, project, tool, transition, args_digest, reason) "
        "VALUES ('alice', 'proj', 'allocations.release', 'denied', 'd', 'over-reach')"
    )
    row = pg_conn.execute(
        "SELECT object_kind, object_id, reason FROM audit_log WHERE transition = 'denied'"
    ).fetchone()
    assert row == (None, None, "over-reach")


def test_migration_0012_check_accepts_destructive_gate_denied_row_with_object(
    pg_conn: psycopg.Connection,
) -> None:
    # The destructive gate's `{op}:denied` row carries its gated object → object-present
    # branch, no exemption; both denial kinds coexist under the one CHECK.
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO audit_log "
        "(principal, project, tool, object_kind, object_id, transition, args_digest) "
        "VALUES ('alice', 'proj', 'control.force_crash', 'systems', "
        "gen_random_uuid(), 'force_crash:denied', 'd')"
    )
    count = pg_conn.execute(
        "SELECT count(*) FROM audit_log WHERE transition = 'force_crash:denied'"
    ).fetchone()
    assert count is not None and count[0] == 1


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


def test_migration_0014_adds_pcie_claim_jsonb_default_empty(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "allocations")
    assert cols.get("pcie_claim") == "jsonb"
    # NOT NULL with a '[]' default: an allocation inserted without a claim reads as empty.
    assert _nullable(pg_conn, "allocations").get("pcie_claim") == "NO"
    res = pg_conn.execute(
        "INSERT INTO resources (kind, pool, cost_class, status, host_uri) "
        "VALUES ('local-libvirt', 'p', 'local', 'available', 'qemu:///system') RETURNING id"
    ).fetchone()
    assert res is not None
    row = pg_conn.execute(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, 'granted', 'alice', 'p') RETURNING pcie_claim",
        (res[0],),
    ).fetchone()
    assert row is not None and row[0] == []


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
        "0010",
        "0011",
        "0012",
        "0013",
        "0014",
        "0015",
        "0016",
        "0017",
        "0018",
    ]


# Migration 0013 seeds these named shapes (ADR-0067); the resolver and reuse read them.
_SEED_SHAPES = {
    "small": (1, 1024, 10),
    "medium": (2, 4096, 20),
    "large": (4, 8192, 40),
    "max": (8, 16384, 80),
}


def test_migration_0013_creates_system_shapes_table(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "system_shapes")
    assert cols.get("name") == "text"
    assert cols.get("vcpus") == "integer"
    assert cols.get("memory_mb") == "integer"
    assert cols.get("disk_gb") == "integer"
    assert cols.get("pcie_match") == "text"
    assert cols.get("updated_at") == "timestamp with time zone"


def test_system_shapes_name_is_primary_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    rows = pg_conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = 'public' AND tc.table_name = 'system_shapes'
        """
    ).fetchall()
    assert [r[0] for r in rows] == ["name"]


def test_system_shapes_pcie_match_is_nullable(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    nullable = _nullable(pg_conn, "system_shapes")
    assert nullable.get("pcie_match") == "YES"
    for col in ("name", "vcpus", "memory_mb", "disk_gb", "updated_at"):
        assert nullable.get(col) == "NO", col


def test_migration_0013_seeds_all_shapes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    rows = pg_conn.execute(
        "SELECT name, vcpus, memory_mb, disk_gb, pcie_match FROM system_shapes"
    ).fetchall()
    seeded = {r[0]: (r[1], r[2], r[3]) for r in rows}
    assert seeded == _SEED_SHAPES
    # Seeds carry no PCIe requirement (the matcher grammar lands later, ADR-0067).
    assert all(r[4] is None for r in rows)


def test_system_shapes_rejects_non_whole_gb_memory(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) "
            "VALUES ('odd', 1, 1500, 10)"
        )


def test_system_shapes_accepts_whole_gb_memory(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) VALUES ('whole', 2, 2048, 16)"
    )
    row = pg_conn.execute("SELECT memory_mb FROM system_shapes WHERE name = 'whole'").fetchone()
    assert row is not None and row[0] == 2048


def test_system_shapes_rejects_non_positive_sizes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    for col, value in (("vcpus", 0), ("memory_mb", 0), ("disk_gb", 0)):
        sizes = {"vcpus": 1, "memory_mb": 1024, "disk_gb": 10}
        sizes[col] = value
        with pytest.raises(psycopg.errors.CheckViolation):
            pg_conn.execute(
                "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) "
                "VALUES (%s, %s, %s, %s)",
                (f"bad-{col}", sizes["vcpus"], sizes["memory_mb"], sizes["disk_gb"]),
            )


def test_system_shapes_updated_at_trigger_bumps_on_update(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    before = pg_conn.execute("SELECT updated_at FROM system_shapes WHERE name = 'small'").fetchone()
    assert before is not None
    pg_conn.execute("UPDATE system_shapes SET disk_gb = 11 WHERE name = 'small'")
    after = pg_conn.execute("SELECT updated_at FROM system_shapes WHERE name = 'small'").fetchone()
    assert after is not None and after[0] > before[0]


# Migration 0015 records the resolved-sizing snapshot identity (ADR-0067, #161): the nullable
# `shape` name label on allocations + systems, and `requested_disk_gb` on allocations.


def test_migration_0015_adds_shape_and_disk_columns(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    alloc_cols = _columns(pg_conn, "allocations")
    assert alloc_cols.get("shape") == "text"
    assert alloc_cols.get("requested_disk_gb") == "integer"
    assert _columns(pg_conn, "systems").get("shape") == "text"


def test_migration_0015_columns_are_nullable(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    assert _nullable(pg_conn, "allocations").get("shape") == "YES"
    assert _nullable(pg_conn, "allocations").get("requested_disk_gb") == "YES"
    assert _nullable(pg_conn, "systems").get("shape") == "YES"


def test_allocations_requested_disk_gb_rejects_non_positive(pg_conn: psycopg.Connection) -> None:
    # requested_disk_gb is a size snapshot; a present value must be > 0 (the snapshot can never
    # denote a zero-disk allocation, matching the selector's disk_gb gate).
    migrate.apply_migrations(pg_conn)
    resource_id = _seed_resource_row(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO allocations (id, principal, project, resource_id, state, "
            "requested_disk_gb) VALUES (gen_random_uuid(), 'p', 'proj', %s, 'granted', 0)",
            (resource_id,),
        )


def test_allocations_requested_disk_gb_accepts_null_and_positive(
    pg_conn: psycopg.Connection,
) -> None:
    # NULL is the legacy / no-snapshot value; a positive value is the new snapshot.
    migrate.apply_migrations(pg_conn)
    resource_id = _seed_resource_row(pg_conn)
    pg_conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state) "
        "VALUES (gen_random_uuid(), 'p', 'proj', %s, 'granted')",
        (resource_id,),
    )
    pg_conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state, requested_disk_gb) "
        "VALUES (gen_random_uuid(), 'p', 'proj', %s, 'granted', 20)",
        (resource_id,),
    )


def _seed_resource_row(conn: psycopg.Connection) -> str:
    row = conn.execute(
        "INSERT INTO resources (id, kind, capabilities, pool, cost_class, status, host_uri) "
        "VALUES (gen_random_uuid(), 'local-libvirt', '{}'::jsonb, 'local-libvirt', 'local', "
        "'available', 'qemu:///system') RETURNING id"
    ).fetchone()
    assert row is not None
    return str(row[0])


# Migration 0016 adds the pending-queue surface (ADR-0069, #164): the distinct
# max_pending_allocations quota column, a nullable resource_id guarded by a CHECK, the
# persisted request-input columns a queued row re-admits from, and the backlog partial index.


def test_migration_0016_adds_max_pending_allocations(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "quotas")
    assert cols.get("max_pending_allocations") == "integer"
    assert _nullable(pg_conn, "quotas").get("max_pending_allocations") == "NO"
    # Backfills existing rows to 0 — the queue is opt-out by default.
    pg_conn.execute(
        "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
        "VALUES ('p', 1, 1)"
    )
    row = pg_conn.execute(
        "SELECT max_pending_allocations FROM quotas WHERE project = 'p'"
    ).fetchone()
    assert row is not None and row[0] == 0


def test_migration_0016_makes_resource_id_nullable(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    assert _nullable(pg_conn, "allocations").get("resource_id") == "YES"


def test_allocations_null_resource_id_only_for_unplaced_states(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    # A queued `requested` row, a cancelled-queue `released` row, and a never-placed `failed`
    # row (a budget recheck terminate / queue_timeout, #165) may carry NULL — all are queued
    # rows that never held a host (ADR-0069).
    for never_placed in ("requested", "released", "failed"):
        pg_conn.execute(
            "INSERT INTO allocations (id, principal, project, resource_id, state) "
            "VALUES (gen_random_uuid(), 'p', 'proj', NULL, %s)",
            (never_placed,),
        )
    # An ever-placed state with a NULL resource_id is rejected by the CHECK.
    for placed in ("granted", "active", "releasing", "expired"):
        with pytest.raises(psycopg.errors.CheckViolation):
            pg_conn.execute(
                "INSERT INTO allocations (id, principal, project, resource_id, state) "
                "VALUES (gen_random_uuid(), 'p', 'proj', NULL, %s)",
                (placed,),
            )


def test_migration_0016_adds_request_input_columns(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "allocations")
    assert cols.get("requested_pcie_specs") == "jsonb"
    assert cols.get("requested_kind") == "text"
    assert cols.get("requested_resource_id") == "uuid"
    # requested_pcie_specs is NOT NULL defaulting to '[]' so a non-queued row reads as empty.
    assert _nullable(pg_conn, "allocations").get("requested_pcie_specs") == "NO"
    resource_id = _seed_resource_row(pg_conn)
    row = pg_conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state) "
        "VALUES (gen_random_uuid(), 'p', 'proj', %s, 'granted') RETURNING requested_pcie_specs",
        (resource_id,),
    ).fetchone()
    assert row is not None and row[0] == []


def test_migration_0016_adds_requested_backlog_index(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_allocations_requested_created_at'"
    ).fetchone()
    assert row is not None
    definition = row[0]
    assert "created_at" in definition
    assert "requested" in definition  # partial index over the backlog only
