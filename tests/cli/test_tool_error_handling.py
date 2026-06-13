"""A tool that *raises* (``fastmcp.ToolError``) is reported cleanly, not as a traceback.

Most tools signal failure by returning a failure ``ToolResponse`` envelope (mapped to an
exit code by :func:`exit_code_for_envelope`). Some server tools instead *raise* — e.g.
``allocations.list``/``accounting.usage_project`` raise ``ToolError`` for a project the
caller is not granted. The CLI must surface that as a one-line error on stderr and a
nonzero exit, never an uncaught stack trace (ADR-0089).
"""

from __future__ import annotations

import argparse
import asyncio

import pytest
from fastmcp.exceptions import ToolError

import kdive.cli.commands.reads as reads
from kdive.cli import dispatch


class _RaisingClient:
    async def __aenter__(self) -> _RaisingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict) -> object:
        raise ToolError(f"Error calling tool '{name}': project 'demo' is not granted")


class _RaisingSession:
    token = "x.y.z"

    def client(self) -> _RaisingClient:
        return _RaisingClient()


def test_raised_tool_error_exits_nonzero_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reads, "_session_factory", lambda: _RaisingSession())
    args = argparse.Namespace(command="allocations", subcommand="list", json=False, project="demo")

    code = asyncio.run(dispatch.run(args))

    assert code == 1
    err = capsys.readouterr().err
    assert "not granted" in err
    assert "Traceback" not in err
