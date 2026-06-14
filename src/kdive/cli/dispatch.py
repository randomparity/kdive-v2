"""Async dispatch for ``kdivectl`` subcommands (ADR-0089, ADR-0105).

The generic ``tool call`` passthrough lives here. It lists the server's tools, classifies the
requested one into a mutation tier, admits it only when the caller opted in to that tier
(``--allow-mutating`` / ``--allow-destructive``), runs the token-``exp`` preflight for mutating
tiers, confirms a destructive call (typed ``yes`` on a TTY, or ``--yes``), calls the tool, prints
the structured result, and derives the exit code from the response envelope. ``login`` mints and
caches a bearer token; curated verbs route through ``commands.run_verb``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable

from fastmcp.exceptions import ToolError

from kdive.cli.commands import registry as commands
from kdive.cli.commands.mutations import TokenExpiringError, ensure_token_valid
from kdive.cli.errors import exit_code_for_envelope
from kdive.cli.passthrough import ToolNotAllowedError, ToolTier, assert_tool_allowed
from kdive.cli.transport import Session, tool_envelope

_TIER_NOT_ALLOWED_EXIT = 3
_TOOL_ERROR_EXIT = 1
_PREFLIGHT_TIERS = frozenset({ToolTier.MUTATING, ToolTier.DESTRUCTIVE})


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


def _session_factory() -> Session:
    """Build the authenticated session; overridden in tests with a fake."""
    return Session.from_env()


def _max_tier(args: argparse.Namespace) -> ToolTier:
    """Resolve the highest tier the caller authorized from the opt-in flags."""
    if args.allow_destructive:
        return ToolTier.DESTRUCTIVE
    if args.allow_mutating:
        return ToolTier.MUTATING
    return ToolTier.READ_ONLY


def _confirm_destructive(
    name: str, *, assume_yes: bool, is_tty: bool, read_line: Callable[[], str]
) -> bool:
    """Return whether a destructive call is confirmed.

    ``--yes`` (``assume_yes``) discharges the prompt without reading. Otherwise a non-interactive
    stdin (``is_tty`` false) is an immediate refusal — the prompt would be unanswerable — and is
    never read. On a TTY the caller must type exactly ``yes``; EOF or anything else refuses.

    Args:
        name: The destructive tool name (accepted so callers pass it positionally; the prompt text
            is built by the injected ``read_line``).
        assume_yes: Whether ``--yes`` was passed.
        is_tty: Whether stdin is interactive.
        read_line: A zero-arg callable returning the typed line (injected for tests).

    Returns:
        ``True`` to proceed, ``False`` to refuse.
    """
    del name
    if assume_yes:
        return True
    if not is_tty:
        return False
    try:
        answer = read_line()
    except EOFError:
        return False
    return answer.strip() == "yes"


def _prompt_line(name: str) -> str:
    """Print the destructive confirmation prompt and read one line from stdin."""
    return input(f"type 'yes' to call destructive tool {name!r}: ")


async def _tool_call(args: argparse.Namespace) -> int:
    """Run the tiered passthrough for ``tool call`` (ADR-0105)."""
    arguments = _parse_payload(args.payload)
    max_tier = _max_tier(args)
    session = _session_factory()
    async with session.client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        try:
            tier = assert_tool_allowed(args.name, tools.get(args.name), max_tier=max_tier)
        except ToolNotAllowedError as exc:
            print(str(exc))
            return _TIER_NOT_ALLOWED_EXIT
        if tier in _PREFLIGHT_TIERS:
            try:
                ensure_token_valid(session.token, now=int(time.time()))
            except TokenExpiringError as exc:
                print(str(exc))
                return _TIER_NOT_ALLOWED_EXIT
        if tier is ToolTier.DESTRUCTIVE and not _confirm_destructive(
            args.name,
            assume_yes=args.yes,
            is_tty=sys.stdin.isatty(),
            read_line=lambda: _prompt_line(args.name),
        ):
            print("destructive call needs confirmation: re-run with --yes for non-interactive use")
            return _TIER_NOT_ALLOWED_EXIT
        result = await client.call_tool(args.name, arguments)
    envelope = tool_envelope(result)
    print(json.dumps(envelope, indent=2, default=str))
    return exit_code_for_envelope(envelope)


def _parse_payload(payload: str) -> dict[str, object]:
    """Parse the ``--json`` payload into an arguments dict, failing on malformed input."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--json payload is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("--json payload must be a JSON object")
    return parsed
