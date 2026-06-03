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
            raise MigrationError(f"migration filename {path.name!r} does not match NNNN_*.sql")
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
        The versions applied by this call (empty when the schema was already up to
        date).

    Raises:
        MigrationError: An applied migration's file is missing or its checksum no
            longer matches the recorded value.
    """
    migrations = discover_migrations()
    by_version = {m.version: m for m in migrations}
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_CLASS_MIGRATION, _LOCK_OBJID))
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
                raise MigrationError(f"applied migration {version} is missing from {SCHEMA_DIR}")
            if migration.checksum != checksum:
                raise MigrationError(
                    f"applied migration {migration.filename} checksum changed; "
                    "applied migrations are immutable (ADR-0015)"
                )
        for migration in migrations:
            if migration.version in recorded:
                continue
            # bytes (not a dynamic str) so the parameterless multi-statement file
            # type-checks against psycopg's LiteralString-or-bytes query overload.
            conn.execute(migration.sql.encode())
            conn.execute(
                "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.filename, migration.checksum),
            )
            applied_now.append(migration.version)
    return applied_now
