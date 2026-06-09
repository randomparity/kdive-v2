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

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        """Write one gdb/MI command and return records emitted before the prompt.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the prompt does not arrive
                before ``timeout_sec``. Callers that interpret error records should surface
                gdb/MI command failures as ``DEBUG_ATTACH_FAILURE``.
        """
        ...

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        """Read pending gdb/MI records without sending a command.

        A read timeout is non-fatal and returns an empty list; this lets polling callers
        distinguish "no records yet" from a command timeout.
        """
        ...

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        """Read records until the prompt or timeout.

        Args:
            timeout_sec: Maximum wait for a prompt.
            raise_error_on_timeout: If true, timeout raises; if false, timeout returns
                an empty list.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when ``raise_error_on_timeout``
                is true and no prompt arrives before ``timeout_sec``.
        """
        ...

    def exit(self) -> None:
        """Terminate the underlying gdb/MI controller.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` if the controller cannot be
                terminated cleanly.
        """
        ...


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
        """Resume execution and return a stop record.

        If the requested wait times out, the provider interrupts execution and returns the
        resulting stop with ``timed_out=True``. If no stop arrives after the interrupt, the
        provider raises instead of returning ``None``.

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
