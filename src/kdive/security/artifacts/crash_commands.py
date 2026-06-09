"""Crash command validation policy."""

from __future__ import annotations

import re
from collections.abc import Sequence

# Pipe-to-shell, redirection, command substitution, chaining, backgrounding.
_DENY_CHARS = ("|", ">", "<", "`", "$(", ";", "&")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
CRASH_COMMAND_ALLOWLIST: frozenset[str] = frozenset(
    {
        "bt",
        "ps",
        "log",
        "kmem",
        "sys",
        "mod",
        "struct",
        "union",
        "p",
        "rd",
        "vtop",
        "task",
        "files",
        "vm",
        "net",
        "dev",
        "irq",
        "mach",
        "runq",
        "mount",
        "swap",
        "timer",
        "dis",
        "sym",
        "list",
        "tree",
        "search",
        "foreach",
        "help",
    }
)


def crash_command_rejection_reason(command: str, allowlist: frozenset[str]) -> str | None:
    """Return ``None`` if the command is permitted, else a rejection reason."""
    stripped = command.strip()
    if not stripped:
        return "empty command"
    if _CONTROL.search(command):
        return "command contains a newline or control character"
    if stripped[0] == "!":
        return "shell escape ('!') is not permitted"
    for token in _DENY_CHARS:
        if token in command:
            return f"disallowed metacharacter {token!r}"
    verb = stripped.split()[0].lower()
    if verb not in allowlist:
        return f"verb {verb!r} is not in the crash command allowlist"
    return None


def validate_crash_commands(commands: Sequence[str]) -> str | None:
    """Return ``None`` if every command is permitted by the default crash policy."""
    for command in commands:
        reason = crash_command_rejection_reason(command, CRASH_COMMAND_ALLOWLIST)
        if reason is not None:
            return reason
    return None
