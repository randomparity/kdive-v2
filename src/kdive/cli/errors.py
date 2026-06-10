"""Stable CLI exit codes derived from kdive ``ToolResponse`` error categories (ADR-0089).

The codes are part of the CLI's contract with scripts and CI, so they are explicit and
fixed: 1 is the generic failure, and each mapped category gets a distinct nonzero code.
"""

from __future__ import annotations

_CODES = {
    "configuration_error": 2,
    "authorization_denied": 3,
    "not_found": 4,
    "conflict": 5,
}


def exit_code_for_category(category: str) -> int:
    """Map an error category to a stable nonzero exit code.

    Args:
        category: A kdive ``ErrorCategory`` value (e.g. ``"authorization_denied"``).

    Returns:
        The mapped exit code, or ``1`` (generic failure) for an unmapped category.
    """
    return _CODES.get(category, 1)
