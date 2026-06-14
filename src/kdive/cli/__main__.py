"""``kdivectl`` entry point: parse args and dispatch to the async handlers (ADR-0089)."""

from __future__ import annotations

import argparse
import asyncio


def build_parser() -> argparse.ArgumentParser:
    """Build the ``kdivectl`` argument parser (login / generic read-only tool call)."""
    parser = argparse.ArgumentParser(prog="kdivectl")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="acquire and cache a bearer token")
    login.add_argument(
        "--platform-role",
        choices=["platform_admin", "platform_operator"],
        default=None,
    )

    tool = sub.add_parser("tool", help="generic MCP passthrough (read-only by default)")
    tool_sub = tool.add_subparsers(dest="tool_command", required=True)
    call = tool_sub.add_parser(
        "call",
        help="call a tool by name (read-only by default; opt in for mutating/destructive)",
    )
    call.add_argument("name")
    call.add_argument(
        "--json",
        dest="payload",
        default="{}",
        help="JSON object of tool arguments",
    )
    call.add_argument(
        "--allow-mutating",
        dest="allow_mutating",
        action="store_true",
        help="permit a mutating (non-destructive) tool",
    )
    call.add_argument(
        "--allow-destructive",
        dest="allow_destructive",
        action="store_true",
        help="permit a destructive tool (implies --allow-mutating; needs confirmation or --yes)",
    )
    call.add_argument(
        "--yes",
        dest="yes",
        action="store_true",
        help="skip the destructive-call confirmation prompt (for non-interactive use)",
    )

    from kdive.cli.commands.registry import add_subparsers

    add_subparsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run the selected subcommand, returning its exit code."""
    args = build_parser().parse_args(argv)
    from kdive.cli import dispatch

    return asyncio.run(dispatch.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
