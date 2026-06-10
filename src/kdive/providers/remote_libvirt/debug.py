"""Remote-libvirt gdb-MI attach seam over the shared engine (ADR-0079/0083).

The gdb subprocess still runs on the worker; the only difference from local is the host policy
(ACL-remote, not loopback) and the debuginfo resolver (the remote build's vmlinux). Both the
resolver and the real attach are ``live_vm``-gated, so off-gate the seam fails closed with
``MISSING_DEPENDENCY`` and unit tests assert that contract.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.debug_common.hostpolicy import allow_acl_remote
from kdive.providers.ports import GdbMiAttachment


def _resolve_remote_debuginfo_ref(run_id: str) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "resolving a remote Run's debuginfo object runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"run_id": run_id},
    )


def remote_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:
    """Resolve+materialize the remote debuginfo, spawn gdb, connect RSP (ACL-remote policy).

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` off the ``live_vm`` gate (the debuginfo
            resolver seam); ``DEBUG_ATTACH_FAILURE`` for a gdb/RSP attach fault on a live host.
    """
    debuginfo_ref = _resolve_remote_debuginfo_ref(run_id)  # fails closed off-gate
    del debuginfo_ref  # the live path fetches it to a temp file before attach
    vmlinux_path = Path(tempfile.gettempdir()) / f"kdive-remote-debuginfo-{run_id}"
    return GdbMiEngine(host_policy=allow_acl_remote).attach(
        host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
    )


__all__ = ["remote_attach_seam"]
