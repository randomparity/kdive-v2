"""``kdivectl images`` verbs: list (read) + the operator/admin mutating verbs (ADR-0089).

The verbs are thin MCP clients over the shared ``images.*`` server tools — there is no second
source of truth. ``images list`` is a read passthrough (RBAC-filtered server-side to public +
the caller's projects' private rows). The mutating verbs run the fail-closed token-``exp``
preflight before their one MCP call, exactly like the break-glass mutations:
``upload``/``delete`` route the project-scoped tools, ``build``/``publish`` the
``platform_operator`` tools, and ``prune --expired``/``extend`` the ``platform_admin``
break-glass tools. A server-side denial returns a typed failure envelope the verb maps to a
non-zero exit, so an unprivileged or cross-project invocation is observable as exit ``3``.

``_session_factory`` is the seam the tests replace with a fake session.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Mapping

from kdive.cli.commands.mutations import ensure_token_valid
from kdive.cli.errors import exit_code_for_category
from kdive.cli.render import render, render_record
from kdive.cli.transport import Session


def _session_factory() -> Session:
    """Build the authenticated session; overridden in tests with a fake."""
    return Session.from_env()


async def _fetch(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
    """Call read tool ``name`` and return its envelope dict (no token preflight)."""
    session = _session_factory()
    async with session.client() as client:
        result = await client.call_tool(name, dict(arguments))
    return result.data


async def _call(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
    """Run the fail-closed token preflight, then call mutating tool ``name`` once."""
    session = _session_factory()
    ensure_token_valid(session.token, now=int(time.time()))
    async with session.client() as client:
        result = await client.call_tool(name, dict(arguments))
    return result.data


def _flatten(envelope: object) -> dict[str, object]:
    """Flatten one envelope into a row: ``id``/``state`` plus the envelope's ``data``."""
    if not isinstance(envelope, Mapping):
        return {}
    fields: Mapping[str, object] = {str(k): v for k, v in envelope.items()}
    row: dict[str, object] = {"id": fields.get("object_id"), "state": fields.get("status")}
    data = fields.get("data")
    if isinstance(data, Mapping):
        for key, value in data.items():
            row[str(key)] = value
    return row


def _rows(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    """Flatten a collection envelope's item sub-envelopes into rows."""
    items = envelope.get("items")
    if not isinstance(items, list):
        return []
    return [_flatten(item) for item in items]


def _exit_code(envelope: object) -> int:
    """Map a response envelope to its exit code: 0 on success, else the category's code."""
    if not isinstance(envelope, Mapping):
        return 0
    fields: Mapping[str, object] = {str(k): v for k, v in envelope.items()}
    category = fields.get("error_category")
    return exit_code_for_category(category) if isinstance(category, str) else 0


async def _run(name: str, arguments: Mapping[str, object], *, as_json: bool) -> int:
    """Preflight, call ``name``, render the response record, and return the exit code."""
    envelope = await _call(name, arguments)
    render_record(_flatten(envelope), as_json=as_json)
    return _exit_code(envelope)


def _capabilities(args: argparse.Namespace) -> list[str]:
    """Split the comma-separated ``--capabilities`` value into a tag list."""
    raw = getattr(args, "capabilities", None)
    if not raw:
        return []
    return [tag.strip() for tag in str(raw).split(",") if tag.strip()]


async def images_list(args: argparse.Namespace) -> int:
    """List catalog images visible to the caller (public + own-project private)."""
    envelope = await _fetch("images.list", {})
    render(
        _rows(envelope),
        columns=["id", "name", "arch", "visibility", "owner", "state"],
        as_json=args.json,
    )
    return 0


async def images_upload(args: argparse.Namespace) -> int:
    """Register a quarantined upload as a project-private image (operator on the project)."""
    arguments: dict[str, object] = {
        "project": args.project,
        "name": args.name,
        "arch": args.arch,
        "quarantine_key": args.quarantine_key,
    }
    lifetime = getattr(args, "lifetime_seconds", None)
    if lifetime is not None:
        arguments["lifetime_seconds"] = int(lifetime)
    return await _run("images.upload", arguments, as_json=args.json)


async def images_delete(args: argparse.Namespace) -> int:
    """Delete a project-private image (operator on the image's project)."""
    return await _run("images.delete", {"image_id": args.image_id}, as_json=args.json)


async def images_build(args: argparse.Namespace) -> int:
    """Enqueue an IMAGE_BUILD job for a public base image (platform_operator)."""
    return await _run(
        "images.build",
        {
            "provider": args.provider,
            "name": args.name,
            "arch": args.arch,
            "releasever": args.releasever,
            "source_image_digest": args.source_image_digest,
            "capabilities": _capabilities(args),
        },
        as_json=args.json,
    )


async def images_publish(args: argparse.Namespace) -> int:
    """Promote a built image to a public catalog row (platform_operator)."""
    return await _run(
        "images.publish",
        {
            "provider": args.provider,
            "name": args.name,
            "arch": args.arch,
            "releasever": args.releasever,
            "source_image_digest": args.source_image_digest,
            "capabilities": _capabilities(args),
        },
        as_json=args.json,
    )


async def images_prune(args: argparse.Namespace) -> int:
    """Force the expired-private-image sweep now (platform_admin break-glass).

    Raises:
        SystemExit: When ``--expired`` is not supplied; the flag is the explicit
            acknowledgement that this triggers the destructive expiry sweep.
    """
    if not getattr(args, "expired", False):
        raise SystemExit("images prune is destructive: pass --expired to confirm the sweep")
    return await _run("images.prune_expired", {"reason": args.reason}, as_json=args.json)


async def images_extend(args: argparse.Namespace) -> int:
    """Re-arm a private image's lifetime (platform_admin break-glass)."""
    return await _run(
        "images.extend",
        {"image_id": args.image_id, "seconds": int(args.seconds), "reason": args.reason},
        as_json=args.json,
    )
