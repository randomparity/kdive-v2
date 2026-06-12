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
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.ports import SystemHandle, TransportHandle, TransportHandleData
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProfilePolicy
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.resources.discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROFILE_POLICY = LocalLibvirtProfilePolicy()
_FAULT_POLICY = FaultInjectProfilePolicy()
_REMOTE_POLICY = RemoteLibvirtProfilePolicy()

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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
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
        port = 22 if kind == "drgn-live" else 1234
        return TransportHandle(TransportHandleData(kind=kind, host="127.0.0.1", port=port).encode())

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


def _handlers(
    connector: _FakeConnector,
    *,
    runtime: Any | None = None,
    secret_backend: Any | None = None,
    secret_backend_factory: Any | None = None,
    secret_registry: SecretRegistry | None = None,
    profile_policy: ProfilePolicy = _PROFILE_POLICY,
) -> debug_tools.DebugSessionHandlers:
    if secret_backend is not None:

        def _backend_factory(_: UUID) -> Any:
            return secret_backend

        secret_backend_factory = _backend_factory
    registry = secret_registry if secret_registry is not None else SecretRegistry()
    return debug_tools.DebugSessionHandlers.from_fixed_connector(
        connector,
        profile_policy=profile_policy,
        runtime=runtime,
        secret_backend_factory=secret_backend_factory,
        secret_registry=registry,
    )


async def _start_session(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    transport: str = "gdbstub",
    connector: _FakeConnector,
    secret_backend: Any | None = None,
    secret_backend_factory: Any | None = None,
    secret_registry: SecretRegistry | None = None,
    profile_policy: ProfilePolicy = _PROFILE_POLICY,
):
    return await _handlers(
        connector,
        secret_backend=secret_backend,
        secret_backend_factory=secret_backend_factory,
        secret_registry=secret_registry,
        profile_policy=profile_policy,
    ).start_session(pool, ctx, run_id=run_id, transport=transport)


async def _end_session(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    *,
    connector: _FakeConnector,
    runtime: Any | None = None,
    secret_registry: SecretRegistry | None = None,
):
    return await _handlers(connector, runtime=runtime, secret_registry=secret_registry).end_session(
        pool, ctx, session_id
    )


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
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
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
    boot_result: dict[str, Any] | None = None,
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
                (run.id, Jsonb({} if boot_result is None else boot_result)),
            )
    return str(run.id)


