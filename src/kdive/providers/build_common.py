"""Shared kernel-build fragment helpers for both libvirt providers (ADR-0096).

The local and remote build planes are independent modules (ADR-0076), but the kdump
config-fragment survival check is pure text logic identical on both. Hoisting it here keeps
the two providers' fragment handling from drifting; the merge/olddefconfig orchestration that
calls these stays provider-local (it threads each provider's typed failure helper).
"""

from __future__ import annotations


def _fragment_symbols(fragment_text: str) -> list[str]:
    """The ``CONFIG_X`` names a fragment sets to ``=y``/``=m`` (ignoring comments/blanks)."""
    symbols = []
    for raw in fragment_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if value in ("y", "m"):
            symbols.append(name)
    return symbols


def _dropped_fragment_symbols(fragment_text: str, final_config_text: str) -> list[str]:
    """Fragment symbols absent from the final ``.config`` (dropped by olddefconfig)."""
    enabled = {
        line.split("=", 1)[0]
        for line in final_config_text.splitlines()
        if line and not line.startswith("#") and line.rstrip().endswith(("=y", "=m"))
    }
    return [sym for sym in _fragment_symbols(fragment_text) if sym not in enabled]


__all__ = [
    "_dropped_fragment_symbols",
    "_fragment_symbols",
]
