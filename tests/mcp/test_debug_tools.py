"""debug.* tool tests — handlers called directly with an injected pool + fake Connector.

A `ready` System + a `succeeded` Run with a succeeded `boot` step is the attachable state;
the Connect provider is faked, so no socket/libvirt host is needed. The single-attach rule
is enforced per System (joined through `runs`), and `force_crash`-detach stays the control
plane's (#23) — these tests cover only the agent-initiated start/end paths.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, DEBUG_SESSIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import (
    Allocation,
    DebugSession,
    Investigation,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import debug as debug_tools
from kdive.providers.interfaces import SystemHandle, TransportHandle
from kdive.providers.local_libvirt.connect import TransportHandleData
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from kdive.security.rbac import AuthorizationError, Role
from tests.providers.local_libvirt.conftest import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc",
            "crashkernel": "256M",
        }
    },
}


class _FakeConnector:
    """Records open/close calls; returns a canned handle or raises a canned error."""

    def __init__(self, *, raises: CategorizedError | None = None) -> None:
        self._raises = raises
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        self.opened.append((str(system), kind))
        if self._raises is not None:
            raise self._raises
        return TransportHandle(
            TransportHandleData(kind="gdbstub", host="127.0.0.1", port=1234).encode()
        )

    def close_transport(self, handle: TransportHandle) -> None:
        self.closed.append(str(handle))


class _RaisingCloseConnector(_FakeConnector):
    """A connector whose close_transport raises — the detach must still complete."""

    def close_transport(self, handle: TransportHandle) -> None:
        super().close_transport(handle)
        raise CategorizedError("close blew up", category=ErrorCategory.TRANSPORT_FAILURE)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _granted_allocation(pool: AsyncConnectionPool) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=2
    )
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, disc, pool="local-libvirt", cost_class="local"
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
    return str(alloc.id)


async def _seed_system(pool: AsyncConnectionPool, alloc_id: str, state: SystemState) -> str:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=copy.deepcopy(_PROFILE),
                domain_name="kdive-x",
            ),
        )
    return str(system.id)


async def _seed_run(
    pool: AsyncConnectionPool,
    sys_id: str,
    *,
    state: RunState = RunState.SUCCEEDED,
    booted: bool = True,
) -> str:
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
                system_id=UUID(sys_id),
                state=state,
                build_profile={},
            ),
        )
        if booted:
            await conn.execute(
                "INSERT INTO run_steps (run_id, step, state, result) "
                "VALUES (%s, 'boot', 'succeeded', %s)",
                (run.id, Jsonb({})),
            )
    return str(run.id)


async def _seed_session(pool: AsyncConnectionPool, run_id: str, state: DebugSessionState) -> str:
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
                state=state,
                transport="gdbstub",
                transport_handle=TransportHandleData(
                    kind="gdbstub", host="127.0.0.1", port=1234
                ).encode(),
            ),
        )
    return str(session.id)


async def _session_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT count(*) AS n FROM debug_sessions")
        row = await cur.fetchone()
    return 0 if row is None else int(row["n"])


# --- debug.start_session -------------------------------------------------------------------


def test_start_session_attaches_and_row_is_live(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            conn_fake = _FakeConnector()
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id=run_id, transport="gdbstub", connector=conn_fake
            )
            assert resp.status == "live"
            assert conn_fake.opened == [("kdive-x", "gdbstub")]
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, transport_handle, worker_heartbeat_at "
                    "FROM debug_sessions WHERE id = %s",
                    (resp.object_id,),
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_kind = 'debug_sessions' AND transition IN "
                    "('->attach', 'attach->live')"
                )
                audit = await cur.fetchone()
        assert row is not None
        assert row["state"] == "live"
        assert row["transport_handle"] is not None
        assert row["worker_heartbeat_at"] is not None
        assert audit is not None and audit["n"] == 2

    asyncio.run(_run())


def test_second_start_session_is_transport_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_a = await _seed_run(pool, sys_id)
            run_b = await _seed_run(pool, sys_id)
            await _seed_session(pool, run_a, DebugSessionState.LIVE)
            before = await _session_count(pool)
            conn_fake = _FakeConnector()
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id=run_b, transport="gdbstub", connector=conn_fake
            )
            after = await _session_count(pool)
        assert resp.status == "error"
        assert resp.error_category == "transport_conflict"
        assert after == before  # no new row
        # The pre-lock read catches the conflict before any transport is opened (fast-fail),
        # so the connector is never invoked at all.
        assert conn_fake.opened == []
        assert conn_fake.closed == []

    asyncio.run(_run())


def test_locked_recheck_closes_transport_when_system_crashed(migrated_url: str) -> None:
    """The lost-race branch: System flipped non-ready between the pre-read and the lock."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                system = await SYSTEMS.get(conn, UUID(sys_id))
                assert run is not None and system is not None
                # Drive the real row to `crashed` so the locked re-read observes the race,
                # while the `system` object handed to the locked insert is still stale-ready.
                await SYSTEMS.update_state(conn, system.id, SystemState.CRASHED)
                conn_fake = _FakeConnector()
                handle = conn_fake.open_transport(SystemHandle("kdive-x"), "gdbstub")
                resp = await debug_tools._insert_session_locked(
                    conn, _ctx(), run, system, handle, conn_fake
                )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "crashed"
        assert count == 0
        assert conn_fake.closed  # the orphaned transport was closed

    asyncio.run(_run())


