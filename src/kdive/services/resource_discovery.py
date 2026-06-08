"""Resource discovery registration service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.discovery import DiscoverySource, ResourceRecord
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Resource, ResourceKind


async def register_discovered_resource(
    conn: AsyncConnection,
    record: ResourceRecord,
    *,
    pool: str,
    cost_class: str,
) -> Resource:
    """Upsert one discovered Resource by ``(kind, resource_id)``."""
    resource = _resource_from_record(record, pool=pool, cost_class=cost_class)
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RESOURCE, _resource_key(resource)),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
            (resource.kind.value, resource.host_uri),
        )
        existing = await cur.fetchone()
        if existing is not None:
            await cur.execute(
                "UPDATE resources SET capabilities = %s, status = %s, pool = %s, "
                "cost_class = %s WHERE id = %s RETURNING *",
                (
                    Jsonb(resource.capabilities),
                    resource.status.value,
                    resource.pool,
                    resource.cost_class,
                    existing["id"],
                ),
            )
        else:
            await _insert_resource(cur, resource)
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT/UPDATE ... RETURNING always yields one row.
        raise RuntimeError("resource registration returned no row")
    return Resource.model_validate(row)


async def ensure_discovered_resource_registered(
    pool: AsyncConnectionPool,
    discovery: DiscoverySource,
    *,
    kind: ResourceKind,
    resource_id: str,
    pool_name: str,
    cost_class: str,
) -> None:
    """Insert the target discovered Resource only when it is absent."""
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RESOURCE, _resource_key(kind, resource_id)),
    ):
        if await _resource_exists(conn, kind, resource_id):
            return
        record = _select_record(discovery.list_resources(), kind=kind, resource_id=resource_id)
        resource = _resource_from_record(record, pool=pool_name, cost_class=cost_class)
        async with conn.cursor(row_factory=dict_row) as cur:
            await _insert_resource(cur, resource)


async def _resource_exists(conn: AsyncConnection, kind: ResourceKind, resource_id: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM resources WHERE kind = %s AND host_uri = %s",
            (kind.value, resource_id),
        )
        return await cur.fetchone() is not None


def _select_record(
    records: list[ResourceRecord], *, kind: ResourceKind, resource_id: str
) -> ResourceRecord:
    for record in records:
        if record["kind"] == kind and record["resource_id"] == resource_id:
            return record
    raise CategorizedError(
        "discovery source did not return the requested resource",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"kind": kind.value, "resource_id": resource_id},
    )


def _resource_from_record(record: ResourceRecord, *, pool: str, cost_class: str) -> Resource:
    now = datetime.now(UTC)
    return Resource(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=record["kind"],
        capabilities=record["capabilities"],
        pool=pool,
        cost_class=cost_class,
        status=record["status"],
        host_uri=record["resource_id"],
    )


def _resource_key(kind: ResourceKind | Resource, resource_id: str | None = None) -> str:
    if isinstance(kind, Resource):
        return f"{kind.kind.value}:{kind.host_uri}"
    if resource_id is None:
        raise ValueError("resource_id is required when kind is not a Resource")
    return f"{kind.value}:{resource_id}"


async def _insert_resource(cur: AsyncCursor[dict[str, Any]], resource: Resource) -> None:
    await cur.execute(
        """
        INSERT INTO resources (id, kind, capabilities, pool, cost_class, status, host_uri)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            resource.id,
            resource.kind.value,
            Jsonb(resource.capabilities),
            resource.pool,
            resource.cost_class,
            resource.status.value,
            resource.host_uri,
        ),
    )