async def _seed_session(
    pool: AsyncConnectionPool,
    run_id: str,
    state: DebugSessionState,
    *,
    transport: str = "gdbstub",
) -> str:
    port = 22 if transport == "drgn-live" else 1234
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
                transport=transport,
                transport_handle=TransportHandleData(
                    kind=transport, host="127.0.0.1", port=port
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
            resp = await _start_session(
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
            resp = await _start_session(
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
                conn_fake = _RaisingCloseConnector()
                handle = conn_fake.open_transport(SystemHandle("kdive-x"), "gdbstub")
                resp = await debug_tools._insert_session_locked(
                    conn, _ctx(), run, system, handle, conn_fake, "gdbstub", uuid4()
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
                conn_fake = _RaisingCloseConnector()
                handle = conn_fake.open_transport(SystemHandle("kdive-x"), "gdbstub")
                resp = await debug_tools._insert_session_locked(
                    conn, _ctx(), run, system, handle, conn_fake, "gdbstub", uuid4()
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
            resp = await _start_session(
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
            resp = await _start_session(
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
            resp = await _start_session(
                pool, _ctx(), run_id=run_id, transport="gdbstub", connector=_FakeConnector()
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "defined"
        assert count == 0

    asyncio.run(_run())


def test_start_session_rejects_expected_crash_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(
                pool,
                sys_id,
                boot_result={"boot_outcome": "expected_crash_observed"},
            )
            conn_fake = _FakeConnector()
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="gdbstub",
                connector=conn_fake,
            )
            count = await _session_count(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "expected_crash_not_live_debuggable"
        assert count == 0
        assert conn_fake.opened == []

    asyncio.run(_run())


def test_start_session_bad_transport_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            conn_fake = _FakeConnector()
            resp = await _start_session(
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
            conn_fake = _FakeConnector(
                raises=CategorizedError(
                    "x",
                    category=category,
                    details={"provider": "local-libvirt", "retryable": False},
                )
            )
            resp = await _start_session(
                pool, _ctx(), run_id=run_id, transport="gdbstub", connector=conn_fake
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == expected
        assert resp.data == {"provider": "local-libvirt", "retryable": False}
        assert count == 0

    asyncio.run(_run())


def test_start_session_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            resp = await _start_session(
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
            resp = await _start_session(
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
                await _start_session(
                    pool,
                    _ctx(Role.VIEWER),
                    run_id=run_id,
                    transport="gdbstub",
                    connector=_FakeConnector(),
                )

    asyncio.run(_run())


# --- debug.start_session(transport="drgn-live") (ADR-0039) ---------------------------------------


class _OrderRecordingBackend:
    """A fake SecretBackend that records resolution order against a shared log.

    Registers into an injected ``SecretRegistry`` (a test-local one, never the process
    global) before returning — mirroring ``FileRefBackend``'s structural post-condition —
    so the ordering test can assert the registry was seeded before the connector ran.
    """

    def __init__(
        self,
        log: list[str],
        *,
        value: str = "guest-ssh-secret",
        registry: SecretRegistry | None = None,
        scope: object | None = None,
    ) -> None:
        self._log = log
        self._value = value
        self._registry = registry
        self._scope = scope
        self.refs: list[str] = []

    def resolve(self, ref: str) -> str:
        self.refs.append(ref)
        self._log.append(f"resolve:{ref}")
        if self._registry is not None:
            self._registry.register(self._value, scope=self._scope)
        return self._value


class _OrderRecordingConnector(_FakeConnector):
    """A connector that appends to a shared log when open_transport is invoked."""

    def __init__(self, log: list[str], *, raises: CategorizedError | None = None) -> None:
        super().__init__(raises=raises)
        self._log = log

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        self._log.append(f"open:{kind}")
        return super().open_transport(system, kind)


class _FailingConnectorResolver(ProviderResolver):
    """A ProviderResolver test double that fails before returning a connector."""

    def __init__(self) -> None:
        pass

    async def runtime_for_run(self, conn: Any, run_id: UUID) -> Any:
        del conn
        raise CategorizedError(
            "no test runtime",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )


class _ConnectionRecordingContext:
    def __init__(self, context: Any, log: list[str]) -> None:
        self._context = context
        self._log = log

    async def __aenter__(self) -> Any:
        self._log.append("connection:enter")
        return await self._context.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        self._log.append("connection:exit")
        return await self._context.__aexit__(exc_type, exc, traceback)


class _ConnectionRecordingPool:
    def __init__(self, pool: AsyncConnectionPool, log: list[str]) -> None:
        self._pool = pool
        self._log = log

    def connection(self) -> _ConnectionRecordingContext:
        return _ConnectionRecordingContext(self._pool.connection(), self._log)


def _ssh_profile() -> dict[str, Any]:
    profile = copy.deepcopy(_PROFILE)
    profile["provider"]["local-libvirt"]["ssh_credential_ref"] = "ssh/guest-key"
    return profile


def _fault_inject_profile() -> dict[str, Any]:
    profile = copy.deepcopy(_PROFILE)
    profile["provider"] = {"fault-inject": {}}
    return profile


async def _seed_ssh_system(pool: AsyncConnectionPool, alloc_id: str) -> str:
    return await _seed_profiled_system(pool, alloc_id, _ssh_profile())


async def _seed_profiled_system(
    pool: AsyncConnectionPool, alloc_id: str, profile: dict[str, Any]
) -> str:
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
                state=SystemState.READY,
                provisioning_profile=profile,
                domain_name="kdive-x",
            ),
        )
    return str(system.id)


def test_start_session_drgn_live_attaches_and_row_records_transport(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            log: list[str] = []
            connector = _OrderRecordingConnector(log)
            backend = _OrderRecordingBackend(log)
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                secret_backend=backend,
            )
            assert resp.status == "live"
            assert connector.opened == [("kdive-x", "drgn-live")]
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT transport, transport_handle FROM debug_sessions WHERE id = %s",
                    (resp.object_id,),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["transport"] == "drgn-live"
        # Core stores whatever the connector returned (opaque); this fake echoes the kind. The
        # real local connector's ssh:// realization scheme is asserted in the provider test, and
        # the remote bare-domain handle in test_start_session_drgn_live_remote_skips_credential.
        assert row["transport_handle"].startswith("drgn-live://")

    asyncio.run(_run())


def test_start_session_ssh_resolves_credential_before_opening_transport(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            log: list[str] = []
            registry = SecretRegistry()
            connector = _OrderRecordingConnector(log)
            backend = _OrderRecordingBackend(log, registry=registry)
            await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                secret_backend=backend,
            )
            # Ordering acceptance: resolve (which registers) precedes the open.
            assert log == ["resolve:ssh/guest-key", "open:drgn-live"]
            assert backend.refs == ["ssh/guest-key"]
            # The credential is in the registry before the transport was used.
            assert "guest-ssh-secret" in registry.snapshot()

    asyncio.run(_run())


def test_start_session_ssh_resolves_connector_before_credential(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            log: list[str] = []
            registry = SecretRegistry()
            backend = _OrderRecordingBackend(log, registry=registry)
            handlers = debug_tools.DebugSessionHandlers.from_resolver(
                _FailingConnectorResolver(),
                runtime_resolver=None,
                secret_backend_factory=lambda _session_id: backend,
                secret_registry=registry,
            )

            resp = await handlers.start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
            )
            count = await _session_count(pool)

        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert count == 0
        assert backend.refs == []
        assert log == []
        assert "guest-ssh-secret" not in registry.snapshot()

    asyncio.run(_run())


def test_start_session_opens_transport_between_db_connections(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            log: list[str] = []
            recording_pool: Any = _ConnectionRecordingPool(pool, log)
            connector = _OrderRecordingConnector(log)

            resp = await _handlers(connector).start_session(
                recording_pool,
                _ctx(),
                run_id=run_id,
                transport="gdbstub",
            )

        assert resp.status == "live"
        assert log == [
            "connection:enter",
            "connection:exit",
            "open:gdbstub",
            "connection:enter",
            "connection:exit",
        ]

    asyncio.run(_run())


def test_start_session_ssh_missing_credential_ref_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)  # no ssh_credential_ref
            run_id = await _seed_run(pool, sys_id)
            connector = _FakeConnector()
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                secret_backend=_OrderRecordingBackend([]),
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert count == 0
        assert connector.opened == []  # no transport opened without a credential

    asyncio.run(_run())


def test_start_session_drgn_live_fault_inject_skips_credential(migrated_url: str) -> None:
    # A fault-inject profile has no local-libvirt section, so drgn_live_requires_credential is
    # False: the drgn-live session opens with no credential (#215/ADR-0085), like remote.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_profiled_system(pool, alloc_id, _fault_inject_profile())
            run_id = await _seed_run(pool, sys_id)
            connector = _FakeConnector()
            backend = _OrderRecordingBackend([])
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                secret_backend=backend,
                profile_policy=_FAULT_POLICY,
            )
        assert resp.status == "live"
        assert connector.opened == [("kdive-x", "drgn-live")]
        assert backend.refs == []  # no credential resolved for a non-local-section profile

    asyncio.run(_run())


def test_start_session_ssh_invalid_profile_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            profile = _ssh_profile()
            profile["provider"] = {"local_libvirt": profile["provider"]["local-libvirt"]}
            sys_id = await _seed_profiled_system(pool, alloc_id, profile)
            run_id = await _seed_run(pool, sys_id)
            connector = _FakeConnector()
            backend = _OrderRecordingBackend([])
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                secret_backend=backend,
            )
            count = await _session_count(pool)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert count == 0
        assert backend.refs == []
        assert connector.opened == []

    asyncio.run(_run())


class _RaisingBackend:
    """A SecretBackend whose resolve raises a planted error (degraded secret store)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def resolve(self, ref: str) -> str:
        del ref
        raise self._exc


def test_start_session_ssh_path_escape_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            connector = _FakeConnector()
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                secret_backend=_RaisingBackend(PathSafetyError("escapes root")),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert connector.opened == []  # credential never resolved → no transport opened

    asyncio.run(_run())


def test_start_session_ssh_backend_dependency_failure_preserves_category(
    migrated_url: str,
) -> None:
    # A degraded secret store (e.g. a manager backend) must not be mislabeled as bad input.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            err = CategorizedError("vault down", category=ErrorCategory.MISSING_DEPENDENCY)
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=_FakeConnector(),
                secret_backend=_RaisingBackend(err),
            )
        assert resp.status == "error" and resp.error_category == "missing_dependency"

    asyncio.run(_run())


def test_ssh_credential_masks_after_session_ends(migrated_url: str) -> None:
    # ADR-0039 §2: the guest credential is registered before transport use, scoped to the
    # DebugSession, and released when the session detaches.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_id = await _seed_run(pool, sys_id)
            registry = SecretRegistry()

            def _backend_for(session_id: UUID) -> _OrderRecordingBackend:
                return _OrderRecordingBackend(
                    [],
                    registry=registry,
                    scope=f"debug-session:{session_id}",
                )

            start = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=_FakeConnector(),
                secret_backend_factory=_backend_for,
            )
            assert start.status == "live"
            assert "guest-ssh-secret" in registry.snapshot()
            await _end_session(
                pool,
                _ctx(),
                start.object_id,
                connector=_FakeConnector(),
                secret_registry=registry,
            )
            assert "guest-ssh-secret" not in registry.snapshot()

    asyncio.run(_run())


def test_second_ssh_attach_is_transport_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_a = await _seed_run(pool, sys_id)
            run_b = await _seed_run(pool, sys_id)
            await _seed_session(pool, run_a, DebugSessionState.LIVE, transport="drgn-live")
            connector = _FakeConnector()
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_b,
                transport="drgn-live",
                connector=connector,
                secret_backend=_OrderRecordingBackend([]),
            )
        assert resp.status == "error" and resp.error_category == "transport_conflict"
        assert connector.opened == []

    asyncio.run(_run())


def test_gdbstub_and_ssh_sessions_coexist_on_one_system(migrated_url: str) -> None:
    # Per-transport scoping (ADR-0039 §4): an existing gdbstub session must not block ssh.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_ssh_system(pool, alloc_id)
            run_a = await _seed_run(pool, sys_id)
            run_b = await _seed_run(pool, sys_id)
            await _seed_session(pool, run_a, DebugSessionState.LIVE, transport="gdbstub")
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_b,
                transport="drgn-live",
                connector=_FakeConnector(),
                secret_backend=_OrderRecordingBackend([]),
            )
        assert resp.status == "live"  # the gdbstub session does not conflict with ssh

    asyncio.run(_run())


def _remote_profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 4,
        "memory_mb": 4096,
        "disk_gb": 20,
        "boot_method": "disk-image",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
        "provider": {"remote-libvirt": {"base_image_volume": "base-fedora40"}},
    }


class _DomainHandleConnector(_FakeConnector):
    """Mimics the remote connector: drgn-live returns the bare SystemHandle (domain name)."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        self.opened.append((str(system), kind))
        if kind == "drgn-live":
            return TransportHandle(str(system))
        return super().open_transport(system, kind)


def test_start_session_drgn_live_remote_skips_credential_and_stores_domain_handle(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_profiled_system(pool, alloc_id, _remote_profile())
            run_id = await _seed_run(pool, sys_id)
            connector = _DomainHandleConnector()
            # No secret_backend supplied: a remote drgn-live start must not need one.
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                profile_policy=_REMOTE_POLICY,
            )
            assert resp.status == "live"
            assert connector.opened == [("kdive-x", "drgn-live")]
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT transport, transport_handle FROM debug_sessions WHERE id = %s",
                    (resp.object_id,),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["transport"] == "drgn-live"
        assert row["transport_handle"] == "kdive-x"  # bare domain, ADR-0083 §4

    asyncio.run(_run())


def test_start_session_drgn_live_local_missing_ref_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)  # local, no ssh ref
            run_id = await _seed_run(pool, sys_id)
            connector = _FakeConnector()
            resp = await _start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="drgn-live",
                connector=connector,
                secret_backend=_OrderRecordingBackend([]),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data == {"reason": "ssh_credential_ref_missing"}
        assert connector.opened == []

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
            resp = await _end_session(pool, _ctx(), session_id, connector=conn_fake)
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
            resp = await _end_session(pool, _ctx(), session_id, connector=_FakeConnector())
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
            resp = await _end_session(pool, _ctx(), session_id, connector=_FakeConnector())
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
            resp = await _end_session(pool, _ctx(), session_id, connector=_RaisingCloseConnector())
            assert resp.status == "detached"
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM debug_sessions WHERE id = %s", (session_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "detached"

    asyncio.run(_run())


def test_end_session_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _end_session(pool, _ctx(), "not-a-uuid", connector=_FakeConnector())
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_end_session_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, sys_id)
            session_id = await _seed_session(pool, run_id, DebugSessionState.LIVE)
            resp = await _end_session(
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
                await _end_session(pool, _ctx(Role.VIEWER), session_id, connector=_FakeConnector())

    asyncio.run(_run())
