"""Tiered, fail-closed gate for ``kdivectl tool call`` (ADR-0107, relaxes ADR-0089 decision 2a).

``classify_tool`` derives a :class:`ToolTier` from a tool's MCP ``ToolAnnotations`` (the same
``readOnlyHint`` / ``destructiveHint`` the server registers, ADR-0047). ``assert_tool_allowed``
admits a tool only when its tier is at or below the ``max_tier`` the caller authorized via
``--allow-mutating`` / ``--allow-destructive``; read-only stays the zero-flag default. ``UNKNOWN``
(unannotated, unresolvable, or a hint that is not a literal ``True``/``False``) is fail-closed and
unreachable at every tier, so nothing slips through unclassified.

This is a client-side policy/UX guard — the server-side destructive-op gate (ADR-0006/0020) is the
real authorization boundary — but it must still fail closed.
"""

from __future__ import annotations

import enum


class ToolTier(enum.Enum):
    """The mutation tier of a tool, derived from its MCP annotations.

    ``UNKNOWN`` is the fail-closed sentinel for a tool that is not positively classified (missing
    annotations, an absent tool, or a hint that is not the literal ``True``/``False``). It is never
    admitted by any opt-in flag.
    """

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


_RANK = {ToolTier.READ_ONLY: 0, ToolTier.MUTATING: 1, ToolTier.DESTRUCTIVE: 2}

_FLAG_FOR_TIER = {
    ToolTier.MUTATING: "--allow-mutating",
    ToolTier.DESTRUCTIVE: "--allow-destructive",
}


class ToolNotAllowedError(RuntimeError):
    """Raised when a tool's tier exceeds the caller-authorized ``max_tier`` (or is ``UNKNOWN``)."""


def classify_tool(tool: object) -> ToolTier:
    """Classify ``tool`` into a :class:`ToolTier` from its ``annotations`` hint flags.

    ``READ_ONLY`` dominates: a tool whose ``readOnlyHint`` is the literal ``True`` is read-only even
    if a destructive hint is also set. A ``readOnlyHint`` of literal ``False`` then splits on
    ``destructiveHint`` (literal ``True`` → ``DESTRUCTIVE``, else ``MUTATING``). Anything else — a
    missing/``None`` hint, a truthy-but-not-``True`` value, missing annotations, or a ``None``
    tool — is ``UNKNOWN`` (fail-closed).

    Args:
        tool: The resolved tool object (or any object whose ``annotations`` are inspected).

    Returns:
        The tool's :class:`ToolTier`.
    """
    annotations = getattr(tool, "annotations", None)
    read_only = getattr(annotations, "readOnlyHint", None)
    if read_only is True:
        return ToolTier.READ_ONLY
    if read_only is False:
        destructive = getattr(annotations, "destructiveHint", None)
        return ToolTier.DESTRUCTIVE if destructive is True else ToolTier.MUTATING
    return ToolTier.UNKNOWN


def assert_tool_allowed(name: str, tool: object, *, max_tier: ToolTier) -> ToolTier:
    """Admit ``tool`` iff its tier is at or below ``max_tier``; raise otherwise.

    Args:
        name: The tool name, used in the refusal message.
        tool: The resolved tool object (or any object whose annotations are inspected).
        max_tier: The highest tier the caller authorized for this invocation.

    Returns:
        The tool's resolved :class:`ToolTier` (so the caller knows whether to confirm a destructive
        call).

    Raises:
        ToolNotAllowedError: When the tool is ``UNKNOWN`` (refused at every tier), or its tier
            exceeds ``max_tier``. The message names the tool and the flag that would admit it
            (``UNKNOWN`` names no flag — it is unreachable).
    """
    tier = classify_tool(tool)
    if tier is ToolTier.UNKNOWN:
        raise ToolNotAllowedError(
            f"{name!r} is not positively classified (read-only/mutating/destructive); "
            "it is unreachable via `tool call`"
        )
    if _RANK[tier] <= _RANK[max_tier]:
        return tier
    raise ToolNotAllowedError(f"{name!r} is {tier.value}; pass {_FLAG_FOR_TIER[tier]} to call it")
