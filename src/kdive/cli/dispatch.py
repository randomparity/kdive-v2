"""Async dispatch for ``kdivectl`` subcommands (ADR-0089).

The generic read-only ``tool call`` passthrough lives here. It lists the server's tools,
fail-closed-gates the requested tool on ``readOnlyHint``, calls it, and prints the
structured result. ``login`` and the curated mutating verbs are wired by later M2.2 tasks.
"""

from __future__ import annotations

import argparse
import json

from kdive.cli.passthrough import NotReadOnlyError, assert_read_only
from kdive.cli.transport import Session

_NOT_READ_ONLY_EXIT = 3


async def run(args: argparse.Namespace) -> int:
    """Dispatch a parsed ``kdivectl`` invocation to its handler.

    Returns:
        The process exit code (0 on success; see :mod:`kdive.cli.errors` for failures).
    """
    if args.command == "tool" and args.tool_command == "call":
        return await _tool_call(args)
    if args.command == "login":
        raise SystemExit("`kdivectl login` is not available yet (M2.2/2)")
    raise SystemExit(f"unknown command: {args.command}")


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
    print(json.dumps(result.data, indent=2, default=str))
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
