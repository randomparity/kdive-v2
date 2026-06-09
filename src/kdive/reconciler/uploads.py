"""Abandoned upload repair for the reconciler."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.state import RunState, SystemState

_log = logging.getLogger(__name__)

_UPLOAD_RUN_OWNER_KIND = "runs"
_UPLOAD_SYSTEM_OWNER_KIND = "systems"
_UPLOAD_PRE_FINALIZE_VALUES = {
    _UPLOAD_RUN_OWNER_KIND: RunState.CREATED.value,
    _UPLOAD_SYSTEM_OWNER_KIND: SystemState.DEFINED.value,
}


@runtime_checkable
class UploadStore(Protocol):
    """The narrow object-store port the upload reaper consumes."""

    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


async def repair_abandoned_uploads(conn: AsyncConnection, store: UploadStore) -> int:
    """Prefix-reap uncommitted objects of pre-finalize owners past their upload deadline."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT m.owner_kind, m.owner_id FROM upload_manifests m "
            "WHERE m.deadline < now() AND ("
            "  (m.owner_kind = %s AND EXISTS ("
            "     SELECT 1 FROM runs r WHERE r.id = m.owner_id AND r.state = %s)) "
            "  OR (m.owner_kind = %s AND EXISTS ("
            "     SELECT 1 FROM systems s WHERE s.id = m.owner_id AND s.state = %s)))",
            (
                _UPLOAD_RUN_OWNER_KIND,
                _UPLOAD_PRE_FINALIZE_VALUES[_UPLOAD_RUN_OWNER_KIND],
                _UPLOAD_SYSTEM_OWNER_KIND,
                _UPLOAD_PRE_FINALIZE_VALUES[_UPLOAD_SYSTEM_OWNER_KIND],
            ),
        )
        candidates = await cur.fetchall()
    reaped = 0
    for cand in candidates:
        scope = LockScope.RUN if cand["owner_kind"] == _UPLOAD_RUN_OWNER_KIND else LockScope.SYSTEM
        if await reap_one_owner(conn, store, cand["owner_kind"], cand["owner_id"], scope):
            reaped += 1
    return reaped


async def reap_one_owner(
    conn: AsyncConnection, store: UploadStore, owner_kind: str, owner_id: UUID, scope: LockScope
) -> bool:
    """Re-validate under the per-owner lock, then prefix-reap and delete the manifest."""
    async with conn.transaction(), advisory_xact_lock(conn, scope, owner_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT prefix FROM upload_manifests "
                "WHERE owner_kind = %s AND owner_id = %s AND deadline < now()",
                (owner_kind, owner_id),
            )
            row = await cur.fetchone()
        if row is None:
            return False
        if not await owner_pre_finalize(conn, owner_kind, owner_id):
            return False
        for key in await asyncio.to_thread(store.list_prefix, row["prefix"]):
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT 1 FROM artifacts WHERE object_key = %s", (key,))
                if await cur.fetchone() is None:
                    await asyncio.to_thread(store.delete, key)
        await conn.execute(
            "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
    _log.info("reconciler: abandoned upload owner %s/%s reaped", owner_kind, owner_id)
    return True


async def owner_pre_finalize(conn: AsyncConnection, owner_kind: str, owner_id: UUID) -> bool:
    """Report whether the owner is still in its pre-finalize state."""
    if owner_kind == _UPLOAD_RUN_OWNER_KIND:
        table = _UPLOAD_RUN_OWNER_KIND
    elif owner_kind == _UPLOAD_SYSTEM_OWNER_KIND:
        table = _UPLOAD_SYSTEM_OWNER_KIND
    else:
        raise ValueError(f"unsupported upload owner kind: {owner_kind}")
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT 1 FROM {table} WHERE id = %s AND state = %s",  # noqa: S608 - 2-value whitelist
            (owner_id, _UPLOAD_PRE_FINALIZE_VALUES[owner_kind]),
        )
        return await cur.fetchone() is not None
