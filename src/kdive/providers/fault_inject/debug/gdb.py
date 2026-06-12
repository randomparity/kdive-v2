"""Fault-inject gdb/MI debug ports."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from kdive.providers.ports import (
    GdbBreakpointRef,
    GdbMiAttachment,
    GdbStopRecord,
)


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
        self._breakpoints: dict[Path, dict[str, GdbBreakpointRef]] = {}
        self._next = 1
        self._lock = Lock()

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> GdbBreakpointRef:
        with self._lock:
            number = str(self._next)
            self._next += 1
            ref = GdbBreakpointRef(number=number, type="breakpoint", func=location, enabled=True)
            self._breakpoints.setdefault(attachment.transcript_path, {})[number] = ref
            return ref

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        with self._lock:
            bucket = self._breakpoints.get(attachment.transcript_path)
            if bucket is None:
                return
            bucket.pop(number, None)
            if not bucket:
                self._breakpoints.pop(attachment.transcript_path, None)

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[GdbBreakpointRef]:
        with self._lock:
            return list(self._breakpoints.get(attachment.transcript_path, {}).values())

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
