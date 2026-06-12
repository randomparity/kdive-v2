"""Offline drgn introspection of a captured vmcore on the host (ADR-0033).

`LocalLibvirtVmcoreIntrospect` realizes the `VmcoreIntrospector` port, mirroring
`LocalLibvirtRetrieve`'s `CrashPostmortem`: fetching the raw core + `vmlinux` from the
object store, verifying the core's build-id against the Run's recorded build-id
(provenance), opening drgn against the staged core, and running three fixed helpers
(tasks, modules, sysinfo). The drgn open/helper path is `live_vm`-gated, so the
orchestration, provenance, dispatch, byte-cap, and redaction are unit-tested with a fake
`_Program`. The assembled report is `Redactor`-scrubbed **inside the port** — the port is
the single redaction boundary, so any later persistence is of already-redacted text. The
real drgn package is an operator-provided live-host prerequisite, not a normal service
dependency; these ports stay disabled until the live runner injects drgn-backed seams.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.introspect import (
    _REPORT_BYTE_CAP,
    _Program,
    assemble_report,
)
from kdive.providers.ports import IntrospectOutput, LiveIntrospector, VmcoreIntrospector
from kdive.security.secrets.secret_registry import SecretRegistry

# --- LocalLibvirtVmcoreIntrospect (the realized port) --------------------------------------

type _FetchObject = Callable[[str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class LocalLibvirtVmcoreIntrospect:
    """The realized offline-introspection port (ADR-0033).

    Stages the raw core + ``vmlinux`` from the object store, verifies the core's build-id
    against the Run's recorded build-id (provenance), opens drgn against the staged core
    (``live_vm`` seam), runs the three helpers, redacts and byte-caps the assembled report,
    and returns it — the port is the single redaction boundary.

    The drgn seams (``open_program``/``run_helper``) are ``None`` off-gate; ``from_vmcore``
    then raises ``MISSING_DEPENDENCY`` before touching the store, mirroring
    ``LocalLibvirtRetrieve.run``'s seam guard. The ``live_vm`` runner injects real seams.
    """

    def __init__(
        self,
        *,
        fetch_object: _FetchObject,
        read_vmcore_build_id: _ReadBuildId,
        secret_registry: SecretRegistry,
        open_program: _OpenProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._fetch_object = fetch_object
        self._read_vmcore_build_id = read_vmcore_build_id
        self._secret_registry = secret_registry
        self._open_program = open_program
        self._run_helper = run_helper
        self._report_byte_cap = _REPORT_BYTE_CAP

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtVmcoreIntrospect:
        """Build from env; does not import drgn or open the store (lazy ``live_vm`` seams).

        The drgn seams are left ``None``, so ``from_vmcore`` raises ``MISSING_DEPENDENCY``
        up front off-gate — it never reads the store or imports drgn. The ``live_vm`` runner
        constructs the port with real seams on a host where the operator has provided drgn.
        """
        return cls(
            fetch_object=_real_fetch_object,
            read_vmcore_build_id=_real_read_vmcore_build_id,
            secret_registry=secret_registry,
        )

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Open the core, run the helpers, and return a redacted, size-bounded report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if the drgn seams were not configured
                (off-gate); ``CONFIGURATION_ERROR`` for a malformed ref rejected by an
                injected fetch/build-id seam or a build-id provenance mismatch;
                ``STALE_HANDLE`` when a referenced object is missing;
                ``INFRASTRUCTURE_FAILURE`` for object-store IO failures; or
                ``DEBUG_ATTACH_FAILURE`` if drgn cannot open the core or load the vmlinux.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        self._verify_provenance(vmcore_bytes, expected_build_id, vmcore_ref)
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            program = self._open(self._open_program, Path(core_file.name), Path(vmlinux_file.name))
            tasks = self._run_helper(program, "tasks")
            modules = self._run_helper(program, "modules")
            sysinfo = self._run_helper(program, "sysinfo")
        return self._assemble(tasks, modules, sysinfo)

    def _verify_provenance(self, vmcore_bytes: bytes, expected: str, vmcore_ref: str) -> None:
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )

    @staticmethod
    def _open(open_program: _OpenProgram, core: Path, vmlinux: Path) -> _Program:
        try:
            return open_program(core, vmlinux)
        except CategorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any drgn open fault becomes a typed attach failure
            raise CategorizedError(
                "drgn could not open the vmcore against the supplied vmlinux",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            ) from exc

    def _assemble(
        self,
        tasks: dict[str, object],
        modules: dict[str, object],
        sysinfo: dict[str, object],
    ) -> IntrospectOutput:
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=self._report_byte_cap,
            secret_registry=self._secret_registry,
        )


def _normalize_attach_error(exc: Exception, message: str) -> CategorizedError:
    """A categorized fault passes through; any other open fault becomes an attach failure."""
    if isinstance(exc, CategorizedError):
        return exc
    return CategorizedError(message, category=ErrorCategory.DEBUG_ATTACH_FAILURE)


# --- LocalLibvirtLiveIntrospect (the live drgn-over-SSH port, ADR-0039) ---------------------

type _OpenLiveProgram = Callable[[str], _Program]


class LocalLibvirtLiveIntrospect:
    """The realized live-introspection port (ADR-0039 §3).

    Attaches drgn to the **running** guest kernel over the session's transport handle
    (drgn-over-SSH), runs one selected helper from the same fixed set as the offline port,
    and returns the same redacted, byte-bounded report. The port is the single redaction
    boundary.

    The drgn seams (``open_live_program``/``run_helper``) are ``None`` off-gate; ``run`` then
    raises ``MISSING_DEPENDENCY``, mirroring the offline port's seam guard. The ``live_vm``
    runner injects the real ``open_live_program`` on a host where the operator has provided
    drgn; that seam opens drgn against the live kernel over the already-authenticated
    transport.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        open_live_program: _OpenLiveProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._secret_registry = secret_registry
        self._open_live_program = open_live_program
        self._run_helper = run_helper
        self._report_byte_cap = _REPORT_BYTE_CAP

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtLiveIntrospect:
        """Build from env; the drgn seam is left ``None`` so ``introspect_live`` raises off-gate."""
        return cls(secret_registry=secret_registry)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        """Attach drgn to the live kernel, run one helper, return a redacted report.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if ``helper`` is not one of the fixed
                in-tree helper names.
            CategorizedError: ``MISSING_DEPENDENCY`` if the drgn seams were not configured
                (off-gate); a transport-layer ``CategorizedError`` (``transport_failure`` /
                ``debug_attach_failure``) propagated from the live seam; ``DEBUG_ATTACH_FAILURE``
                if drgn cannot attach to the live kernel for any other reason.
        """
        if self._open_live_program is None or self._run_helper is None:
            raise CategorizedError(
                "live drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        try:
            program = self._open_live_program(transport_handle)
        except Exception as exc:  # noqa: BLE001 - any live-attach fault becomes a typed failure
            raise _normalize_attach_error(
                exc, "drgn could not attach to the live guest kernel"
            ) from exc
        if helper == "tasks":
            tasks = self._run_helper(program, "tasks")
            modules: dict[str, object] = {}
            sysinfo: dict[str, object] = {}
        elif helper == "modules":
            tasks = {}
            modules = self._run_helper(program, "modules")
            sysinfo = {}
        elif helper == "sysinfo":
            tasks = {}
            modules = {}
            sysinfo = self._run_helper(program, "sysinfo")
        else:
            raise CategorizedError(
                f"unknown live introspection helper: {helper}",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=self._report_byte_cap,
            secret_registry=self._secret_registry,
        )


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    from kdive.store.objectstore import object_store_from_env

    # The ref is a key the system itself produced; there is no client etag handle, so the
    # read is unconditional (ADR-0054). An empty etag would 412 here, not skip the check.
    return object_store_from_env().get_artifact(ref, None).data


def _real_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = [
    "IntrospectOutput",
    "LiveIntrospector",
    "LocalLibvirtLiveIntrospect",
    "LocalLibvirtVmcoreIntrospect",
    "VmcoreIntrospector",
]
