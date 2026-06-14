# Postgres Schema & Migration Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the M0 Postgres schema (the ten durable-object/ledger/audit tables from `docs/specs/m0-walking-skeleton.md`) via a minimal forward-only migration runner, plus an async connection pool from `KDIVE_DATABASE_URL`.

**Architecture:** A hand-written, forward-only runner (ADR-0015) applies `src/kdive/db/schema/NNNN_*.sql` in one advisory-lock-guarded transaction, tracked in `schema_migrations`. The runner is **synchronous** (a one-shot startup operation); the runtime **pool is async** (`psycopg_pool.AsyncConnectionPool`). Closed value sets are `text` + named `CHECK` constraints mirroring `kdive.domain.state`/`errors`; `updated_at` is trigger-maintained. Tests use a disposable Postgres via `testcontainers`.

**Tech Stack:** Python 3.13 · `psycopg` 3 (sync runner + async pool) · `psycopg-pool` · Postgres 14+ (`gen_random_uuid` core) · `testcontainers` · `pytest`.

---

## File structure

- Create `src/kdive/db/__init__.py` — empty package marker.
- Create `src/kdive/db/schema/0001_init.sql` — the M0 schema DDL (tables, CHECKs, FKs, unique constraints, `set_updated_at` trigger).
- Create `src/kdive/db/migrate.py` — sync forward-only runner: discover → validate → advisory-lock → apply pending in one transaction → record in `schema_migrations`; verify checksums of applied files.
- Create `src/kdive/db/pool.py` — `database_url()` (env, fail-fast) + `create_pool()` returning an unopened `AsyncConnectionPool`.
- Create `tests/db/__init__.py`, `tests/db/conftest.py` — testcontainers Postgres fixtures (`postgres_url` session-scoped; `pg_conn` clean-schema per test).
- Create `tests/db/test_migrate.py`, `tests/db/test_pool.py`.
- Modify `pyproject.toml` — add `psycopg-pool==3.3.1` (runtime) and `testcontainers==4.14.2` (dev).
- Create `.gitattributes` — `*.sql text eol=lf` (stable checksums, ADR-0015).
- Modify `.github/workflows/ci.yml` — set `KDIVE_REQUIRE_DOCKER=1` on the test step.

The runner-bookkeeping table `schema_migrations` is created by `migrate.py`, not by `0001_init.sql`.

---

## Task 1: Dependencies + `.gitattributes`

**Files:**
- Modify: `pyproject.toml`
- Create: `.gitattributes`
- Create: `src/kdive/db/__init__.py`, `tests/db/__init__.py`

- [ ] **Step 1: Add dependencies**

In `pyproject.toml` add `"psycopg-pool==3.3.1"` to `[project].dependencies` and `"testcontainers==4.14.2"` to `[dependency-groups].dev`.

- [ ] **Step 2: Lock + sync**

Run: `uv lock && uv sync`
Expected: resolves and installs both packages, exit 0.

- [ ] **Step 3: Create `.gitattributes`**

```
*.sql text eol=lf
```

- [ ] **Step 4: Create empty package markers**