def test_locked_recheck_closes_transport_when_conflict_appears(migrated_url: str) -> None:
    """The lost-race branch: another attach committed between the pre-read and the lock."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_a = await _seed_run(pool, sys_id)
            run_b = await _seed_run(pool, sys_id)
            await _seed_session(pool, run_a, DebugSessionState.LIVE)  # the race winner
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_b))
                system = await SYSTEMS.get(conn, UUID(sys_id))
                assert run is not None and system is not None
                conn_fake = _FakeConnector()
                handle = conn_fake.open_transport(SystemHandle("kdive-x"), "gdbstub")
                resp = await debug_tools._insert_session_locked(
                    conn, _ctx(), run, system, handle, conn_fake
                )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "transport_conflict"
        assert count == 1  # only the race winner's row
        assert conn_fake.closed

    asyncio.run(_run())


def test_start_session_run_not_succeeded_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id, state=RunState.CREATED, booted=False)
            conn_fake = _FakeConnector()
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id=run_id, transport="gdbstub", connector=conn_fake
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "created"
        assert count == 0
        assert conn_fake.opened == []  # connector not invoked

    asyncio.run(_run())


def test_start_session_unbooted_run_is_boot_first(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id, booted=False)
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id=run_id, transport="gdbstub", connector=_FakeConnector()
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "boot_first"
        assert count == 0

    asyncio.run(_run())


def test_start_session_non_ready_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.DEFINED)
            run_id = await _seed_run(pool, sys_id)
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id=run_id, transport="gdbstub", connector=_FakeConnector()
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "defined"
        assert count == 0

    asyncio.run(_run())


def test_start_session_bad_transport_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            conn_fake = _FakeConnector()
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id=run_id, transport="serial", connector=conn_fake
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert count == 0
        assert conn_fake.opened == []

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ErrorCategory.DEBUG_ATTACH_FAILURE, "debug_attach_failure"),
        (ErrorCategory.TRANSPORT_FAILURE, "transport_failure"),
        (ErrorCategory.MISSING_DEPENDENCY, "debug_attach_failure"),
    ],
)
def test_start_session_connector_failure_maps_category(
    migrated_url: str, category: ErrorCategory, expected: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            conn_fake = _FakeConnector(raises=CategorizedError("x", category=category))
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id=run_id, transport="gdbstub", connector=conn_fake
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == expected
        assert count == 0

    asyncio.run(_run())


def test_start_session_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            resp = await debug_tools.start_session(
                pool,
                _ctx(projects=("other",)),
                run_id=run_id,
                transport="gdbstub",
                connector=_FakeConnector(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_start_session_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await debug_tools.start_session(
                pool, _ctx(), run_id="not-a-uuid", transport="gdbstub", connector=_FakeConnector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_start_session_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            with pytest.raises(AuthorizationError):
                await debug_tools.start_session(
                    pool,
                    _ctx(Role.VIEWER),
                    run_id=run_id,
                    transport="gdbstub",
                    connector=_FakeConnector(),
                )

    asyncio.run(_run())


# --- debug.end_session ---------------------------------------------------------------------


def test_end_session_detaches_live(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.LIVE)
            conn_fake = _FakeConnector()
            resp = await debug_tools.end_session(pool, _ctx(), session_id, connector=conn_fake)
            assert resp.status == "detached"
            assert conn_fake.closed  # transport closed on detach
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM debug_sessions WHERE id = %s", (session_id,))
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_id = %s AND transition = 'live->detached'",
                    (session_id,),
                )
                audit = await cur.fetchone()
        assert row is not None and row["state"] == "detached"
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_end_session_already_detached_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.DETACHED)
            resp = await debug_tools.end_session(
                pool, _ctx(), session_id, connector=_FakeConnector()
            )
            assert resp.status == "detached"
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE object_id = %s", (session_id,)
                )
                audit = await cur.fetchone()
        assert audit is not None and audit["n"] == 0  # no second transition audited

    asyncio.run(_run())


def test_end_session_detaches_attach(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.ATTACH)
            resp = await debug_tools.end_session(
                pool, _ctx(), session_id, connector=_FakeConnector()
            )
            assert resp.status == "detached"
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_id = %s AND transition = 'attach->detached'",
                    (session_id,),
                )
                audit = await cur.fetchone()
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_end_session_close_failure_still_detaches(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.LIVE)
            resp = await debug_tools.end_session(
                pool, _ctx(), session_id, connector=_RaisingCloseConnector()
            )
            assert resp.status == "detached"
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM debug_sessions WHERE id = %s", (session_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "detached"

    asyncio.run(_run())


def test_end_session_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await debug_tools.end_session(
                pool, _ctx(), "not-a-uuid", connector=_FakeConnector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_end_session_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.LIVE)
            resp = await debug_tools.end_session(
                pool, _ctx(projects=("other",)), session_id, connector=_FakeConnector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_end_session_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.LIVE)
            with pytest.raises(AuthorizationError):
                await debug_tools.end_session(
                    pool, _ctx(Role.VIEWER), session_id, connector=_FakeConnector()
                )

    asyncio.run(_run())
