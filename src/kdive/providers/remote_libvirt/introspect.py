"""Remote-libvirt introspection ports (ADR-0079/0083).

`RemoteVmcoreIntrospect` runs the offline drgn path on the worker (fetch core + vmlinux, verify
build-id provenance, run the shared helpers, redact + byte-cap) — no live reachability.
`RemoteLiveIntrospect` runs the in-guest drgn helper via the guest-agent seam. Both reuse
``debug_common.introspect.assemble_report`` as the single redaction boundary. The drgn open/exec
paths are ``live_vm``-gated; orchestration, provenance, and error contracts are unit-tested with
fakes.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.introspect import assemble_report
from kdive.providers.ports import IntrospectOutput
from kdive.security.secrets.secret_registry import SecretRegistry

_REPORT_BYTE_CAP = 1 << 20


class _Program(Protocol):
    def iter_tasks(self) -> list[object]: ...
    def iter_modules(self) -> list[object]: ...
    def uts(self) -> dict[str, str]: ...
    def boot_cmdline(self) -> str: ...
    def cpus_online(self) -> int: ...
    def mem_total_pages(self) -> int: ...


type _FetchObject = Callable[[str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class RemoteVmcoreIntrospect:
    """Worker-side offline drgn introspection of a remote-captured vmcore (ADR-0033/0083)."""

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

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteVmcoreIntrospect:
        """Build from env; drgn seams left None (off-gate ``from_vmcore`` raises before any IO)."""
        return cls(
            fetch_object=_real_fetch_object,
            read_vmcore_build_id=_real_read_vmcore_build_id,
            secret_registry=secret_registry,
        )

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Open the core, run the helpers, return a redacted, size-bounded report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` off the ``live_vm`` gate;
                ``CONFIGURATION_ERROR`` for a build-id provenance mismatch;
                ``INFRASTRUCTURE_FAILURE`` for object-store IO; ``DEBUG_ATTACH_FAILURE`` if drgn
                cannot open the core.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected_build_id:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            program = self._open(Path(core_file.name), Path(vmlinux_file.name))
            tasks = self._run_helper(program, "tasks")
            modules = self._run_helper(program, "modules")
            sysinfo = self._run_helper(program, "sysinfo")
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=_REPORT_BYTE_CAP,
            secret_registry=self._secret_registry,
        )

    def _open(self, core: Path, vmlinux: Path) -> _Program:
        assert self._open_program is not None
        try:
            return self._open_program(core, vmlinux)
        except CategorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any drgn open fault becomes a typed attach failure
            raise CategorizedError(
                "drgn could not open the vmcore against the supplied vmlinux",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            ) from exc


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    from kdive.store.objectstore import object_store_from_env

    return object_store_from_env().get_artifact(ref, None).data


def _real_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = ["RemoteVmcoreIntrospect"]
