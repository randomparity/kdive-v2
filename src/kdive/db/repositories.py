"""Typed async CRUD over the M0 durable objects (ADR-0003, ADR-0016).

A base `Repository[M]` provides `insert` / `get`; `StatefulRepository[M, S]` adds
`update_state`, guarded by `kdive.domain.state.can_transition` and bound to the
object's state enum `S`. Module-level instances bind these to each table. Rows map to
Pydantic models field-for-column; the database owns the `created_at` / `updated_at`
timestamps (they are omitted from inserts and read back via `RETURNING *`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.models import (
    Allocation,
    Artifact,
    DebugSession,
    DomainModel,
    Investigation,
    Job,
    Resource,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
    ensure_transition,
)

# DB-authoritative columns, omitted from inserts so their defaults/trigger apply.
_SERVER_GENERATED = ("created_at", "updated_at")


class ObjectNotFound(RuntimeError):
    """An `update_state` target id does not exist — a consistency error."""


class Repository[M: DomainModel]:
    """Async `insert` / `get` for one durable-object table."""

    def __init__(
        self,
        model: type[M],
        table: str,
        *,
        json_columns: frozenset[str] = frozenset(),
    ) -> None:
        self._model = model
        self._table = table
        self._json_columns = json_columns
        self._insert_columns = tuple(
            name for name in model.model_fields if name not in _SERVER_GENERATED
        )

    def _insert_params(self, obj: M) -> dict[str, Any]:
        dumped = obj.model_dump()
        return {
            name: Jsonb(dumped[name]) if name in self._json_columns else dumped[name]
            for name in self._insert_columns
        }

    async def insert(self, conn: AsyncConnection, obj: M) -> M:
        """Insert ``obj`` and return it as persisted (DB-authoritative timestamps)."""
        query = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING *").format(
            table=sql.Identifier(self._table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in self._insert_columns),
            vals=sql.SQL(", ").join(sql.Placeholder(c) for c in self._insert_columns),
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, self._insert_params(obj))
            row = await cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields one row.
        return self._model.model_validate(row)

    async def get(self, conn: AsyncConnection, obj_id: UUID) -> M | None:
        """Return the object with ``obj_id``, or ``None`` if absent."""
        query = sql.SQL("SELECT * FROM {} WHERE id = %s").format(sql.Identifier(self._table))
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (obj_id,))
            row = await cur.fetchone()
        return None if row is None else self._model.model_validate(row)


class StatefulRepository[M: DomainModel, S: StrEnum](Repository[M]):
    """A `Repository` plus `update_state`, bound to the object's state enum ``S``."""

    def __init__(
        self,
        model: type[M],
        table: str,
        state_enum: type[S],
        *,
        state_column: str = "state",
        json_columns: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(model, table, json_columns=json_columns)
        self._state_enum = state_enum
        self._state_column = state_column

    async def update_state(self, conn: AsyncConnection, obj_id: UUID, new_state: S) -> M:
        """Transition ``obj_id`` to ``new_state`` if `can_transition` permits it.

        Reads the current state under `FOR UPDATE` and writes in one transaction, so
        concurrent updaters are serialized.

        Raises:
            ObjectNotFound: No row has ``obj_id``.
            IllegalTransition: The current → ``new_state`` edge is not permitted.
        """
        col = self._state_column
        table = sql.Identifier(self._table)
        col_id = sql.Identifier(col)
        select_q = sql.SQL("SELECT {col} FROM {table} WHERE id = %s FOR UPDATE").format(
            col=col_id, table=table
        )
        update_q = sql.SQL("UPDATE {table} SET {col} = %s WHERE id = %s RETURNING *").format(
            table=table, col=col_id
        )
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(select_q, (obj_id,))
            row = await cur.fetchone()
            if row is None:
                raise ObjectNotFound(f"{self._table} id {obj_id} does not exist")
            ensure_transition(self._state_enum(row[col]), new_state)
            await cur.execute(update_q, (new_state, obj_id))
            updated = await cur.fetchone()
        assert updated is not None  # The row existed under FOR UPDATE.
        return self._model.model_validate(updated)


RESOURCES = StatefulRepository(
    Resource,
    "resources",
    ResourceStatus,
    state_column="status",
    json_columns=frozenset({"capabilities"}),
)
ALLOCATIONS = StatefulRepository(
    Allocation, "allocations", AllocationState, json_columns=frozenset({"capability_scope"})
)
SYSTEMS = StatefulRepository(
    System, "systems", SystemState, json_columns=frozenset({"provisioning_profile"})
)
INVESTIGATIONS = StatefulRepository(
    Investigation, "investigations", InvestigationState, json_columns=frozenset({"external_refs"})
)
RUNS = StatefulRepository(Run, "runs", RunState, json_columns=frozenset({"build_profile"}))
DEBUG_SESSIONS = StatefulRepository(DebugSession, "debug_sessions", DebugSessionState)
JOBS = StatefulRepository(Job, "jobs", JobState, json_columns=frozenset({"payload", "authorizing"}))
ARTIFACTS = Repository(Artifact, "artifacts")
