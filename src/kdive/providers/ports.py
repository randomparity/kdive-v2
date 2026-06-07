"""Handler-facing provider ports used by MCP tools and worker handlers.

Concrete providers satisfy these contracts structurally. Provider implementation modules may
import these types, but MCP and worker code should not import provider-specific contracts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction
from kdive.profiles.build import ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.interfaces import SystemHandle, TransportHandle
from kdive.store.objectstore import HeadResult, StoredArtifact

_TRANSPORT_KINDS = frozenset({"gdbstub", "ssh"})


class BuildOutput(NamedTuple):
    kernel_ref: str
    debuginfo_ref: str
    build_id: str


class ValidatedUpload(NamedTuple):
    output: BuildOutput
    heads: dict[str, HeadResult]


class CaptureOutput(NamedTuple):
    raw: StoredArtifact
    redacted: StoredArtifact
    vmcore_build_id: str


class CrashResult(NamedTuple):
    exit_status: int
    stdout: bytes
    stderr: bytes


class CrashOutput(NamedTuple):
    results: dict[str, object]
    transcript: str
    truncated: bool


class IntrospectOutput(NamedTuple):
    tasks: dict[str, object]
    modules: dict[str, object]
    sysinfo: dict[str, object]
    truncated: bool


class _ProviderModel(BaseModel):
    """Frozen wire shape for provider-returned records."""

    model_config = ConfigDict(extra="forbid")


class GdbFrame(_ProviderModel):
    """One stack frame from a gdb/MI stop record."""

    level: int | None = None
    func: str | None = None
    addr: str | None = None
    file: str | None = None
    line: int | None = None


class GdbStopRecord(_ProviderModel):
    """A parsed gdb/MI stop record."""

    reason: str | None = None
    bkptno: str | None = None
    stopped_thread: str | None = None
    frame: GdbFrame | None = None
    timed_out: bool = False


class GdbBreakpointRef(_ProviderModel):
    """One gdb/MI breakpoint reference."""

    number: str
    type: str | None = None
    addr: str | None = None
    func: str | None = None
    what: str | None = None
    enabled: bool | None = None


class GdbController(Protocol):
    """Controller operations a gdb/MI attachment exposes."""

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]: ...
    def read(self, *, timeout_sec: float) -> list[dict[str, object]]: ...
    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]: ...
    def exit(self) -> None: ...


@dataclass
class GdbMiAttachment:
    """A live gdb/MI attachment plus endpoint and transcript metadata."""

    controller: GdbController
    rsp_host: str
    rsp_port: int
    transcript_path: Path
    records: list[object] = field(default_factory=list)


class TransportHandleData(NamedTuple):
    """A decoded transport handle: the transport kind and its loopback endpoint."""

    kind: str
    host: str
    port: int

    def encode(self) -> str:
        """Serialize to the ``<kind>://host:port`` wire form."""
        return f"{self.kind}://{self.host}:{self.port}"

    @classmethod
    def decode(cls, raw: str) -> TransportHandleData:
        """Parse a serialized ``<kind>://host:port`` handle."""
        scheme, sep, remainder = raw.partition("://")
        if not sep or scheme not in _TRANSPORT_KINDS:
            raise _config_error("transport handle has no known transport scheme")
        host, sep, port_text = remainder.rpartition(":")
        if not sep or not host:
            raise _config_error("transport handle must be <kind>://host:port")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise _config_error("transport handle port must be numeric") from exc
        if port <= 0 or port > 65535:
            raise _config_error("transport handle port is outside 1..65535")
        return cls(scheme, host, port)


class Provisioner(Protocol):
    """Provisioning port keyed on the already-minted System id."""

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Create and start a System, returning the provider domain name.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid provider-specific profile
                data, ``PROVISIONING_FAILURE`` for domain/rootfs creation failures, or
                ``INFRASTRUCTURE_FAILURE`` for provider-control-plane faults.
        """
        ...

    def teardown(self, domain_name: str) -> None:
        """Destroy provider state for a domain name.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the provider cannot complete
                or verify teardown.
        """
        ...

    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Replace a System's provider state, returning the new provider domain name.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid provider-specific profile
                data, ``PROVISIONING_FAILURE`` for replacement-domain creation failures, or
                ``INFRASTRUCTURE_FAILURE`` for teardown/control-plane faults.
        """
        ...


class Builder(Protocol):
    """Build port returning stored kernel and debuginfo refs."""

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        """Build a kernel and store its boot artifact plus debuginfo.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for unresolvable refs or malformed
                build input, ``MISSING_DEPENDENCY`` for absent build tools or source roots,
                ``INFRASTRUCTURE_FAILURE`` for workspace/store IO failures, or
                ``BUILD_FAILURE`` for compiler/validation failures.
        """
        ...