`src/kdive/db/__init__.py` and `tests/db/__init__.py` are empty files.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .gitattributes src/kdive/db/__init__.py tests/db/__init__.py
git commit -m "build: add psycopg-pool and testcontainers; pin .sql to LF"
```

---

## Task 2: Test harness (testcontainers Postgres fixtures)

**Files:**
- Create: `tests/db/conftest.py`

- [ ] **Step 1: Write the fixtures**

```python
"""Disposable-Postgres fixtures for the db tests (ADR-0015).

`postgres_url` starts one container per session and yields a psycopg-style
conninfo. When the Docker daemon is unreachable the fixture skips, unless
`KDIVE_REQUIRE_DOCKER=1` (set in CI), which turns the skip into a hard failure so
a broken runner cannot mask the suite. `pg_conn` gives each test a connection to a
freshly-emptied `public` schema.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

_POSTGRES_IMAGE = "postgres:17"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    require_docker = os.environ.get("KDIVE_REQUIRE_DOCKER") == "1"
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - dev dep always present
        if require_docker:
            raise
        pytest.skip(f"testcontainers not installed: {exc}")

    container = PostgresContainer(_POSTGRES_IMAGE)
    try:
        container.start()
    except Exception as exc:  # Docker daemon unreachable / image pull failure.
        if require_docker:
            raise
        pytest.skip(f"Docker unavailable for testcontainers: {exc}")
    try:
        yield container.get_connection_url(driver=None)
    finally:
        container.stop()


@pytest.fixture
def pg_conn(postgres_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        yield conn
```

- [ ] **Step 2: Write a connectivity test that actually exercises the fixtures**

Create `tests/db/test_harness.py`:

```python
"""Smoke test that the disposable-Postgres fixtures connect."""

from __future__ import annotations

import psycopg


def test_pg_conn_connects(pg_conn: psycopg.Connection) -> None:
    row = pg_conn.execute("SELECT 1").fetchone()
    assert row is not None and row[0] == 1
```

- [ ] **Step 3: Run it — this is what proves the container starts and the URL API is right**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_harness.py -q`
Expected: PASS. A failure here means `get_connection_url(driver=None)` returned an
unexpected shape or Docker is misconfigured — fix the fixture before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/db/conftest.py tests/db/test_harness.py
git commit -m "test(db): add disposable-Postgres fixtures"
```

---

## Task 3: Schema DDL + migration runner

**Files:**
- Create: `src/kdive/db/schema/0001_init.sql`
- Create: `src/kdive/db/migrate.py`
- Test: `tests/db/test_migrate.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the forward-only migration runner (ADR-0015)."""

from __future__ import annotations

import hashlib
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
    "resources", "allocations", "systems", "investigations", "runs",
    "run_steps", "debug_sessions", "jobs", "artifacts", "audit_log",
}


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
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
    assert OBJECT_TABLES <= tables
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


def test_applied_file_missing_raises(pg_conn: psycopg.Connection, monkeypatch, tmp_path) -> None:
    migrate.apply_migrations(pg_conn)  # records 0001 from the real dir
    monkeypatch.setattr(migrate, "discover_migrations", lambda: [])
    with pytest.raises(migrate.MigrationError, match="missing"):
        migrate.apply_migrations(pg_conn)


