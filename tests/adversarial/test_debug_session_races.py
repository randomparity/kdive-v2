"""Adversarial: the debug single-attach lifecycle under genuine concurrency.

`debug.start_session` opens the gdbstub transport **outside** the per-System advisory
lock (a multi-second RSP probe), then re-checks single-attach + System-ready
**under** the lock before inserting the `live` row; a lost race closes the
just-opened transport (debug.py, ADR-0032 §6a). The invariants under attack:

  * **single-attach** — no matter how many `start_session` calls race the same System,
    at most one `attach`/`live` `debug_sessions` row survives; every loser gets
    `transport_conflict`;
  * **no transport leak** — exactly one transport stays open (the winner's); every
    loser's just-opened transport is closed;
  * **idempotent end** — two concurrent `end_session` of one session both succeed,
    write exactly one `->detached` audit row, and close the transport exactly once.

The existing suite covers the conflict with a *pre-seeded* live session on one
connection; this races the two handlers on separate pooled connections.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import DEBUG_SESSIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.models import DebugSession, Investigation, Run, System
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.providers.interfaces import SystemHandle, TransportHandle
from kdive.providers.ports import TransportHandleData
from kdive.security.rbac import Role
from tests.adversarial.conftest import seed_allocation, seed_resource

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {
                "kind": "path",
                "path": "oci://registry.internal/rootfs/fedora-40@sha256:abc",
            },
            "crashkernel": "256M",
        }
    },
}


class _TrackingConnector:
    """Hands out a unique transport per open and tracks the live set + close calls.

    A non-empty ``live`` after a settled race counts leaked transports; ``closed``
    counts how many detaches actually closed a transport.
    """

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.closed: list[str] = []
        self._n = 0

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        self._n += 1
        handle = TransportHandle(
            TransportHandleData(kind="gdbstub", host="127.0.0.1", port=1234 + self._n).encode()
        )
        self.live.add(str(handle))
        return handle

    def close_transport(self, handle: TransportHandle) -> None:
        self.closed.append(str(handle))
        self.live.discard(str(handle))


def _ctx() -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=6, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system_ready(pool: AsyncConnectionPool) -> str:
    async with pool.connection() as conn:
        resource = await seed_resource(conn, cap=4)
        allocation = await seed_allocation(conn, resource.id, AllocationState.ACTIVE)
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=SystemState.READY,
                provisioning_profile=copy.deepcopy(_PROFILE),
                domain_name="kdive-x",
            ),
        )
    return str(system.id)


async def _seed_booted_run(pool: AsyncConnectionPool, system_id: str) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                title="t",
                state=InvestigationState.ACTIVE,
            ),
        )
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv.id,
                system_id=UUID(system_id),
                state=RunState.SUCCEEDED,
                build_profile={},
            ),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'boot', 'succeeded', %s)",
            (run.id, Jsonb({})),
        )
    return str(run.id)


async def _seed_live_session(pool: AsyncConnectionPool, run_id: str) -> str:
    async with pool.connection() as conn:
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                run_id=UUID(run_id),
                state=DebugSessionState.LIVE,
                transport="gdbstub",
                transport_handle=TransportHandleData(
                    kind="gdbstub", host="127.0.0.1", port=1234
                ).encode(),
                worker_heartbeat_at=_DT,
            ),
        )
    return str(session.id)


async def _live_session_count(pool: AsyncConnectionPool, system_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM debug_sessions s JOIN runs r ON r.id = s.run_id "
            "WHERE r.system_id = %s AND s.state IN ('attach', 'live')",
            (UUID(system_id),),
        )
        row = await cur.fetchone()
    return 0 if row is None else int(row["n"])


def test_concurrent_start_session_keeps_single_attach_and_leaks_no_transport(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for _ in range(12):
                system_id = await _seed_system_ready(pool)
                # Two Runs on the SAME System share the one gdbstub endpoint.
                run_a = await _seed_booted_run(pool, system_id)
                run_b = await _seed_booted_run(pool, system_id)
                conn = _TrackingConnector()

                async def start(run_id: str, conn: _TrackingConnector = conn) -> Any:
                    return await debug_tools.start_session(
                        pool, _ctx(), run_id=run_id, transport="gdbstub", connector=conn
                    )

                results = await asyncio.gather(start(run_a), start(run_b))
                statuses = sorted(r.status for r in results)
                assert statuses == ["error", "live"], f"got {statuses}"
                conflict = next(r for r in results if r.status == "error")
                assert conflict.error_category == "transport_conflict"
                assert await _live_session_count(pool, system_id) == 1
                assert len(conn.live) == 1, f"leaked transport(s): {conn.live}"

    asyncio.run(_run())


def test_concurrent_end_session_is_idempotent_and_closes_once(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for _ in range(10):
                system_id = await _seed_system_ready(pool)
                run_id = await _seed_booted_run(pool, system_id)
                session_id = await _seed_live_session(pool, run_id)
                conn = _TrackingConnector()

                async def end(sid: str = session_id, conn: _TrackingConnector = conn) -> Any:
                    return await debug_tools.end_session(pool, _ctx(), sid, connector=conn)

                results = await asyncio.gather(end(), end())
                assert all(r.status == "detached" for r in results)
                assert len(conn.closed) == 1, f"transport closed {len(conn.closed)}x, want 1"
                async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT state FROM debug_sessions WHERE id = %s", (UUID(session_id),)
                    )
                    state_row = await cur.fetchone()
                    await cur.execute(
                        "SELECT count(*) AS n FROM audit_log WHERE object_id = %s "
                        "AND transition LIKE '%%->detached'",
                        (UUID(session_id),),
                    )
                    detach_row = await cur.fetchone()
                assert state_row is not None and detach_row is not None
                state = state_row["state"]
                detaches = detach_row["n"]
                assert state == "detached"
                assert detaches == 1, f"{detaches} detach audit rows, want exactly 1"

    asyncio.run(_run())
