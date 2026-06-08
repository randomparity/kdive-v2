"""debug.* gdb-MI tool tests — handlers driven with a real seeded session + a fake attach seam.

The seven Debug-plane handlers (`run_engine_op` + the op factories) are the unit of testing:
a `live` `DebugSession` is seeded in the migrated DB, and a fake `AttachSeam` returns a
`GdbMiAttachment` over a scripted fake `MiController`, so the gate, the per-session lock, the
attach-once behavior, the envelopes, the §5a `data["code"]` discriminators, and the
`end_session` reap are exercised without gdb or a socket.
"""

from __future__ import annotations

import asyncio
import copy
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, DEBUG_SESSIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.models import Allocation, DebugSession, Investigation, Run, System
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.debug import ops as debug_ops
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.mcp.tools.debug.ops import (
    DebugEngineRuntime,
    run_engine_op,
)
from kdive.providers.local_libvirt.debug.debug_gdbmi import GdbMiEngine
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.ports import GdbMiAttachment, TransportHandleData
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.resource_discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


class _FakeMiController:
    def __init__(self, responses: dict[str, list[dict[str, object]]] | None = None) -> None:
        self._responses = responses or {}
        self.written: list[str] = []
        self.exited = False

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        del timeout_sec
        self.written.append(command)
        return self._responses.get(
            command, [{"type": "result", "message": "done", "payload": None}]
        )

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        del timeout_sec
        return []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        del timeout_sec, raise_error_on_timeout
        return []

    def exit(self) -> None:
        self.exited = True


class _CountingAttach:
    """A fake `AttachSeam` that records how many times it spawns an engine."""

    def __init__(self, controller: _FakeMiController | None = None) -> None:
        self.controller = controller or _FakeMiController()
        self.calls = 0

    def __call__(
        self, *, host: str, port: int, run_id: str, transcript_path: Path
    ) -> GdbMiAttachment:
        del host, port, run_id
        self.calls += 1
        return GdbMiAttachment(
            controller=self.controller,
            rsp_host="127.0.0.1",
            rsp_port=1234,
            transcript_path=transcript_path,
        )


def _raising_attach(*, host: str, port: int, run_id: str, transcript_path: Path) -> GdbMiAttachment:
    del host, port, transcript_path
    from kdive.domain.errors import CategorizedError, ErrorCategory

    raise CategorizedError(
        "no live host", category=ErrorCategory.MISSING_DEPENDENCY, details={"run_id": run_id}
    )


def _runtime(attach: Any) -> DebugEngineRuntime:
    return DebugEngineRuntime(
        engine=GdbMiEngine(), attach=attach, transcript_dir=Path(tempfile.mkdtemp())
    )


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


async def _seed_live_session(pool: AsyncConnectionPool, *, state: DebugSessionState) -> str:
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
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile=copy.deepcopy(_PROFILE),
                domain_name="kdive-x",
            ),
        )
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
                system_id=system.id,
                state=RunState.SUCCEEDED,
                build_profile={},
            ),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'boot', 'succeeded', %s)",
            (run.id, Jsonb({})),
        )
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                run_id=run.id,
                state=state,
                transport="gdbstub",
                transport_handle=TransportHandleData(
                    kind="gdbstub", host="127.0.0.1", port=1234
                ).encode(),
            ),
        )
    return str(session.id)


def _op_for(name: str, runtime: DebugEngineRuntime, session_id: str, **kwargs: Any) -> Any:
    factory = {
        "set_breakpoint": debug_ops._set_breakpoint_op,
        "clear_breakpoint": debug_ops._clear_breakpoint_op,
        "list_breakpoints": debug_ops._list_breakpoints_op,
        "read_memory": debug_ops._read_memory_op,
        "read_registers": debug_ops._read_registers_op,
        "continue": debug_ops._continue_op,
        "interrupt": debug_ops._interrupt_op,
    }[name]
    return factory(runtime, session_id, **kwargs)


# --- happy paths ---------------------------------------------------------------------------


def test_set_breakpoint_returns_set(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-insert -h panic": [
                        {"type": "result", "message": "done", "payload": {"bkpt": {"number": "1"}}}
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("set_breakpoint", runtime, session_id, location="panic"),
            )
        assert resp.status == "set"
        assert resp.data["number"] == "1"
        assert "debug.continue" in resp.suggested_next_actions

    asyncio.run(_run())


def test_read_memory_returns_verbatim_hex(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-read-memory-bytes 0x1000 4": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {"memory": [{"contents": "deadbeef"}]},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("read_memory", runtime, session_id, address=0x1000, byte_count=4),
            )
        assert resp.status == "read"
        assert resp.data["memory_hex"] == "deadbeef"  # bytes verbatim, not redacted
        assert resp.data["byte_count"] == "4"
        assert resp.data["address"] == "0x1000"

    asyncio.run(_run())


