"""Scoped path-safety for the file-ref secret backend (ADR-0027 §4).

A scoped port of the PoC ``kdive.safety.paths``: only the single
``confine_to_root`` primitive the file-ref backend needs is ported. The PoC's
run-id / Linux-tree / external-artifact / ``vmlinux`` validators depend on modules
out of scope for #25 (``SecretReference``, ``read_elf_build_id``, run-dir
confinement) and return with the planes that own them.
"""

from __future__ import annotations

from pathlib import Path

# Defense-in-depth only. The primary defense against shell injection is that every
# subprocess is invoked with a list argv and shell=False — no validated value is
# interpolated into a shell string. This set rejects the unambiguously dangerous
# shell-control characters without rejecting glob/brace/tilde chars that occur in
# legitimate filesystem paths.
_SHELL_METACHARS = set(";|&`$<>\\")


class PathSafetyError(ValueError):
    """A path failed a containment or character-safety check."""


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def confine_to_root(path: Path, *, allowed_root: Path) -> Path:
    r"""Resolve *path* and require it to live under *allowed_root*.

    Rejects shell metacharacters / control characters first, then resolves *path*
    with ``resolve(strict=False)`` so symlinks in existing components are followed
    (a final-component symlink whose target escapes the root is caught by the
    containment check) while a not-yet-existing tail under the root is admitted
    lexically. Existence is **not** asserted here — the caller layers that check.

    The check is point-in-time: a TOCTOU window exists between confining a path and
    any later use. For M0 this is bounded by worker-host filesystem trust (ADR-0012);
    a caller that acts much later must re-confine.

    Operator contract: a confined path must not contain shell-control characters
    (``;|&`` `` ` `` ``$<>\``) or control characters. These are legal on a POSIX
    filesystem but are rejected as defense-in-depth, so a secrets root and the files
    under it must avoid them. The control-character rejection is the load-bearing
    guard; the shell-metachar set is the same defense-in-depth the PoC carried.
    """
    text = str(path)
    if any(char in _SHELL_METACHARS for char in text) or any(ord(char) < 32 for char in text):
        raise PathSafetyError("secret file reference contains unsafe characters")
    resolved = path.expanduser().resolve()
    if _is_relative_to(resolved, allowed_root.expanduser().resolve()):
        return resolved
    raise PathSafetyError(f"secret file reference escapes the allowed root: {path!r}")
