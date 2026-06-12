"""The Debug-plane gdb-MI tools — `debug.set_breakpoint/.clear/.list`, `.read_memory`,
`.read_registers`, `.continue`, `.interrupt` (ADR-0034).

These extend the `debug.*` session lifecycle tools registered by ``sessions.py``. A `live`
`DebugSession` records an open single-attach gdbstub transport; the first Debug-plane op for
a session lazily spawns a gdb/MI engine over the session's RSP endpoint, cached in a
process-scoped
:class:`DebugEngineRuntime` (registry + per-session ``asyncio.Lock`` table + the
``live_vm``-gated attach seam). Every op is gated (operator + project + ``live`` state), takes
the per-session lock, attaches-or-reuses, and runs the blocking engine call via
``asyncio.to_thread`` so a long `continue` never stalls the event loop.

Textual transcript/record output is redacted by the engine before persistence/response; raw
`read_memory` bytes are returned **verbatim** under the 4096 cap (rendered as hex in
``data["memory_hex"]``) — the cap is the memory control, redaction is the transcript-text
control, and they are independent (ADR-0034 §3/§6).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.config.core_settings import DEBUG_DIR
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DebugSession, ResourceKind
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.session_context import (
    resolve_debug_session_context,
)
from kdive.mcp.tools.debug.session_registry import GdbMiSessionRegistry
from kdive.providers.ports import (
    AttachSeam,
    GdbMiAttachment,
    GdbMiEngine,
    TransportHandleData,
)
from kdive.providers.resolver import ProviderBinding, ProviderResolver
from kdive.security.authz.context import RequestContext

_EngineOp = Callable[[GdbMiEngine, GdbMiAttachment], ToolResponse]


def _default_transcript_dir() -> Path:
    # Configurable (KDIVE_DEBUG_DIR) so a deployment points it at the run-artifact tree and
    # tests at a temp dir; the registry default mirrors the other planes' /var/lib/kdive/* roots.
    return Path(config.require(DEBUG_DIR))


class DebugEngineRuntime:
    """Process-scoped holder for the lazy gdb-MI engines + per-session locks (ADR-0034 §4a).

    Owns the in-process :class:`GdbMiSessionRegistry`, a per-session ``asyncio.Lock`` table (the
    get-or-create guarded by a plain :class:`threading.Lock`), and the injected
    :class:`AttachSeam`. One instance is built in ``debug.register`` and shared by every
    Debug-plane handler (and by `end_session`'s reap).
    """

    def __init__(
        self, *, engine: GdbMiEngine, attach: AttachSeam, transcript_dir: Path | None = None
    ) -> None:
        self._engine = engine
        self._attach = attach
        self._transcript_dir = (
            transcript_dir if transcript_dir is not None else _default_transcript_dir()
        )
        self._registry = GdbMiSessionRegistry()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    @property
    def engine(self) -> GdbMiEngine:
        return self._engine

    def lock_for(self, session_id: str) -> asyncio.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    def get_or_attach(self, session: DebugSession) -> GdbMiAttachment:
        """Return the live attachment for ``session``, attaching once on a registry miss."""
        session_id = str(session.id)
        existing = self._registry.get(session_id)
        if existing is not None:
            return existing
        endpoint = TransportHandleData.decode(session.transport_handle or "")
        attachment = self._attach(
            host=endpoint.host,
            port=endpoint.port,
            run_id=str(session.run_id),
            transcript_path=self._transcript_dir / f"{session_id}.jsonl",
        )
        self._registry.register(session_id, attachment)
        return attachment

    def reap(self, session_id: str) -> None:
        """Exit + drop the live engine for ``session_id`` (no-op if never attached)."""
        attachment = self._registry.reap(session_id)
        if attachment is not None:
            with contextlib.suppress(Exception):
                attachment.controller.exit()
        with self._locks_guard:
            self._locks.pop(session_id, None)


class DebugRuntimeResolver:
    """Provider-aware cache of per-provider debug engine runtimes."""

    def __init__(self, resolver: ProviderResolver, *, transcript_dir: Path | None = None) -> None:
        self._resolver = resolver
        self._transcript_dir = transcript_dir
        self._runtimes: dict[ResourceKind, DebugEngineRuntime] = {}
        self._guard = threading.Lock()

    async def runtime_for_session(
        self, pool: AsyncConnectionPool, session_id: UUID
    ) -> DebugEngineRuntime | ToolResponse:
        async with pool.connection() as conn:
            try:
                binding = await self._resolver.binding_for_session(conn, session_id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(str(session_id), exc)
        return self.runtime_for_binding(binding, object_id=str(session_id))

    def runtime_for_binding(
        self, binding: ProviderBinding, *, object_id: str | None = None
    ) -> DebugEngineRuntime | ToolResponse:
        debug = binding.runtime.debug
        if debug is None:
            return ToolResponse.failure(
                object_id or binding.kind.value,
                ErrorCategory.DEBUG_ATTACH_FAILURE,
                data={"reason": "provider_debug_unavailable"},
            )
        with self._guard:
            runtime = self._runtimes.get(binding.kind)
            if runtime is None:
                runtime = DebugEngineRuntime(
                    engine=debug.engine,
                    attach=debug.attach_seam,
                    transcript_dir=self._transcript_dir,
                )
                self._runtimes[binding.kind] = runtime
            return runtime


def _op_failure(session_id: str, exc: CategorizedError) -> ToolResponse:
    """Map an engine ``CategorizedError`` onto a failure envelope (with its ``code`` if any)."""
    category = exc.category
    if category is ErrorCategory.MISSING_DEPENDENCY:
        category = ErrorCategory.DEBUG_ATTACH_FAILURE
    return ToolResponse.failure_from_error(session_id, exc, category=category)


async def _live_session(
    pool: AsyncConnectionPool, ctx: RequestContext, session_id: str
) -> DebugSession | ToolResponse:
    """UUID-parse, load, project/role-gate, and require ``live`` state (ADR-0034 §5a codes)."""
    async with pool.connection() as conn:
        resolved = await resolve_debug_session_context(conn, ctx, session_id, require_live=True)
    if isinstance(resolved, ToolResponse):
        return resolved
    return resolved.session


async def run_engine_op(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    runtime: DebugEngineRuntime | DebugRuntimeResolver,
    op: _EngineOp,
) -> ToolResponse:
    """Gate the session, take the per-session lock, attach-or-reuse, and run ``op`` off-loop.

    The blocking engine work (attach + ``op``) is dispatched via ``asyncio.to_thread`` under the
    per-session ``asyncio.Lock`` so a long `continue` never stalls the event loop and only one
    op ever attaches/drives a given engine (ADR-0034 §4a/§4b).
    """
    with bind_context(principal=ctx.principal):
        gated = await _live_session(pool, ctx, session_id)
        if isinstance(gated, ToolResponse):
            return gated
        session = gated
        resolved_runtime = await _runtime_for_op(pool, session, runtime)
        if isinstance(resolved_runtime, ToolResponse):
            return resolved_runtime
        async with resolved_runtime.lock_for(session_id):
            try:
                return await asyncio.to_thread(_attach_and_run, resolved_runtime, session, op)
            except CategorizedError as exc:
                return _op_failure(session_id, exc)


async def _runtime_for_op(
    pool: AsyncConnectionPool,
    session: DebugSession,
    runtime: DebugEngineRuntime | DebugRuntimeResolver,
) -> DebugEngineRuntime | ToolResponse:
    if isinstance(runtime, DebugRuntimeResolver):
        return await runtime.runtime_for_session(pool, session.id)
    return runtime


def _attach_and_run(
    runtime: DebugEngineRuntime, session: DebugSession, op: _EngineOp
) -> ToolResponse:
    return op(runtime.engine, runtime.get_or_attach(session))


def _set_breakpoint_op(session_id: str, location: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        ref = engine.set_breakpoint(attachment, location)
        return ToolResponse.success(
            session_id,
            "set",
            suggested_next_actions=["debug.continue", "debug.list_breakpoints"],
            data={"number": ref.number},
        )

    return op


def _clear_breakpoint_op(session_id: str, number: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        engine.clear_breakpoint(attachment, number)
        return ToolResponse.success(
            session_id, "cleared", suggested_next_actions=["debug.list_breakpoints"]
        )

    return op


def _list_breakpoints_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        refs = engine.list_breakpoints(attachment)
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.set_breakpoint", "debug.continue"],
            data={"count": str(len(refs))},
        )

    return op


def _read_memory_op(session_id: str, address: int, byte_count: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        blob = engine.read_memory(attachment, address=address, byte_count=byte_count)
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_registers", "debug.continue"],
            data={
                "address": f"0x{address:x}",
                "byte_count": str(len(blob)),
                "memory_hex": blob.hex(),
            },
        )

    return op


def _read_registers_op(session_id: str, registers: list[str]) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        values = engine.read_registers(attachment, registers)
        rendered = {str(k): str(v) for k, v in values.items()}
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_memory", "debug.continue"],
            data=rendered,
        )

    return op


def _continue_op(session_id: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.continue_(attachment, timeout_sec=timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=[
                "debug.read_registers",
                "debug.read_memory",
                "debug.list_breakpoints",
            ],
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _interrupt_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.interrupt(attachment)
        reason = stop.reason if stop is not None else None
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=["debug.read_registers", "debug.continue"],
            data=_stop_data(reason, False),
        )

    return op


def _stop_data(reason: str | None, timed_out: bool) -> dict[str, str]:
    data = {"timed_out": "true" if timed_out else "false"}
    if reason is not None:
        data["reason"] = reason
    return data


def _register_debug_ops(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    """Register the seven gdb-MI `debug.*` tools on ``app``, sharing ``runtime`` (ADR-0034 §5)."""

    @app.tool(
        name="debug.set_breakpoint",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def debug_set_breakpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession to set a breakpoint on.")
        ],
        location: Annotated[str, Field(description="Bare C function or symbol name to break at.")],
    ) -> ToolResponse:
        """Set a breakpoint on a live DebugSession via gdb-MI. Requires operator."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _set_breakpoint_op(session_id, location),
        )

    @app.tool(
        name="debug.clear_breakpoint",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def debug_clear_breakpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose breakpoint to clear.")
        ],
        number: Annotated[
            str,
            Field(description="Breakpoint number to clear (from debug.list_breakpoints)."),
        ],
    ) -> ToolResponse:
        """Clear a breakpoint by number on a live DebugSession. Requires operator."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _clear_breakpoint_op(session_id, number),
        )

    @app.tool(
        name="debug.list_breakpoints",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def debug_list_breakpoints(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose breakpoints to list.")
        ],
    ) -> ToolResponse:
        """List all breakpoints on a live DebugSession. Requires operator."""
        return await run_engine_op(
            pool, current_context(), session_id, runtime, _list_breakpoints_op(session_id)
        )

    @app.tool(
        name="debug.read_memory",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def debug_read_memory(
        session_id: Annotated[str, Field(description="The live DebugSession to read memory from.")],
        address: Annotated[int, Field(description="Start address (integer) to read from.")],
        byte_count: Annotated[int, Field(description="Number of bytes to read (capped at 4096).")],
    ) -> ToolResponse:
        """Read raw memory bytes from a live DebugSession (up to 4096 bytes). Requires operator."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_memory_op(session_id, address, byte_count),
        )

    @app.tool(
        name="debug.read_registers",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def debug_read_registers(
        session_id: Annotated[
            str, Field(description="The live DebugSession to read registers from.")
        ],
        registers: Annotated[
            list[str],
            Field(description='Register names to read (e.g. ["rip", "rsp"]).'),
        ],
    ) -> ToolResponse:
        """Read named registers from a live DebugSession. Requires operator."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_registers_op(session_id, registers),
        )

    @app.tool(
        name="debug.continue",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def debug_continue(
        session_id: Annotated[
            str, Field(description="The live DebugSession to continue execution on.")
        ],
        timeout_sec: Annotated[
            float,
            Field(
                description="Seconds to wait for a stop event; 0.0 uses the provider "
                "interactive wait cap."
            ),
        ] = 0.0,
    ) -> ToolResponse:
        """Resume execution on a live DebugSession and wait for a stop event. Operator only."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _continue_op(session_id, timeout_sec),
        )

    @app.tool(
        name="debug.interrupt",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def debug_interrupt(
        session_id: Annotated[str, Field(description="The live DebugSession to interrupt.")],
    ) -> ToolResponse:
        """Send an interrupt to halt a running live DebugSession. Requires operator."""
        return await run_engine_op(
            pool, current_context(), session_id, runtime, _interrupt_op(session_id)
        )