def test_checksum_matches_file_bytes(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    version, checksum = pg_conn.execute(
        "SELECT version, checksum FROM schema_migrations WHERE version = '0001'"
    ).fetchone()
    sql_path = migrate.SCHEMA_DIR / "0001_init.sql"
    expected = hashlib.sha256(sql_path.read_bytes()).hexdigest()
    assert version == "0001" and checksum == expected


@pytest.mark.parametrize("constraint, enum", CHECK_ENUMS)
def test_check_constraint_covers_every_enum_value(
    pg_conn: psycopg.Connection, constraint: str, enum: type
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


def test_advisory_lock_serializes_migrators(
    pg_conn: psycopg.Connection, postgres_url: str
) -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_migrate.py -q`
Expected: import/collection error or failures — `kdive.db.migrate` and the SQL file do not exist yet.

- [ ] **Step 3: Write `0001_init.sql`**

```sql
-- 0001_init.sql — M0 walking-skeleton schema (ADR-0003, ADR-0005, ADR-0015).
-- Mirrors src/kdive/domain/{models,state,errors}.py. text + named CHECK encode the
-- closed value sets; updated_at is trigger-maintained (changed-at semantics).

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TABLE resources (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         text NOT NULL CONSTRAINT resources_kind_check
                     CHECK (kind IN ('local-libvirt')),
    capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
    pool         text NOT NULL,
    cost_class   text NOT NULL,
    status       text NOT NULL CONSTRAINT resources_status_check
                     CHECK (status IN ('available', 'degraded', 'offline')),
    host_uri     text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER resources_set_updated_at BEFORE UPDATE ON resources
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE allocations (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id      uuid NOT NULL REFERENCES resources (id),
    state            text NOT NULL CONSTRAINT allocations_state_check
                         CHECK (state IN ('requested', 'granted', 'active',
                                          'releasing', 'released', 'failed')),
    lease_expiry     timestamptz,
    capability_scope jsonb NOT NULL DEFAULT '{}'::jsonb,
    principal        text NOT NULL,
    agent_session    text,
    project          text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER allocations_set_updated_at BEFORE UPDATE ON allocations
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE systems (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    allocation_id        uuid NOT NULL REFERENCES allocations (id),
    state                text NOT NULL CONSTRAINT systems_state_check
                             CHECK (state IN ('defined', 'provisioning', 'ready',
                                              'crashed', 'torn_down', 'failed')),
    provisioning_profile jsonb NOT NULL,
    target_fingerprint   text,
    domain_name          text,
    principal            text NOT NULL,
    agent_session        text,
    project              text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER systems_set_updated_at BEFORE UPDATE ON systems
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE investigations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title         text NOT NULL,
    external_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    state         text NOT NULL CONSTRAINT investigations_state_check
                      CHECK (state IN ('open', 'active', 'closed', 'abandoned')),
    last_run_at   timestamptz,
    principal     text NOT NULL,
    agent_session text,
    project       text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER investigations_set_updated_at BEFORE UPDATE ON investigations
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE runs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investigation_id uuid NOT NULL REFERENCES investigations (id),
    system_id        uuid NOT NULL REFERENCES systems (id),
    state            text NOT NULL CONSTRAINT runs_state_check
                         CHECK (state IN ('created', 'running', 'succeeded',
                                          'failed', 'canceled')),
    build_profile    jsonb NOT NULL,
    kernel_ref       text,
    debuginfo_ref    text,
    failure_category text CONSTRAINT runs_failure_category_check
                         CHECK (failure_category IN (
                             'configuration_error', 'missing_dependency',
                             'build_failure', 'boot_timeout', 'readiness_failure',
                             'debug_attach_failure', 'infrastructure_failure',
                             'stale_handle', 'transport_conflict', 'not_implemented',
                             'allocation_denied', 'lease_expired',
                             'provisioning_failure', 'install_failure',
                             'transport_failure', 'control_failure')),
    principal        text NOT NULL,
    agent_session    text,
    project          text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER runs_set_updated_at BEFORE UPDATE ON runs
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE run_steps (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     uuid NOT NULL REFERENCES runs (id),
    step       text NOT NULL,
    state      text NOT NULL,
    result     jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_steps_run_id_step_key UNIQUE (run_id, step)
);
CREATE TRIGGER run_steps_set_updated_at BEFORE UPDATE ON run_steps
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE debug_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              uuid NOT NULL REFERENCES runs (id),
    state               text NOT NULL CONSTRAINT debug_sessions_state_check
                            CHECK (state IN ('attach', 'live', 'detached')),
    transport           text NOT NULL,
    transport_handle    text,
    worker_heartbeat_at timestamptz,
    principal           text NOT NULL,
    agent_session       text,
    project             text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER debug_sessions_set_updated_at BEFORE UPDATE ON debug_sessions
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL CONSTRAINT jobs_kind_check
                         CHECK (kind IN ('provision', 'teardown', 'build', 'install',
                                         'boot', 'force_crash', 'power',
                                         'capture_vmcore')),
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    state            text NOT NULL CONSTRAINT jobs_state_check
                         CHECK (state IN ('queued', 'running', 'succeeded',
                                          'failed', 'canceled')),
    attempt          integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL,
    worker_id        text,
    lease_expires_at timestamptz,
    heartbeat_at     timestamptz,
    result_ref       text,
    error_category   text CONSTRAINT jobs_error_category_check
                         CHECK (error_category IN (
                             'configuration_error', 'missing_dependency',
                             'build_failure', 'boot_timeout', 'readiness_failure',
                             'debug_attach_failure', 'infrastructure_failure',
                             'stale_handle', 'transport_conflict', 'not_implemented',
                             'allocation_denied', 'lease_expired',
                             'provisioning_failure', 'install_failure',
                             'transport_failure', 'control_failure')),
    authorizing      jsonb NOT NULL,
    dedup_key        text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_dedup_key_key UNIQUE (dedup_key)
);
CREATE TRIGGER jobs_set_updated_at BEFORE UPDATE ON jobs
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE artifacts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_kind      text NOT NULL,
    owner_id        uuid NOT NULL,
    object_key      text NOT NULL,
    etag            text NOT NULL,
    sensitivity     text NOT NULL CONSTRAINT artifacts_sensitivity_check
                        CHECK (sensitivity IN ('sensitive', 'redacted')),
    retention_class text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER artifacts_set_updated_at BEFORE UPDATE ON artifacts
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE audit_log (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts            timestamptz NOT NULL DEFAULT now(),
    principal     text NOT NULL,
    agent_session text,
    project       text NOT NULL,
    tool          text NOT NULL,
    object_kind   text NOT NULL,
    object_id     uuid NOT NULL,
    transition    text NOT NULL,
    args_digest   text NOT NULL
);
```

- [ ] **Step 4: Write `migrate.py`**

```python
"""Forward-only SQL migration runner (ADR-0015).

Applies ``schema/NNNN_*.sql`` in ascending order inside one advisory-lock-guarded
transaction, recording each applied file in ``schema_migrations``. Re-running is a
no-op; an edited applied file fails the checksum check. The runner is synchronous —
migration is a one-shot startup operation, distinct from the async runtime pool in
:mod:`kdive.db.pool`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

SCHEMA_DIR = Path(__file__).parent / "schema"

# Two-int advisory-lock space, reserved for migrations only (ADR-0015): application
# locks use the single-bigint form, a separate space, so they never contend.
_LOCK_CLASS_MIGRATION = 0x6B64  # "kd"
_LOCK_OBJID = 1

_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")


class MigrationError(RuntimeError):
    """A migration could not be discovered or applied (deployment/programming error)."""


@dataclass(frozen=True)
class Migration:
    """One discovered migration file."""

    version: str
    filename: str
    sql: str
    checksum: str


def discover_migrations(schema_dir: Path | None = None) -> list[Migration]:
    """Discover and validate migration files, sorted by version.

    Args:
        schema_dir: Directory of ``NNNN_*.sql`` files; defaults to the packaged
            ``schema/`` directory.

    Returns:
        Migrations sorted ascending by version.

    Raises:
        MigrationError: A filename does not match ``NNNN_*.sql`` or two files share
            a version.
    """
    directory = schema_dir if schema_dir is not None else SCHEMA_DIR
    migrations: list[Migration] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename {path.name!r} does not match NNNN_*.sql"
            )
        version = match.group(1)
        if version in seen:
            raise MigrationError(
                f"duplicate migration version {version}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        data = path.read_bytes()
        migrations.append(
            Migration(version, path.name, data.decode("utf-8"), hashlib.sha256(data).hexdigest())
        )
    migrations.sort(key=lambda m: m.version)
    return migrations


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply all pending migrations in one transaction; return versions applied now.

    Idempotent: an already-applied version is skipped after its checksum is verified
    against the file on disk. Concurrent migrators are serialized by a
    transaction-scoped advisory lock (ADR-0015).

    Args:
        conn: A psycopg connection the runner controls for the duration.

    Returns:
        The versions applied by this call (empty when the schema was already
        up to date).

    Raises:
        MigrationError: An applied migration's file is missing or its checksum no
            longer matches the recorded value.
    """
    migrations = discover_migrations()
    by_version = {m.version: m for m in migrations}
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_CLASS_MIGRATION, _LOCK_OBJID)
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    text PRIMARY KEY,
                filename   text NOT NULL,
                checksum   text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        recorded = {
            row[0]: row[1]
            for row in conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        }
        for version, checksum in recorded.items():
            migration = by_version.get(version)
            if migration is None:
                raise MigrationError(
                    f"applied migration {version} is missing from {SCHEMA_DIR}"
                )
            if migration.checksum != checksum:
                raise MigrationError(
                    f"applied migration {migration.filename} checksum changed; "
                    "applied migrations are immutable (ADR-0015)"
                )
        for migration in migrations:
            if migration.version in recorded:
                continue
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, filename, checksum) "
                "VALUES (%s, %s, %s)",
                (migration.version, migration.filename, migration.checksum),
            )
            applied_now.append(migration.version)
    return applied_now
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_migrate.py -q`
Expected: all tests PASS.

- [ ] **Step 6: Guardrails**

Run: `uv run ruff check . && uv run ruff format --check . && uv run ty check src`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/db/schema/0001_init.sql src/kdive/db/migrate.py tests/db/test_migrate.py
git commit -m "feat(db): add M0 schema and forward-only migration runner"
```

---

## Task 4: Async connection pool

**Files:**
- Create: `src/kdive/db/pool.py`
- Test: `tests/db/test_pool.py`

- [ ] **Step 1: Write the failing tests**

```python
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
        async with pool.create_pool(postgres_url) as created:
            async with created.connection() as conn:
                cur = await conn.execute("SELECT 1")
                row = await cur.fetchone()
                assert row is not None
                return row[0]

    assert asyncio.run(_check()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_pool.py -q`
Expected: import error — `kdive.db.pool` does not exist.

- [ ] **Step 3: Write `pool.py`**

```python
"""Async Postgres connection pool from the environment (ADR-0005).

The runtime core, worker, and reconciler share a :class:`AsyncConnectionPool`
built from ``KDIVE_DATABASE_URL``. Schema creation is the synchronous runner in
:mod:`kdive.db.migrate`, not this module.
"""

from __future__ import annotations

import os

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory

_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"


def database_url() -> str:
    """Return the Postgres conninfo from the environment, failing fast if unset.

    Raises:
        CategorizedError: ``KDIVE_DATABASE_URL`` is unset
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`).
    """
    url = os.environ.get(_DATABASE_URL_ENV)
    if not url:
        raise CategorizedError(
            f"{_DATABASE_URL_ENV} is not set; cannot connect to Postgres",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return url


def create_pool(conninfo: str | None = None) -> AsyncConnectionPool:
    """Build an unopened async connection pool.

    Args:
        conninfo: Postgres conninfo; read from :func:`database_url` when omitted.

    Returns:
        A pool that is not yet open — enter it (``async with``) or call
        ``await pool.open()`` before use.
    """
    return AsyncConnectionPool(conninfo or database_url(), open=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_pool.py -q`
Expected: all PASS. If `ty` flags `psycopg_pool` as unresolved, add a scoped `# ty: ignore[unresolved-import]` with a one-line justification, matching the C-extension policy in `pyproject.toml`.

- [ ] **Step 5: Guardrails**

Run: `uv run ruff check . && uv run ruff format --check . && uv run ty check src`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/pool.py tests/db/test_pool.py
git commit -m "feat(db): add async connection pool from KDIVE_DATABASE_URL"
```

---

## Task 5: CI fail-loud on missing Docker

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Set the env on the test step**

In the `lint-type-test` job's `Test` step, add `env: { KDIVE_REQUIRE_DOCKER: "1" }` so the DB suite fails rather than silently skips when Docker is unavailable on the runner (ADR-0015).

- [ ] **Step 2: Lint the workflow**

Run: `actionlint .github/workflows/ci.yml && zizmor .github/workflows/ci.yml`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: require Docker for the db test suite"
```

---

## Self-review (spec coverage)

- Tables (resources, allocations, systems, investigations, runs, run_steps, debug_sessions, jobs, artifacts, audit_log) with attribution + FKs → Task 3 `0001_init.sql`; asserted by `test_creates_all_tables`. ✓
- `run_steps UNIQUE(run_id, step)` and `jobs.dedup_key NOT NULL UNIQUE` → Task 3; asserted by `test_unique_constraints_present` / `test_dedup_key_not_null`. ✓
- Forward-only runner, `schema_migrations`, idempotent re-run → Task 3; `test_rerun_is_a_noop`. ✓
- `\d`-visible unique constraints → introspected via `information_schema` in `test_unique_constraints_present`. ✓
- Async pool from `KDIVE_DATABASE_URL` → Task 4. ✓
- Columns/enums match `domain/models.py` → CHECK lists mirror `state.py`/`errors.py`; `test_check_constraint_covers_every_enum_value` asserts **every** value of all seven lifecycle enums and the 16-member `ErrorCategory` (both columns) appears in its CHECK, and `test_state_check_rejects_unknown_value` proves a valid value inserts and an off-list value is rejected. ✓
- ADR-0015 hardening (lock-space, checksum immutability, validation rules, concurrency) → Task 3 (`apply_migrations`, `discover_migrations`); `test_checksum_mismatch_raises`, `test_bad_filename_rejected`, `test_duplicate_version_rejected`, `test_applied_file_missing_raises`, `test_advisory_lock_serializes_migrators`. ✓
- Harness actually exercised before use → Task 2 `test_harness.py::test_pg_conn_connects`. ✓

**Packaging note:** the `.sql` files live inside the `kdive.db` package; `uv_build` includes package-directory data, and `uv run` resolves them from the source tree. No extra packaging config is needed for M0/CI.
