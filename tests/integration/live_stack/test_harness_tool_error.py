"""LiveStackClient.call_tool raises LiveStackToolError on a tool-error result (ADR-0045 §2).

The raised-RBAC path (a handler that raises an ``AuthorizationError`` rather than returning a
``ToolResponse``) surfaces as a tool-error ``CallToolResult``. These Docker-free unit tests
drive the harness over a fake client so the conversion is covered in normal CI; the
envelope-parsing path must stay intact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast

import pytest
from fastmcp import Client

from tests.integration.live_stack.harness import LiveStackClient, LiveStackToolError


@dataclass
class _FakeResult:
    is_error: bool
    structured_content: dict[str, object] | None
    content: tuple[object, ...] = field(default_factory=tuple)


@dataclass
class _FakeText:
    text: str


class _FakeClient:
    """Stands in for fastmcp.Client: returns a preset CallToolResult-shaped object."""

    def __init__(self, result: _FakeResult) -> None:
        self._result = result

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, args: dict[str, object]) -> _FakeResult:
        return self._result


def test_call_tool_raises_on_is_error() -> None:
    """A tool-error result raises LiveStackToolError carrying the tool name + error text."""
    result = _FakeResult(is_error=True, structured_content=None, content=(_FakeText("denied"),))
    client = LiveStackClient(cast(Client, _FakeClient(result)))

    async def _run() -> None:
        async with client:
            with pytest.raises(LiveStackToolError) as excinfo:
                await client.call_tool("allocations.request")
        assert excinfo.value.tool == "allocations.request"
        assert "denied" in str(excinfo.value)

    asyncio.run(_run())


def test_call_tool_parses_envelope_when_not_error() -> None:
    """A non-error result still parses structured_content into a ToolResponse (unchanged path)."""
    payload: dict[str, object] = {"object_id": "o1", "status": "granted"}
    client = LiveStackClient(
        cast(Client, _FakeClient(_FakeResult(is_error=False, structured_content=payload)))
    )

    async def _run() -> None:
        async with client:
            resp = await client.call_tool("allocations.request")
        assert not isinstance(resp, list)
        assert resp.object_id == "o1"
        assert resp.status == "granted"

    asyncio.run(_run())
