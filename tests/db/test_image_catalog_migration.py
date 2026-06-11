"""Migration 0023 — image_catalog schema, CHECKs, and partial unique indexes (ADR-0092/0093).

The DB-level invariants are the catalog's safety net: a private row must carry an owner and an
expiry; a `defined` row must have no object and a non-`defined` row must; one registered public
image per identity and one registered private image per (owner, provider, name); but a `pending`
duplicate is admitted so a crashed publish never wedges retry.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from kdive.db import migrate
from kdive.domain.models import ImageState, ImageVisibility


def _columns(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: dtype for name, dtype in rows}


def _nullable(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: is_nullable for name, is_nullable in rows}


def _insert_image(conn: psycopg.Connection, **overrides: object) -> None:
    """Insert one image_catalog row, defaulting to a registered public image."""
    row: dict[str, object] = {
        "provider": "local-libvirt",
        "name": "base",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "object_key": "images/local-libvirt/base/x86_64.qcow2",
        "digest": "sha256:abc",
        "visibility": "public",
        "owner": None,
        "expires_at": None,
        "state": "registered",
    }
    row.update(overrides)
    columns = list(row.keys())
    query = sql.SQL("INSERT INTO image_catalog ({cols}) VALUES ({vals})").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
    )
    conn.execute(query, row)


def test_migration_0023_creates_image_catalog(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "image_catalog")
    assert cols.get("provider") == "text"
    assert cols.get("name") == "text"
    assert cols.get("arch") == "text"
    assert cols.get("format") == "text"
    assert cols.get("root_device") == "text"
    assert cols.get("object_key") == "text"
    assert cols.get("digest") == "text"
    assert cols.get("capabilities") == "ARRAY"
    assert cols.get("provenance") == "jsonb"
    assert cols.get("visibility") == "text"
    assert cols.get("owner") == "text"
    assert cols.get("expires_at") == "timestamp with time zone"
    assert cols.get("state") == "text"
    assert cols.get("pending_since") == "timestamp with time zone"
    assert cols.get("created_at") == "timestamp with time zone"
    assert cols.get("updated_at") == "timestamp with time zone"


def test_nullable_columns(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    nullable = _nullable(pg_conn, "image_catalog")
    # object_key/digest/owner/expires_at are conditionally present (CHECK-bound), so nullable.
    for col in ("object_key", "digest", "owner", "expires_at"):
        assert nullable.get(col) == "YES", col
    # identity + state columns are always present.
    for col in ("provider", "name", "arch", "format", "root_device", "visibility", "state"):
        assert nullable.get(col) == "NO", col


def test_visibility_check_rejects_unknown(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="internal")


def test_state_check_rejects_unknown(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="published")


def test_private_row_requires_owner(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="private", owner=None, expires_at="2099-01-01T00:00:00Z")


def test_public_row_rejects_owner(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="public", owner="proj", expires_at=None)


def test_private_row_requires_expiry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="private", owner="proj", expires_at=None)


def test_public_row_rejects_expiry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="public", owner=None, expires_at="2099-01-01T00:00:00Z")


def test_private_row_accepts_owner_and_expiry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj",
        expires_at="2099-01-01T00:00:00Z",
    )
    row = pg_conn.execute("SELECT owner FROM image_catalog WHERE visibility = 'private'").fetchone()
    assert row is not None and row[0] == "proj"


def test_defined_row_requires_null_object_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="defined", object_key="images/x", digest=None)


def test_non_defined_row_requires_object_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="pending", object_key=None, digest=None)


def test_defined_row_accepts_null_object_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="defined", object_key=None, digest=None)
    row = pg_conn.execute("SELECT object_key FROM image_catalog WHERE state = 'defined'").fetchone()
    assert row is not None and row[0] is None


def test_two_registered_public_same_identity_rejected(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, object_key="images/a")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_image(pg_conn, object_key="images/b")


def test_pending_duplicate_admitted(pg_conn: psycopg.Connection) -> None:
    # A crashed publish's leftover `pending` row must never block a re-publish of the same
    # identity, so the partial unique index covers `registered` only.
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="pending", object_key="images/a")
    _insert_image(pg_conn, state="pending", object_key="images/b")
    row = pg_conn.execute("SELECT count(*) FROM image_catalog WHERE state = 'pending'").fetchone()
    assert row is not None and row[0] == 2


def test_registered_coexists_with_pending_same_identity(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="registered", object_key="images/a")
    _insert_image(pg_conn, state="pending", object_key="images/b")


def test_two_defined_public_same_identity_rejected(pg_conn: psycopg.Connection) -> None:
    # Seed idempotency at the DB level: one `defined` baseline per public identity.
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="defined", object_key=None, digest=None)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_image(pg_conn, state="defined", object_key=None, digest=None)


def test_two_registered_private_same_identity_rejected(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj",
        expires_at="2099-01-01T00:00:00Z",
        object_key="images/a",
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_image(
            pg_conn,
            visibility="private",
            owner="proj",
            expires_at="2099-01-01T00:00:00Z",
            object_key="images/b",
        )


def test_two_projects_register_same_private_name(pg_conn: psycopg.Connection) -> None:
    # The private unique index is keyed by (owner, provider, name): two projects may both hold
    # a registered private image of the same name.
    migrate.apply_migrations(pg_conn)
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj-a",
        expires_at="2099-01-01T00:00:00Z",
        object_key="images/a",
    )
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj-b",
        expires_at="2099-01-01T00:00:00Z",
        object_key="images/b",
    )


def test_updated_at_trigger_bumps_on_update(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="pending", object_key="images/a")
    before = pg_conn.execute(
        "SELECT updated_at FROM image_catalog WHERE state = 'pending'"
    ).fetchone()
    assert before is not None
    pg_conn.execute("UPDATE image_catalog SET state = 'registered' WHERE state = 'pending'")
    after = pg_conn.execute(
        "SELECT updated_at FROM image_catalog WHERE state = 'registered'"
    ).fetchone()
    assert after is not None and after[0] > before[0]


def test_state_check_covers_every_enum_value(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'image_state_check'"
    ).fetchone()
    assert row is not None
    definition = row[0]
    missing = [m.value for m in ImageState if f"'{m.value}'" not in definition]
    assert not missing, f"image_state_check is missing {missing}"


def test_visibility_check_covers_every_enum_value(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'image_visibility_check'"
    ).fetchone()
    assert row is not None
    definition = row[0]
    missing = [m.value for m in ImageVisibility if f"'{m.value}'" not in definition]
    assert not missing, f"image_visibility_check is missing {missing}"
