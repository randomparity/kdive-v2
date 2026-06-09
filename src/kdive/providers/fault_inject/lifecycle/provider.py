"""Fault-inject provider ports for deterministic local-stack exercises (ADR-0072).

Each port satisfies the typed provider contract with synthetic-but-plausible output: a
synthetic domain name from ``provision``, a loopback ``TransportHandle`` from
``open_transport``, and a stored synthetic vmcore from ``capture``. The provider drives
the full server/worker/reconciler spine without requiring libvirt while still exercising
artifact writes, transport handles, debug operations, and crash retrieval.

A single fixed synthetic build-id keeps ``build`` and ``capture`` aligned, so the mock
spine's build-id provenance check passes end to end.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

from kdive.components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction, Sensitivity
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.ports import (
    BuildOutput,
    CaptureOutput,
    CrashOutput,
    GdbBreakpointRef,
    GdbMiAttachment,
    GdbStopRecord,
    InstallRequest,
    IntrospectOutput,
    SystemHandle,
    TransportHandle,
)
from kdive.providers.ports._common import config_error
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.security.artifacts.crash_commands import validate_crash_commands

_TENANT = "fault-inject"
_RETENTION_CLASS = "vmcore"
_TRANSPORT_KINDS = frozenset({"gdbstub", "ssh"})
_LOOPBACK_HOST = "127.0.0.1"

# A plausible 40-hex GNU build-id shared by build and capture so provenance holds.
_SYNTHETIC_BUILD_ID = "fa017" + "0" * 35


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


def _domain_name(system_id: UUID) -> str:
    return f"fault-inject-{system_id}"


def _synthetic_port(handle: str) -> int:
    """Derive a stable loopback port in 1024..65535 from a domain handle."""
    digest = hashlib.blake2b(handle.encode(), digest_size=2).digest()
    return 1024 + int.from_bytes(digest, "big") % (65535 - 1024 + 1)


class FaultInjectProvision:
    """Provisioner port: mint a synthetic domain and track it in the mock inventory."""

    def __init__(self, inventory: FaultInjectInventory) -> None:
        self._inventory = inventory

    def provision(self, system_id: UUID, profile: object) -> str:
        domain = _domain_name(system_id)
        self._inventory.record(system_id, domain)
        return domain

    def teardown(self, domain_name: str) -> None:
        self._inventory.forget(domain_name)

    def reprovision(self, system_id: UUID, profile: object) -> str:
        self._inventory.forget(_domain_name(system_id))
        return self.provision(system_id, profile)


class FaultInjectBuild:
    """Builder port: store synthetic kernel + debuginfo and return their refs."""

    def __init__(self, *, store_factory: Callable[[], _StorePort]) -> None:
        self._store_factory = store_factory
        self._store: _StorePort | None = None

    def build(self, run_id: UUID, profile: object) -> BuildOutput:
        kernel = self._put(run_id, "kernel", b"fault-inject-kernel", Sensitivity.REDACTED)
        debuginfo = self._put(run_id, "vmlinux", b"fault-inject-vmlinux", Sensitivity.REDACTED)
        return BuildOutput(
            kernel_ref=kernel.key, debuginfo_ref=debuginfo.key, build_id=_SYNTHETIC_BUILD_ID
        )

    def _put(self, run_id: UUID, name: str, data: bytes, sens: Sensitivity) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind="runs",
                owner_id=str(run_id),
                name=name,
                data=data,
                sensitivity=sens,
                retention_class="kernel-build",
            )
        )


class FaultInjectInstall:
    def install(self, request: InstallRequest) -> None:
        del request
        return None

    def boot(self, system_id: UUID) -> None:
        return None


class FaultInjectConnect:
    """Connector port: open and close a loopback debug transport."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        if kind not in _TRANSPORT_KINDS:
            raise config_error(f"unknown transport kind {kind!r}")
        endpoint = TransportHandleData(kind, _LOOPBACK_HOST, _synthetic_port(str(system)))
        return TransportHandle(endpoint.encode())

    def close_transport(self, handle: TransportHandle) -> None:
        TransportHandleData.decode(handle)  # validate the handle is well-formed, then no-op


class FaultInjectControl:
    def power(self, domain_name: str, action: PowerAction) -> None:
        return None

    def force_crash(self, domain_name: str) -> None:
        return None


class FaultInjectRetrieve:
    """Retriever + CrashPostmortem ports: store a synthetic vmcore, symbolize synthetically."""

    def __init__(self, *, store_factory: Callable[[], _StorePort]) -> None:
        self._store_factory = store_factory
        self._store: _StorePort | None = None

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        raw = self._put(system_id, f"vmcore-{method.value}", Sensitivity.SENSITIVE)
        redacted = self._put(system_id, f"vmcore-{method.value}-redacted", Sensitivity.REDACTED)
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=_SYNTHETIC_BUILD_ID)

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        rejected = validate_crash_commands(commands)
        if rejected is not None:
            raise CategorizedError(
                "crash command batch rejected",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": rejected},
            )
        results: dict[str, object] = {command: "synthetic" for command in commands}
        return CrashOutput(results=results, transcript="fault-inject postmortem", truncated=False)

    def _put(self, system_id: UUID, name: str, sens: Sensitivity) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind="systems",
                owner_id=str(system_id),
                name=name,
                data=b"fault-inject-vmcore",
                sensitivity=sens,
                retention_class=_RETENTION_CLASS,
            )
        )


class FaultInjectIntrospect:
    """VmcoreIntrospector + LiveIntrospector ports: synthetic introspection output."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)


class _SyntheticGdbController:
    """A no-op gdb/MI controller for the synthetic attachment."""

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        return []

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        return []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        return []

    def exit(self) -> None:
        return None


def fault_inject_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:
    return GdbMiAttachment(
        controller=_SyntheticGdbController(),
        rsp_host=host,
        rsp_port=port,
        transcript_path=transcript_path,
    )


class FaultInjectDebugEngine:
    """GdbMiEngine port: track breakpoints in-memory and return plausible records."""

    def __init__(self) -> None:
        self._breakpoints: dict[str, GdbBreakpointRef] = {}
        self._next = 1

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> GdbBreakpointRef:
        number = str(self._next)
        self._next += 1
        ref = GdbBreakpointRef(number=number, type="breakpoint", func=location, enabled=True)
        self._breakpoints[number] = ref
        return ref

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        self._breakpoints.pop(number, None)

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[GdbBreakpointRef]:
        return list(self._breakpoints.values())

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        return bytes(byte_count)

    def read_registers(
        self, attachment: GdbMiAttachment, register_names: list[str]
    ) -> dict[str, object]:
        return {name: 0 for name in register_names}

    def continue_(self, attachment: GdbMiAttachment, *, timeout_sec: float) -> GdbStopRecord:
        return GdbStopRecord(reason="breakpoint-hit", stopped_thread="1")

    def interrupt(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        return GdbStopRecord(reason="signal-received", stopped_thread="1")
