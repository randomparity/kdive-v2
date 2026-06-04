"""Local-libvirt Retrieve plane: capture a kdump vmcore and run crash postmortem (ADR-0031).

`LocalLibvirtRetrieve` realizes two seam-injected ports, mirroring `LocalLibvirtBuild`:
`Retriever.capture(system_id)` waits for kdump, stores the raw `sensitive` core and a
`redacted` dmesg derivative, and returns both refs plus the core's build-id;
`CrashPostmortem.run(...)` symbolizes the core against the Run's `debuginfo_ref` over an
injected `crash` subprocess. The slow, host-bound operations are `live_vm`-gated seams, so
the orchestration and the full error contract are unit-tested with fakes. The crash-command
validator is the load-bearing security control: the postmortem path is never gated, so every
caller command is sanitized and allowlist-checked before any `crash` invocation.
"""

from __future__ import annotations

import re

# Pipe-to-shell, redirection, command substitution, chaining, backgrounding.
_DENY_CHARS = ("|", ">", "<", "`", "$(", ";", "&")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def crash_command_rejection_reason(command: str, allowlist: frozenset[str]) -> str | None:
    """Return ``None`` if the command is permitted, else a human-readable rejection reason.

    Two layers: a security-critical denylist (newline/control chars, a leading ``!`` shell
    escape, and the shell metacharacters in ``_DENY_CHARS``) and an allowlist of read-only
    leading verbs. The denylist is the boundary the ungated postmortem path relies on.
    """
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
