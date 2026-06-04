"""introspect.from_vmcore tool tests — the handler is called directly with a fake port."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import introspect as introspect_tools
from kdive.providers.local_libvirt.introspect_drgn import IntrospectOutput
from tests.mcp._seed import seed_crashed_system, seed_run_on_system


def _ctx(*, projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    return RequestContext(principal="u", agent_session="s", projects=projects, roles={})


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
            (sys_id, f"local/systems/{sys_id}/vmcore"),
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
        report = json.loads(resp.data["report"])
        assert report["sysinfo"]["release"] == "6.8.0"
        assert resp.data["truncated"] == "false"
        assert port.kwargs["expected_build_id"] == "deadbeef"
        assert port.kwargs["debuginfo_ref"] == "k/runs/r/vmlinux"
        assert str(port.kwargs["vmcore_ref"]).endswith("/vmcore")

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
        assert "hunter2" not in resp.data["report"]
        assert "[REDACTED]" in resp.data["report"]

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
        introspect_tools.register(app, pool)
        tools = await app.list_tools()
        assert any(t.name == "introspect.from_vmcore" for t in tools)

    asyncio.run(_check())
