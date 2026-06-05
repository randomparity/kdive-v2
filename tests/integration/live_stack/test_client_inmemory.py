"""In-memory LiveStackClient tests: envelope parsing + the .data shape pin (no DB, no auth).

The probe app's tools do not read auth: the in-memory ``FastMCPTransport`` carries no access
token, so a tool calling ``current_context()`` could not run here (ADR-0044). These tests
cover only the envelope-parsing seam and the ``.data`` shape pin.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client, FastMCP

from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.harness import LiveStackClient


def _probe_app() -> FastMCP:
    app: FastMCP = FastMCP(name="probe")

    @app.tool(name="scalar.one")
    def scalar_one() -> ToolResponse:
        return ToolResponse.success("obj-1", "ok", suggested_next_actions=["next"])

    @app.tool(name="list.many")
    def list_many() -> list[ToolResponse]:
        return [ToolResponse.success("a", "ok"), ToolResponse.success("b", "ok")]

    return app


def test_call_tool_scalar_returns_one_envelope() -> None:
    async def _run() -> None:
        client = LiveStackClient(Client(_probe_app()))
        async with client:
            result = await client.call_tool("scalar.one")
        assert isinstance(result, ToolResponse)
        assert result.object_id == "obj-1"
        assert result.status == "ok"

    asyncio.run(_run())


def test_call_tool_list_returns_envelope_list() -> None:
    async def _run() -> None:
        client = LiveStackClient(Client(_probe_app()))
        async with client:
            result = await client.call_tool("list.many")
        assert isinstance(result, list)
        assert [r.object_id for r in result] == ["a", "b"]
        assert all(isinstance(r, ToolResponse) for r in result)

    asyncio.run(_run())


def test_structured_content_shape_is_pinned() -> None:
    """Pin the fastmcp 3.4.0 structured_content surface so a future change fails loudly.

    A scalar tool returns the object dict directly; a list tool is wrapped as
    ``{"result": [...]}`` (ADR-0044 §3). LiveStackClient.call_tool depends on this shape.
    """

    async def _run() -> None:
        async with Client(_probe_app()) as raw:
            scalar = await raw.call_tool("scalar.one", {})
            listed = await raw.call_tool("list.many", {})
        assert isinstance(scalar.structured_content, dict)
        assert scalar.structured_content["object_id"] == "obj-1"  # object dict, not wrapped
        assert "result" not in scalar.structured_content
        assert isinstance(listed.structured_content, dict)
        assert list(listed.structured_content) == ["result"]  # list tool -> result wrapper
        assert isinstance(listed.structured_content["result"], list)

    asyncio.run(_run())


def test_list_tools_returns_names() -> None:
    async def _run() -> None:
        client = LiveStackClient(Client(_probe_app()))
        async with client:
            names = await client.list_tools()
        assert {"scalar.one", "list.many"} <= set(names)

    asyncio.run(_run())
