"""Async dispatch for ``kdivectl`` subcommands (ADR-0089).

The generic read-only ``tool call`` passthrough lives here. It lists the server's tools,
fail-closed-gates the requested tool on ``readOnlyHint``, calls it, and prints the
structured result. ``login`` mints and caches a bearer token; curated verbs route through
``commands.run_verb``.
"""

from __future__ import annotations

import argparse
import json
import sys

from fastmcp.exceptions import ToolError

from kdive.cli.commands import registry as commands
from kdive.cli.passthrough import NotReadOnlyError, assert_read_only
from kdive.cli.transport import Session, tool_envelope

_NOT_READ_ONLY_EXIT = 3
_TOOL_ERROR_EXIT = 1


async def run(args: argparse.Namespace) -> int:
    """Dispatch a parsed ``kdivectl`` invocation to its handler.

    A tool that signals failure by *raising* ``ToolError`` (rather than returning a failure
    envelope) — e.g. a project-not-granted call to ``allocations.list`` — is surfaced as a
    one-line stderr message and a generic nonzero exit, never an uncaught traceback. Failures
    returned as a ``ToolResponse`` envelope keep their mapped exit code (:mod:`kdive.cli.errors`).

    Returns:
        The process exit code (0 on success; see :mod:`kdive.cli.errors` for failures).
    """
    try:
        return await _dispatch(args)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _TOOL_ERROR_EXIT


async def _dispatch(args: argparse.Namespace) -> int:
    """Route a parsed invocation to its handler (errors propagate to :func:`run`)."""
    if args.command == "tool" and args.tool_command == "call":
        return await _tool_call(args)
    if args.command == "login":
        return _login(args)
    if args.command == "doctor":
        return await commands.doctor.doctor(args)
    return await commands.run_verb(args)


def _login(args: argparse.Namespace) -> int:
    """Acquire a bearer token on the platform-role axis and cache it 0600.

    The token is never printed or logged; only a confirmation line is emitted.
    """
    from kdive.cli.login import login

    login(args.platform_role)
    role = args.platform_role or "none"
    print(f"login ok (platform_role={role}); token cached")
    return 0


async def _tool_call(args: argparse.Namespace) -> int:
    arguments = _parse_payload(args.payload)
    session = Session.from_env()
    async with session.client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        try:
            assert_read_only(args.name, tools.get(args.name))
        except NotReadOnlyError as exc:
            print(str(exc))
            return _NOT_READ_ONLY_EXIT
        result = await client.call_tool(args.name, arguments)
    print(json.dumps(tool_envelope(result), indent=2, default=str))
    return 0


def _parse_payload(payload: str) -> dict[str, object]:
    """Parse the ``--json`` payload into an arguments dict, failing on malformed input."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--json payload is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("--json payload must be a JSON object")
    return parsed
