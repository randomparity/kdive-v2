"""Debug provider contracts and gdb/MI records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from kdive.providers.ports._common import ProviderModel


class GdbFrame(ProviderModel):
    """One stack frame from a gdb/MI stop record."""

    level: int | None = None
    func: str | None = None
    addr: str | None = None
    file: str | None = None
    line: int | None = None


class GdbStopRecord(ProviderModel):
    """A parsed gdb/MI stop record."""

    reason: str | None = None
    bkptno: str | None = None
    stopped_thread: str | None = None
    frame: GdbFrame | None = None
    timed_out: bool = False


class GdbBreakpointRef(ProviderModel):
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
            CategorizedError: ``CONFIGURATION_ERROR`` for empty or invalid register names,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI read failures, or ``INFRASTRUCTURE_FAILURE``
                for command timeouts.
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


class AttachSeam(Protocol):
    """Lazy attach seam returning a live gdb/MI attachment."""

    def __call__(
        self, *, host: str, port: int, run_id: str, transcript_path: Path
    ) -> GdbMiAttachment: ...
