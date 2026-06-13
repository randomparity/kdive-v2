"""The ``doctor`` verb: render the ``ops.diagnostics`` verdict and set a gate-safe exit code.

``doctor`` calls the operator-gated ``ops.diagnostics`` tool over the authenticated session,
renders one row per check (``check``/``status``/``detail``/``fix``/``provider``), and maps
the verdict to an exit code (ADR-0091 §5):

- all ``pass`` → ``0``
- any ``fail`` → :data:`_FAIL_EXIT` (a contract is violated)
- an ``error`` with no ``fail`` → :data:`_ERROR_EXIT` — a *distinct* nonzero code, because a
  check that could not run is not a passed contract and a gate must not go green on it

``fail`` dominates a co-occurring ``error`` so a real contract violation is never masked by
an unrelated down dependency. A server-side denial (the operator gate) arrives as a failure
envelope and maps through the shared category→code table, not the verdict path.

``_session_factory`` is the seam the tests replace with a fake session.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping

from kdive.cli.errors import exit_code_for_category
from kdive.cli.render import render
from kdive.cli.transport import Session, tool_envelope

_TOOL = "ops.diagnostics"
_COLUMNS = ["check", "status", "detail", "fix", "provider"]

_FAIL_EXIT = 1
_ERROR_EXIT = 6


def _session_factory() -> Session:
    """Build the authenticated session; overridden in tests with a fake."""
    return Session.from_env()


def _payload(args: argparse.Namespace) -> dict[str, object]:
    """Thread the explicit provider target and the ``--with-egress`` opt-in to the tool.

    The default run (no provider, no egress) sends an empty payload so the tool runs its
    three cheap read checks; ``--with-egress`` is only forwarded when set so the tool can
    treat the heavy egress probe as strictly opt-in.
    """
    payload: dict[str, object] = {}
    provider = getattr(args, "provider", None)
    if provider is not None:
        payload["provider"] = provider
    if getattr(args, "with_egress", False):
        payload["with_egress"] = True
    return payload


async def _call(arguments: Mapping[str, object]) -> Mapping[str, object]:
    """Call ``ops.diagnostics`` once through the authenticated session and return the envelope."""
    session = _session_factory()
    async with session.client() as client:
        result = await client.call_tool(_TOOL, dict(arguments))
    return tool_envelope(result)


def _envelope_fields(envelope: object) -> Mapping[str, object]:
    """Return the envelope's top-level fields as a string-keyed mapping (empty if malformed)."""
    if not isinstance(envelope, Mapping):
        return {}
    return {str(k): v for k, v in envelope.items()}


def _rows(fields: Mapping[str, object]) -> list[dict[str, object]]:
    """Project each per-check item envelope onto the verdict columns.

    Every cell is sanitized so untrusted tool output (a detail/fix carrying a newline) can
    never forge an extra table row or column.
    """
    items = fields.get("items")
    if not isinstance(items, list):
        return []
    rows: list[dict[str, object]] = []
    for item in items:
        data = _envelope_fields(_envelope_fields(item).get("data"))
        rows.append({column: _sanitize(data.get(column)) for column in _COLUMNS})
    return rows


def _sanitize(value: object) -> object:
    """Strip newlines/tabs from a string cell so a verdict row stays one logical line."""
    if isinstance(value, str):
        return value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return value


def _exit_code(fields: Mapping[str, object]) -> int:
    """Map the verdict to its gate-safe exit code, or to a denial's category code.

    A server-side denial returns a failure envelope carrying an ``error_category`` (e.g. the
    operator-role gate); that takes precedence and maps through the shared table. Otherwise
    ``fail`` → :data:`_FAIL_EXIT` (dominating any ``error``), an ``error`` with no ``fail`` →
    :data:`_ERROR_EXIT`, and all-``pass`` → ``0``.
    """
    category = fields.get("error_category")
    if isinstance(category, str):
        return exit_code_for_category(category)
    if _flag(fields, "has_failure"):
        return _FAIL_EXIT
    if _flag(fields, "has_error"):
        return _ERROR_EXIT
    return 0


def _flag(fields: Mapping[str, object], key: str) -> bool:
    """Read a string-encoded boolean flag from the verdict's ``data`` (``"true"``/``"false"``)."""
    data = _envelope_fields(fields.get("data"))
    return data.get(key) == "true"


async def doctor(args: argparse.Namespace) -> int:
    """Run the deployment diagnostics, render the verdict table, and return the exit code.

    Returns:
        ``0`` when every check passes; :data:`_FAIL_EXIT` on any ``fail``; :data:`_ERROR_EXIT`
        when a check could not run but none failed; or a category exit code on a server-side
        denial. The nonzero codes make ``doctor`` usable as a deployment/CI gate (ADR-0091 §5).
    """
    fields = _envelope_fields(await _call(_payload(args)))
    render(_rows(fields), columns=_COLUMNS, as_json=args.json)
    return _exit_code(fields)