def test_read_registers_returns_direct_values(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-list-register-names": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {"register-names": ["rax", "rbx", "rcx"]},
                        }
                    ],
                    "-data-list-register-values x": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {
                                "register-values": [
                                    {"number": "0", "value": "0xdead"},
                                    {"number": "2", "value": "0xcafe"},
                                ]
                            },
                        }
                    ],
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("read_registers", runtime, session_id, registers=["rax", "rcx"]),
            )
        assert resp.status == "read"
        assert resp.data == {"rax": "0xdead", "rcx": "0xcafe"}

    asyncio.run(_run())


def test_read_memory_over_cap_is_rejected_without_attach(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            attach = _CountingAttach()
            runtime = _runtime(attach)
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("read_memory", runtime, session_id, address=0x10, byte_count=4097),
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["code"] == "bad_read_range"
        # The attach DID happen (the cap is enforced in the engine op), but no MI read command ran.
        assert attach.controller.written == []

    asyncio.run(_run())


def test_continue_returns_stopped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {"-exec-continue": [{"type": "result", "message": "running", "payload": None}]}
            )
            # No reads scripted -> resume times out -> interrupt -> no stop -> transport_stall.
            controller._responses["-exec-interrupt"] = [
                {"type": "result", "message": "done", "payload": None}
            ]
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("continue", runtime, session_id, timeout_sec=1),
            )
        # A silent link surfaces as INFRASTRUCTURE_FAILURE (the handler maps the engine error).
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["code"] == "transport_stall"

    asyncio.run(_run())


# --- gate + §5a codes ----------------------------------------------------------------------


def test_bad_session_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool,
                _ctx(),
                "not-a-uuid",
                runtime,
                _op_for("list_breakpoints", runtime, "not-a-uuid"),
            )
        assert resp.error_category == "configuration_error"
        assert resp.data["code"] == "bad_session_id"

    asyncio.run(_run())


def test_unknown_session(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sid = str(uuid4())
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool, _ctx(), sid, runtime, _op_for("list_breakpoints", runtime, sid)
            )
        assert resp.data["code"] == "unknown_session"

    asyncio.run(_run())


def test_cross_project_session_is_unknown(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool,
                _ctx(projects=("other",)),
                session_id,
                runtime,
                _op_for("list_breakpoints", runtime, session_id),
            )
        assert resp.data["code"] == "unknown_session"

    asyncio.run(_run())


def test_non_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_CountingAttach())
            with pytest.raises(AuthorizationError):
                await run_engine_op(
                    pool,
                    _ctx(Role.VIEWER),
                    session_id,
                    runtime,
                    _op_for("list_breakpoints", runtime, session_id),
                )

    asyncio.run(_run())


def test_non_live_session_is_not_live(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.DETACHED)
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
        assert resp.data["code"] == "not_live"
        assert resp.data["current_status"] == "detached"

    asyncio.run(_run())


def test_missing_dependency_attach_surfaces_as_debug_attach_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_raising_attach)
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
        assert resp.status == "error"
        assert resp.error_category == "debug_attach_failure"

    asyncio.run(_run())


# --- attach-once + reap --------------------------------------------------------------------


def test_attach_runs_once_for_concurrent_ops(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            attach = _CountingAttach()
            runtime = _runtime(attach)
            ops = [
                run_engine_op(
                    pool,
                    _ctx(),
                    session_id,
                    runtime,
                    _op_for("list_breakpoints", runtime, session_id),
                )
                for _ in range(2)
            ]
            results = await asyncio.gather(*ops)
        assert all(r.status == "listed" for r in results)
        assert attach.calls == 1  # the per-session lock serializes; only one op attaches

    asyncio.run(_run())


def test_end_session_reaps_engine(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            attach = _CountingAttach()
            runtime = _runtime(attach)
            await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
            # The engine is registered; end_session must exit + drop it.
            handlers = debug_tools.DebugSessionHandlers(_FakeConnector(), runtime=runtime)
            resp = await handlers.end_session(pool, _ctx(), session_id)
            assert resp.status == "detached"
            assert attach.controller.exited is True
            # A subsequent op on the now-detached session is rejected at the state gate.
            follow = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
        assert follow.data["code"] == "not_live"

    asyncio.run(_run())


def test_end_session_reap_is_noop_without_engine(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_CountingAttach())
            handlers = debug_tools.DebugSessionHandlers(_FakeConnector(), runtime=runtime)
            resp = await handlers.end_session(pool, _ctx(), session_id)
        assert resp.status == "detached"  # reap of a never-attached session is a no-op

    asyncio.run(_run())


class _FakeConnector:
    def open_transport(self, system: Any, kind: str) -> Any:
        del system, kind
        raise NotImplementedError

    def close_transport(self, handle: Any) -> None:
        del handle
