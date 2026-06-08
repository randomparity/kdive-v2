"""introspect.from_vmcore tool tests — the handler is called directly with a fake port."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import DEBUG_SESSIONS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DebugSession
from kdive.domain.state import DebugSessionState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.debug import introspect as introspect_tools
from kdive.providers.ports import IntrospectOutput
from kdive.providers.runtime import ProviderRuntime
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp._seed import seed_crashed_system, seed_run_on_system


def _ctx(
    role: Role | None = Role.VIEWER, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _output(*, comm: str = "init", truncated: bool = False) -> IntrospectOutput:
    return IntrospectOutput(
        tasks={"tasks": [{"pid": 1, "comm": comm}], "truncated": False},
        modules={"modules": [], "decode_errors": 0, "all_failed": False},
        sysinfo={"release": "6.8.0"},
        truncated=truncated,
    )


class _FakeIntrospector:
    """Records the from_vmcore kwargs; returns a canned output or raises a planted error."""

    def __init__(
        self, *, output: IntrospectOutput | None = None, raises: CategorizedError | None = None
    ) -> None:
        self._output = output if output is not None else _output()
        self._raises = raises
        self.kwargs: dict[str, object] = {}

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        self.kwargs = {
            "vmcore_ref": vmcore_ref,
            "debuginfo_ref": debuginfo_ref,
            "expected_build_id": expected_build_id,
        }
        if self._raises is not None:
            raise self._raises
        return self._output


async def _seed_vmcore_row(pool: AsyncConnectionPool, sys_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', %s, %s, 'e', 'sensitive', 'vmcore')",
            (sys_id, f"local/systems/{sys_id}/vmcore-host_dump"),
        )


async def _built_run_with_core(pool: AsyncConnectionPool) -> str:
    sys_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(
        pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
    )
    await _seed_vmcore_row(pool, sys_id)
    return run_id


def test_from_vmcore_happy_path_returns_redacted_report(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            port = _FakeIntrospector()
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status != "error"
        report = resp.data["report"]
        assert isinstance(report, dict)
        assert report["sysinfo"]["release"] == "6.8.0"
        assert resp.data["truncated"] == "false"
        assert port.kwargs["expected_build_id"] == "deadbeef"
        assert port.kwargs["debuginfo_ref"] == "k/runs/r/vmlinux"
        assert str(port.kwargs["vmcore_ref"]).endswith("/vmcore-host_dump")

    asyncio.run(_run())


def test_from_vmcore_surfaces_truncated_true(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            port = _FakeIntrospector(output=_output(truncated=True))
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status != "error"
        assert resp.data["truncated"] == "true"

    asyncio.run(_run())


def test_from_vmcore_passes_through_port_redacted_report(migrated_url: str) -> None:
    # The port is the single redaction boundary (ADR-0033 §6); the handler serializes the
    # already-redacted report verbatim. A real port returns `[REDACTED]` in place of secrets;
    # here the fake supplies that redacted shape and the handler must surface it unchanged.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            port = _FakeIntrospector(output=_output(comm="[REDACTED]"))
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status != "error"
        report = resp.data["report"]
        assert isinstance(report, dict)
        assert report["tasks"]["tasks"][0]["comm"] == "[REDACTED]"

    asyncio.run(_run())


def test_from_vmcore_unbuilt_run_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_from_vmcore_no_build_step_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id=None
            )
            await _seed_vmcore_row(pool, sys_id)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_from_vmcore_no_captured_core_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
            )
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_from_vmcore_malformed_run_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id="nope", introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_from_vmcore_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(projects=("other",)), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_from_vmcore_without_viewer_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            with pytest.raises(AuthorizationError):
                await introspect_tools.introspect_from_vmcore(
                    pool, _ctx(None), run_id=run_id, introspector=_FakeIntrospector()
                )

    asyncio.run(_run())


def test_from_vmcore_port_attach_failure_is_typed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            err = CategorizedError("drgn", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
            port = _FakeIntrospector(raises=err)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status == "error" and resp.error_category == "debug_attach_failure"

    asyncio.run(_run())


def test_register_adds_the_tool() -> None:
    from fastmcp import FastMCP

    async def _check() -> None:
        app: FastMCP = FastMCP(name="t")
        pool = AsyncConnectionPool("postgresql://unused", open=False)
        runtime = cast(
            ProviderRuntime,
            SimpleNamespace(
                vmcore_introspector=_FakeIntrospector(),
                live_introspector=_FakeLiveIntrospector(),
            ),
        )
        introspect_tools.register(app, pool, provider_runtime=runtime)
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert "introspect.from_vmcore" in names
        assert "introspect.run" in names

    asyncio.run(_check())


# --- introspect.run (live drgn over ssh, ADR-0039) -----------------------------------------


def _live_ctx(role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)):
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


class _FakeLiveIntrospector:
    """Records live introspection input; returns a canned output or raises a planted error."""

    def __init__(
        self, *, output: IntrospectOutput | None = None, raises: CategorizedError | None = None
    ) -> None:
        self._output = output if output is not None else _output()
        self._raises = raises
        self.kwargs: dict[str, object] = {}

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        self.kwargs = {"transport_handle": transport_handle, "helper": helper}
        if self._raises is not None:
            raise self._raises
        return self._output


async def _seed_live_ssh_session(
    pool: AsyncConnectionPool,
    *,
    state: DebugSessionState = DebugSessionState.LIVE,
    transport: str = "ssh",
    project: str = "proj",
) -> str:
    sys_id = await seed_crashed_system(pool, project=project)
    run_id = await seed_run_on_system(
        pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef", project=project
    )
    async with pool.connection() as conn:
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                principal="u",
                project=project,
                run_id=UUID(run_id),
                state=state,
                transport=transport,
                transport_handle=f"{transport}://127.0.0.1:22",
            ),
        )
    return str(session.id)


def test_run_live_happy_path_returns_redacted_report(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool)
            port = _FakeLiveIntrospector()
            resp = await introspect_tools.introspect_run(
                pool, _live_ctx(), session_id=session_id, helper="tasks", introspector=port
            )
        assert resp.status != "error"
        report = resp.data["report"]
        assert isinstance(report, dict)
        assert set(report) == {"tasks"}
        assert report["tasks"]["tasks"][0]["pid"] == 1
        assert port.kwargs == {"transport_handle": "ssh://127.0.0.1:22", "helper": "tasks"}

    asyncio.run(_run())


def test_run_live_masks_planted_secret_in_response(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool)
            # The port is the single redaction boundary; it returns the already-masked shape.
            port = _FakeLiveIntrospector(output=_output(comm="[REDACTED]"))
            resp = await introspect_tools.introspect_run(
                pool, _live_ctx(), session_id=session_id, helper="tasks", introspector=port
            )
        report = resp.data["report"]
        assert isinstance(report, dict)
        assert report["tasks"]["tasks"][0]["comm"] == "[REDACTED]"

    asyncio.run(_run())


def test_run_live_marks_transcript_sensitive(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                introspector=_FakeLiveIntrospector(),
            )
        # The raw drgn-over-ssh transcript is sensitive; the response advertises that so a
        # consumer never treats the report as a substitute for the redacted-only contract.
        assert resp.data["transcript_sensitivity"] == "sensitive"

    asyncio.run(_run())


def test_run_live_unknown_helper_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="exec_arbitrary",
                introspector=_FakeLiveIntrospector(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_non_live_session_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool, state=DebugSessionState.DETACHED)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                introspector=_FakeLiveIntrospector(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_non_ssh_session_is_config_error(migrated_url: str) -> None:
    # A live introspect.run requires an ssh transport, not gdbstub (ADR-0039 §4).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool, transport="gdbstub")
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                introspector=_FakeLiveIntrospector(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(projects=("other",)),
                session_id=session_id,
                helper="tasks",
                introspector=_FakeLiveIntrospector(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool)
            with pytest.raises(AuthorizationError):
                await introspect_tools.introspect_run(
                    pool,
                    _live_ctx(Role.VIEWER),
                    session_id=session_id,
                    helper="tasks",
                    introspector=_FakeLiveIntrospector(),
                )

    asyncio.run(_run())


def test_run_live_port_attach_failure_is_typed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_ssh_session(pool)
            err = CategorizedError("ssh dropped", category=ErrorCategory.TRANSPORT_FAILURE)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                introspector=_FakeLiveIntrospector(raises=err),
            )
        assert resp.status == "error" and resp.error_category == "transport_failure"

    asyncio.run(_run())


def test_run_live_malformed_session_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id="nope",
                helper="tasks",
                introspector=_FakeLiveIntrospector(),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())