class Installer(Protocol):
    """Install port keyed on System and Run ids."""

    def install(
        self,
        system_id: UUID,
        run_id: UUID,
        kernel_ref: str,
        *,
        cmdline: str,
        method: CaptureMethod = CaptureMethod.HOST_DUMP,
        initrd_ref: str | None = None,
    ) -> None:
        """Install a built kernel into a System and confirm guest readiness.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid capture/install inputs,
                ``STALE_HANDLE`` for vanished artifact refs, ``INFRASTRUCTURE_FAILURE`` for
                store IO failures, ``INSTALL_FAILURE`` for provider install faults,
                ``READINESS_FAILURE`` for guest readiness command failures, or
                ``BOOT_TIMEOUT`` when the guest never becomes ready.
        """
        ...


class Booter(Protocol):
    """Boot port: power-cycle the domain and confirm run-readiness."""

    def boot(self, system_id: UUID) -> None:
        """Boot a System after installation and confirm run-readiness.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` for provider boot faults,
                ``READINESS_FAILURE`` for guest readiness command failures, or
                ``BOOT_TIMEOUT`` when the guest never becomes ready.
        """
        ...


class Connector(Protocol):
    """Connect port for opening and closing debug transports."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        """Open a debug transport and return an opaque handle.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown transport kind,
                ``MISSING_DEPENDENCY`` for unavailable provider seams,
                ``TRANSPORT_FAILURE`` for tunnel allocation faults, or
                ``DEBUG_ATTACH_FAILURE`` when the endpoint cannot be attached.
        """
        ...

    def close_transport(self, handle: TransportHandle) -> None:
        """Close a previously opened transport handle.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed handles,
                ``MISSING_DEPENDENCY`` for unavailable provider seams, or
                ``TRANSPORT_FAILURE`` when teardown of the tunnel fails.
        """
        ...


class Controller(Protocol):
    """Control port keyed on provider domain name."""

    def power(self, domain_name: str, action: PowerAction) -> None:
        """Apply a power operation to a provider domain.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for absent domains or provider power
                faults.
        """
        ...

    def force_crash(self, domain_name: str) -> None:
        """Trigger a guest crash path for vmcore capture.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for absent domains or provider crash
                trigger faults.
        """
        ...


class Retriever(Protocol):
    """Capture port keyed on the System id."""

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture, store, and redact a vmcore.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for build-id provenance mismatch or
                invalid capture input, ``MISSING_DEPENDENCY`` for unavailable provider/store
                seams, or ``READINESS_FAILURE`` when no complete vmcore is available.
        """
        ...


class CrashPostmortem(Protocol):
    """Crash postmortem port for command batches over a captured vmcore."""

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Run a bounded crash-command batch over a captured vmcore.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed refs, command batches, or
                build-id mismatch, ``MISSING_DEPENDENCY`` for unavailable object-store or
                crash seams, or ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
        """
        ...


class VmcoreIntrospector(Protocol):
    """Offline introspection port for captured vmcores."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Inspect a captured vmcore through the provider's offline helper.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed refs or build-id
                mismatch, ``MISSING_DEPENDENCY`` for unavailable object-store or helper
                seams, or ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
        """
        ...


class LiveIntrospector(Protocol):
    """Live introspection port over an existing transport handle."""

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        """Inspect a live guest through an existing debug transport.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed handles or unknown
                helpers, ``MISSING_DEPENDENCY`` for unavailable helper seams, or
                ``DEBUG_ATTACH_FAILURE`` for live attach faults.
        """
        ...


class GdbMiEngine(Protocol):
    """Debug operation engine over a live gdb/MI attachment."""

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> GdbBreakpointRef:
        """Set a breakpoint through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid locations,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        """Clear a breakpoint through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid breakpoint numbers,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[GdbBreakpointRef]:
        """List breakpoints through gdb/MI.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        """Read guest memory through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid address/count values,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI read failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def read_registers(
        self, attachment: GdbMiAttachment, register_names: list[str]
    ) -> dict[str, object]:
        """Read selected registers through gdb/MI.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` for gdb/MI read failures or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def continue_(self, attachment: GdbMiAttachment, *, timeout_sec: float) -> GdbStopRecord:
        """Resume execution and return the next stop record, if any.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid timeout values,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def interrupt(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        """Interrupt execution and return the stop record when one is reported.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...


class GdbMiSessionRegistry:
    """In-process holder of live gdb/MI attachments keyed on ``session_id``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, GdbMiAttachment] = {}

    def register(self, session_id: str, attachment: GdbMiAttachment) -> None:
        with self._lock:
            self._sessions[session_id] = attachment

    def get(self, session_id: str) -> GdbMiAttachment | None:
        with self._lock:
            return self._sessions.get(session_id)

    def require(self, session_id: str) -> GdbMiAttachment:
        attachment = self.get(session_id)
        if attachment is None:
            raise CategorizedError(
                "no live gdb/MI session; the engine is gone (server restarted or session reaped)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"code": "no_live_session", "debug_session_id": session_id},
            )
        return attachment

    def reap(self, session_id: str) -> GdbMiAttachment | None:
        with self._lock:
            return self._sessions.pop(session_id, None)


class AttachSeam(Protocol):
    """Lazy attach seam returning a live gdb/MI attachment."""

    def __call__(
        self, *, host: str, port: int, run_id: str, transcript_path: Path
    ) -> GdbMiAttachment: ...


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)
