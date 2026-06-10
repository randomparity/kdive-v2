"""Local-libvirt gdb-MI wiring: the provider attach seam over the shared engine (ADR-0034/0083).

The gdb-MI engine itself is provider-neutral (``kdive.providers.debug_common.gdbmi``); this
module keeps only local-libvirt's ``default_attach_seam`` (loopback-only via the engine's default
host policy) and its ``live_vm``-gated debuginfo resolver.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.ports import GdbMiAttachment


def _resolve_debuginfo_ref(run_id: str) -> str:  # pragma: no cover - live_vm
    """Resolve the Run's debuginfo (vmlinux) object key, mirroring the Retrieve plane's lookup.

    Raises ``MISSING_DEPENDENCY`` when the live-VM gate has not supplied the object-store
    lookup seam; the handler re-tags it ``DEBUG_ATTACH_FAILURE`` so a Debug-plane op without
    a reachable host fails as an attach failure rather than leaking the gate seam.
    """
    raise CategorizedError(
        "resolving a Run's debuginfo object runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"run_id": run_id},
    )


def default_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:  # pragma: no cover - live_vm
    """The real ``live_vm`` local attach: resolve+materialize debuginfo, spawn gdb, connect RSP."""
    debuginfo_ref = _resolve_debuginfo_ref(run_id)
    del debuginfo_ref  # the live path fetches it to a temp file before attach
    vmlinux_path = Path(tempfile.gettempdir()) / f"kdive-debuginfo-{run_id}"
    return GdbMiEngine().attach(
        host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
    )


__all__ = ["GdbMiEngine", "default_attach_seam"]
