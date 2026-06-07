"""Shared allocation budget and idempotency helpers."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation


async def within_budget(conn: AsyncConnection, project: str, estimate: Decimal) -> bool:
    """Report whether ``(limit_kcu - spent_kcu) >= estimate`` for ``project``."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT limit_kcu, spent_kcu FROM budgets WHERE project = %s", (project,))
        row = await cur.fetchone()
    if row is None:
        return False
    remaining = Decimal(row[0]) - Decimal(row[1])
    return remaining >= estimate


async def resolve_replay(
    conn: AsyncConnection,
    *,
    principal: str,
    key: str,
    kind: str,
    operation_label: str,
) -> Allocation | None:
    """Return the Allocation stored for a prior key under ``kind``, or ``None``."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT result FROM idempotency_keys WHERE principal = %s AND key = %s AND kind = %s",
            (principal, key, kind),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    allocation_id = UUID(row[0]["allocation_id"])
    allocation = await ALLOCATIONS.get(conn, allocation_id)
    if allocation is None:
        raise RuntimeError(
            f"{operation_label} idempotency key ({principal}, {key}) references missing "
            f"allocation {allocation_id}"
        )
    return allocation


async def record_key(
    conn: AsyncConnection,
    *,
    principal: str,
    key: str,
    project: str,
    kind: str,
    allocation_id: UUID,
) -> None:
    """Record ``(principal, key)`` for an allocation operation."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    key,
                    principal,
                    project,
                    kind,
                    Jsonb({"allocation_id": str(allocation_id)}),
                ),
            )
    except UniqueViolation as exc:
        raise CategorizedError(
            f"idempotency key ({principal}, {key}) is already in use",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"principal": principal},
        ) from exc
