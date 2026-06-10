"""Read-only, fail-closed gate for ``kdivectl tool call`` (ADR-0089 decision 2a).

Allows only tools whose MCP ``readOnlyHint`` is exactly ``True``; anything mutating,
unannotated, or unknown is refused so the generic passthrough is never a route around
the curated break-glass verbs. This is a client-side policy/UX guard — the server-side
destructive-op gate (ADR-0006) is the real boundary — but it must still fail closed.
"""

from __future__ import annotations


class NotReadOnlyError(RuntimeError):
    """Raised when a tool is not positively annotated read-only."""


def assert_read_only(name: str, tool: object) -> None:
    """Raise unless ``tool`` carries ``annotations.readOnlyHint is True``.

    Args:
        name: The tool name, used in the refusal message.
        tool: The resolved tool object (or any object whose annotations are inspected).

    Raises:
        NotReadOnlyError: When the tool is missing annotations, missing the hint, or the
            hint is anything other than the literal ``True``.
    """
    annotations = getattr(tool, "annotations", None)
    if getattr(annotations, "readOnlyHint", None) is not True:
        raise NotReadOnlyError(
            f"{name!r} is not read-only; mutations go through curated break-glass verbs"
        )
