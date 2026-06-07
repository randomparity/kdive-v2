"""Handler-facing provider ports used by MCP tools and worker handlers.

Concrete providers satisfy these contracts structurally. Provider implementation modules may
import these types, but MCP and worker code should not import provider-specific contracts.
"""

from __future__ import annotations

import threading
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.interfaces import SystemHandle, TransportHandle
from kdive.store.objectstore import HeadResult, StoredArtifact

_TRANSPORT_KINDS = frozenset({"gdbstub", "ssh"})


class BuildOutput(NamedTuple):
    """A build result: object-store keys plus the kernel GNU build-id."""

    kernel_ref: str
    debuginfo_ref: str
    build_id: str


class ValidatedUpload(NamedTuple):
    """External build validation result plus per-object HEAD metadata."""

    output: BuildOutput
    heads: dict[str, HeadResult]


class CaptureOutput(NamedTuple):
    """A capture result: raw/redacted artifacts plus the vmcore GNU build-id."""

    raw: StoredArtifact
    redacted: StoredArtifact
    vmcore_build_id: str


class CrashResult(NamedTuple):
    """A raw `crash` subprocess result: exit status and captured streams."""

    exit_status: int
    stdout: bytes
    stderr: bytes


class CrashOutput(NamedTuple):
    """A parsed, redacted crash batch result."""

    results: dict[str, object]
    transcript: str
    truncated: bool


class IntrospectOutput(NamedTuple):
    """A redacted, size-bounded introspection report."""

    tasks: dict[str, object]
    modules: dict[str, object]
    sysinfo: dict[str, object]
    truncated: bool


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


class PowerAction(StrEnum):
    """Power operations accepted by the control port."""

    ON = "on"
    OFF = "off"
    CYCLE = "cycle"
    RESET = "reset"


class Provisioner(Protocol):
    """Provisioning port keyed on the already-minted System id."""

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str: ...
    def teardown(self, domain_name: str) -> None: ...
    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str: ...


class Builder(Protocol):
    """Build port returning stored kernel and debuginfo refs."""

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput: ...


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
    ) -> None: ...


class Booter(Protocol):
    """Boot port: power-cycle the domain and confirm run-readiness."""

    def boot(self, system_id: UUID) -> None: ...


class Connector(Protocol):
    """Connect port for opening and closing debug transports."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle: ...
    def close_transport(self, handle: TransportHandle) -> None: ...


class Controller(Protocol):
    """Control port keyed on provider domain name."""

    def power(self, domain_name: str, action: PowerAction) -> None: ...
    def force_crash(self, domain_name: str) -> None: ...


class Retriever(Protocol):
    """Capture port keyed on the System id."""

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput: ...


class CrashPostmortem(Protocol):
    """Crash postmortem port for command batches over a captured vmcore."""

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput: ...


class VmcoreIntrospector(Protocol):
    """Offline introspection port for captured vmcores."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput: ...


class LiveIntrospector(Protocol):
    """Live introspection port over an existing transport handle."""

    def introspect_live(self, *, transport_handle: str) -> IntrospectOutput: ...


class GdbMiAttachment(Protocol):
    """Live gdb/MI attachment used by Debug-plane operations."""

    controller: Any


class GdbMiEngine(Protocol):
    """Debug operation engine over a live gdb/MI attachment."""

    def set_breakpoint(self, attachment: Any, location: str) -> Any: ...
    def clear_breakpoint(self, attachment: Any, number: str) -> None: ...
    def list_breakpoints(self, attachment: Any) -> list[Any]: ...
    def read_memory(self, attachment: Any, *, address: int, byte_count: int) -> bytes: ...
    def read_registers(self, attachment: Any, register_names: list[str]) -> dict[str, object]: ...
    def continue_(self, attachment: Any, *, timeout_sec: float) -> Any: ...
    def interrupt(self, attachment: Any) -> Any: ...


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
