"""Mutating operator verbs: route through break-glass tools; fail-closed on token expiry.

Each destructive verb is single-call and re-runnable: the server-side break-glass tools
(``ops.force_teardown``/``ops.force_release``/``resources.drain``) return success
idempotently against already-torn-down/already-released state, and ``resources.cordon``
re-cordons a cordoned host as a no-op. Before its one MCP call each verb runs a fail-closed
token-``exp`` preflight (:func:`ensure_token_valid`) so a near-expired token is refused up
front rather than risking a mid-operation 401; a refused token costs nothing to re-acquire
and re-run (ADR-0089).

These tools are ``destructive()``-annotated server-side, so the read-only ``tool call``
passthrough cannot reach them — the curated verb is the only client path.

``_session_factory`` is the seam the tests replace with a fake session.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import time
from collections.abc import Mapping

from kdive.cli.errors import exit_code_for_envelope
from kdive.cli.render import render_record
from kdive.cli.transport import Session, tool_envelope


class TokenExpiringError(RuntimeError):
    """The bearer token is missing/unparsable ``exp`` or too close to expiry to start."""


def _decode_exp(token: str) -> int | None:
    """Return the integer ``exp`` claim of ``token``, or ``None`` if absent/unparsable.

    Fail-closed: any structural problem (missing body segment, undecodable base64url,
    non-JSON body, missing or non-integer ``exp``) yields ``None`` rather than raising,
    so the caller treats it as "not provably valid" and refuses.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    body = parts[1]
    body += "=" * (-len(body) % 4)
    try:
        decoded = base64.urlsafe_b64decode(body)
        claims = json.loads(decoded)
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, Mapping):
        return None
    exp = claims.get("exp")
    return exp if isinstance(exp, int) and not isinstance(exp, bool) else None


def ensure_token_valid(token: str, *, now: int, margin_s: int = 30) -> None:
    """Refuse unless ``token`` has an ``exp`` more than ``margin_s`` seconds past ``now``.

    Fail-closed: a missing or unparsable ``exp``, or an ``exp`` within (or at) the margin,
    raises. The token is never included in the error message.

    Args:
        token: The bearer token to inspect (its ``exp`` claim only).
        now: The current epoch seconds.
        margin_s: Minimum remaining lifetime, in seconds, required to proceed.

    Raises:
        TokenExpiringError: When ``exp`` is missing/unparsable or ``exp - now <= margin_s``.
    """
    exp = _decode_exp(token)
    if exp is None or exp - now <= margin_s:
        raise TokenExpiringError(
            "token missing exp or expiring soon; run `kdivectl login` and retry"
        )


def _session_factory() -> Session:
    """Build the authenticated session; overridden in tests with a fake."""
    return Session.from_env()


async def _call(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
    """Run the token preflight, then call break-glass tool ``name`` once.

    The preflight reads the session's own token so a near-expired credential is refused
    before the destructive call is dispatched.
    """
    session = _session_factory()
    ensure_token_valid(session.token, now=int(time.time()))
    async with session.client() as client:
        result = await client.call_tool(name, dict(arguments))
    return tool_envelope(result)


def _flatten(envelope: object) -> dict[str, object]:
    """Flatten one response envelope into a record: ``id``/``state`` plus its ``data``."""
    if not isinstance(envelope, Mapping):
        return {}
    fields: Mapping[str, object] = {str(k): v for k, v in envelope.items()}
    record: dict[str, object] = {"id": fields.get("object_id"), "state": fields.get("status")}
    data = fields.get("data")
    if isinstance(data, Mapping):
        for key, value in data.items():
            record[str(key)] = value
    return record


async def _run(name: str, arguments: Mapping[str, object], *, as_json: bool) -> int:
    """Preflight, call ``name``, render the response record, and return the exit code.

    A break-glass tool denies by *returning* a failure ``ToolResponse`` (e.g.
    ``authorization_denied`` from the ``platform_admin`` gate), not by raising, so the exit
    code is derived from the envelope's ``error_category`` (:func:`exit_code_for_envelope`) —
    this is what makes a separation-of-duties denial observable as exit ``3`` (ADR-0089).
    """
    envelope = await _call(name, arguments)
    render_record(_flatten(envelope), as_json=as_json)
    return exit_code_for_envelope(envelope)


async def run_mutating_tool(name: str, arguments: Mapping[str, object], *, as_json: bool) -> int:
    """Run one mutating MCP tool for sibling command modules."""
    return await _run(name, arguments, as_json=as_json)


async def teardown(args: argparse.Namespace) -> int:
    """Break-glass teardown of a stuck System by id (requires ``--force``).

    Raises:
        SystemExit: When ``--force`` is not supplied; the flag is the explicit break-glass
            acknowledgement for this destructive verb.
    """
    if not getattr(args, "force", False):
        raise SystemExit("teardown is destructive: pass --force to confirm break-glass")
    arguments = {"system_id": args.system_id, "reason": args.reason}
    return await _run("ops.force_teardown", arguments, as_json=args.json)


async def allocations_force_release(args: argparse.Namespace) -> int:
    arguments = {"allocation_id": args.allocation_id, "reason": args.reason}
    return await _run("ops.force_release", arguments, as_json=args.json)


async def resources_cordon(args: argparse.Namespace) -> int:
    return await _run("resources.cordon", {"resource_id": args.resource_id}, as_json=args.json)


async def resources_drain(args: argparse.Namespace) -> int:
    """Cordon a host, then report (``passive``) or force-release (``force_release``) it."""
    arguments: dict[str, object] = {
        "resource_id": args.resource_id,
        "mode": getattr(args, "mode", None) or "passive",
    }
    reason = getattr(args, "reason", None)
    if reason is not None:
        arguments["reason"] = reason
    return await _run("resources.drain", arguments, as_json=args.json)
