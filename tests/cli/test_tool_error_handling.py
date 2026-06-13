"""CLI exit-code surfacing for the two server failure shapes (ADR-0089, ADR-0098).

A server tool fails one of two ways:

* **Returns** a failure ``ToolResponse`` envelope carrying an ``error_category`` — mapped to a
  stable exit code by :func:`exit_code_for_envelope`. A project-membership denial is now this
  shape: ``require_project`` raises ``ProjectMembershipDenied``, the dispatch boundary envelopes
  it as ``authorization_denied``, and the CLI derives **exit 3** (ADR-0098).
* **Raises** ``fastmcp.ToolError`` — for a non-denial server fault that surfaces as a raised
  error. The CLI must report it as a one-line stderr message and a generic **exit 1**, never an
  uncaught stack trace (the #337 backstop).
"""

from __future__ import annotations

import argparse
import asyncio

import pytest
from fastmcp.exceptions import ToolError

import kdive.cli.commands.reads as reads
from kdive.cli import dispatch


class _FakeResult:
    def __init__(self, data: dict) -> None:
        self.data = data


class _EnvelopingClient:
    """A client whose tool *returns* an ``authorization_denied`` envelope (the denial shape)."""

    async def __aenter__(self) -> _EnvelopingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict) -> _FakeResult:
        return _FakeResult(
            {
                "object_id": "allocations",
                "status": "failed",
                "error_category": "authorization_denied",
                "data": {},
                "items": [],
            }
        )


class _EnvelopingSession:
    token = "x.y.z"

    def client(self) -> _EnvelopingClient:
        return _EnvelopingClient()


class _RaisingClient:
    async def __aenter__(self) -> _RaisingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict) -> object:
        raise ToolError(f"Error calling tool '{name}': upstream provider unavailable")


class _RaisingSession:
    token = "x.y.z"

    def client(self) -> _RaisingClient:
        return _RaisingClient()


def test_membership_denial_envelope_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-member naming a project gets an authorization_denied ENVELOPE (not a raise) → exit 3.
    monkeypatch.setattr(reads, "_session_factory", lambda: _EnvelopingSession())
    args = argparse.Namespace(command="allocations", subcommand="list", json=False, project="demo")

    code = asyncio.run(dispatch.run(args))

    assert code == 3


def test_raised_tool_error_exits_nonzero_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A non-denial raised ToolError still collapses to the generic exit 1 backstop (#337).
    monkeypatch.setattr(reads, "_session_factory", lambda: _RaisingSession())
    args = argparse.Namespace(command="allocations", subcommand="list", json=False, project="demo")

    code = asyncio.run(dispatch.run(args))

    assert code == 1
    err = capsys.readouterr().err
    assert "provider unavailable" in err
    assert "Traceback" not in err
