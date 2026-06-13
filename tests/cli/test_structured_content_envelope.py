"""kdivectl reads the MCP-native ``structured_content``, not fastmcp's ``.data``.

Regression for the live failure: ``ToolResponse.items`` is self-referential, so a tool's
output schema is cyclic and fastmcp's client cannot rebuild the typed ``.data`` view —
it logs "maximum recursion depth exceeded" and leaves ``CallToolResult.data`` ``None``
while ``structured_content`` still carries the same envelope dict. Every curated verb must
flatten ``structured_content`` so the CLI works against a real server (ADR-0089).
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

import kdive.cli.commands.reads as reads
from kdive.cli.transport import tool_envelope


class _Result:
    """A CallToolResult stand-in: ``data`` is ``None`` (the recursive-schema failure)."""

    def __init__(self, *, data: object, structured_content: object) -> None:
        self.data = data
        self.structured_content = structured_content


class _FakeClient:
    def __init__(self, envelope: dict) -> None:
        self._envelope = envelope
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict) -> _Result:
        self.calls.append((name, arguments))
        return _Result(data=None, structured_content=self._envelope)


class _FakeSession:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def client(self) -> _FakeClient:
        return self._client


def test_tool_envelope_prefers_structured_content() -> None:
    envelope = {"object_id": "x", "status": "ok"}
    assert tool_envelope(_Result(data=None, structured_content=envelope)) is envelope


def test_tool_envelope_falls_back_to_data() -> None:
    envelope = {"object_id": "x", "status": "ok"}
    assert tool_envelope(_Result(data=envelope, structured_content=None)) is envelope


def test_tool_envelope_raises_when_no_mapping_envelope() -> None:
    with pytest.raises(RuntimeError, match="no structured envelope"):
        tool_envelope(_Result(data=None, structured_content=None))


def test_resources_list_renders_from_structured_content_when_data_is_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = {
        "object_id": "resources",
        "status": "ok",
        "data": {"count": "1"},
        "items": [
            {
                "object_id": "r1",
                "status": "ok",
                "data": {"kind": "local-libvirt", "host": "qemu:///system"},
                "items": [],
            }
        ],
    }
    client = _FakeClient(envelope)
    monkeypatch.setattr(reads, "_session_factory", lambda: _FakeSession(client))

    code = asyncio.run(reads.resources_list(argparse.Namespace(json=False, kind=None)))

    assert code == 0
    out = capsys.readouterr().out
    assert "r1" in out
    assert "local-libvirt" in out
