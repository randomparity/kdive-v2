"""App-level build-config seed (ADR-0096).

The SQL migration creates the table; this step publishes the packaged kdump fragment to a
fixed reserved object-store key and upserts the catalog row, idempotently. The bytes go to the
object store via ``put_artifact`` (object-store write only — NOT ``register_artifact_row``, so
no project-scoped artifacts row and none of its TTL/owner lifecycle, per ADR-0096). The reserved
key is deterministic in (tenant, owner_kind, owner_id, name), so an edited fragment overwrites
in place — no orphaned object. ``Sensitivity.REDACTED`` marks the fragment serve-eligible (the
``buildconfig.get`` tool serves it); it carries no secret.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest
from kdive.store.objectstore import ObjectStore

KDUMP_FRAGMENT_PATH = Path(__file__).parent / "data" / "kdump.config"
_KDUMP_NAME = "kdump"
_KDUMP_DESCRIPTION = "kdump/debuginfo kernel-config fragment"
_TENANT = "system"
_OWNER_KIND = "build-configs"
_RETENTION_CLASS = "build-config"


async def _stored_sha(conn: AsyncConnection, name: str) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sha256 FROM build_config_catalog WHERE name = %(name)s", {"name": name}
        )
        row = await cur.fetchone()
    return row["sha256"] if row is not None else None


async def _upsert(
    conn: AsyncConnection, name: str, object_key: str, sha256: str, desc: str
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = EXCLUDED.description, updated_at = now()",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": desc},
        )


async def seed_build_configs(conn: AsyncConnection, store: ObjectStore) -> int:
    """Publish the packaged kdump fragment + upsert its row. Returns the count published (0 or 1).

    Idempotent: when the stored sha256 already matches the packaged bytes, nothing is written.

    Args:
        conn: An open async psycopg connection (autocommit recommended).
        store: The object store to publish bytes into.

    Returns:
        The number of fragments published (0 if idempotent skip, 1 if published/updated).
    """
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    if await _stored_sha(conn, _KDUMP_NAME) == sha256:
        return 0
    stored = store.put_artifact(
        ArtifactWriteRequest(
            tenant=_TENANT,
            owner_kind=_OWNER_KIND,
            owner_id=_KDUMP_NAME,
            name="kdump.config",
            data=data,
            sensitivity=Sensitivity.REDACTED,
            retention_class=_RETENTION_CLASS,
        )
    )
    await _upsert(conn, _KDUMP_NAME, stored.key, sha256, _KDUMP_DESCRIPTION)
    return 1
