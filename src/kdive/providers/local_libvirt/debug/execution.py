"""Interactive execution helpers for the local-libvirt gdb/MI provider."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.debug.mi_protocol import MiRecord
from kdive.providers.ports import GdbMiAttachment, GdbStopRecord

MAX_INTERACTIVE_WAIT_SEC = 60
STOP_POLL_SLICE_SEC = 0.5
INTERRUPT_STOP_TIMEOUT_SEC = 10.0


class GdbMiEngineHandle(Protocol):
    """Engine surface used by execution control without importing the engine class."""

    def records_from(self, raw: list[dict[str, object]]) -> list[MiRecord]: ...

    def append_transcript(
        self, transcript_path: Path, command: str, records: list[MiRecord]
    ) -> None: ...

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def stop_record_from(self, record: MiRecord) -> GdbStopRecord: ...

    def redact_stop(self, stop: GdbStopRecord) -> GdbStopRecord: ...


class ExecutionControl:
    """Resume/wait/interrupt machinery for the interactive ops."""

    def __init__(self, engine: GdbMiEngineHandle, *, command_timeout_sec: float) -> None:
        self._engine = engine
        self._command_timeout_sec = command_timeout_sec

    def wait_for_stop(
        self, attachment: GdbMiAttachment, *, timeout_sec: float
    ) -> GdbStopRecord | None:
        slices = max(1, int(timeout_sec / STOP_POLL_SLICE_SEC) + 1)
        for _ in range(slices):
            records = self._engine.records_from(
                attachment.controller.read(timeout_sec=STOP_POLL_SLICE_SEC)
            )
            attachment.records.extend(records)
            if records:
                self._engine.append_transcript(attachment.transcript_path, "<read>", records)
            stop = next((record for record in records if record.message == "stopped"), None)
            if stop is not None:
                return self._engine.stop_record_from(stop)
        return None

    def interrupt(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        raw = attachment.controller.write("-exec-interrupt", timeout_sec=self._command_timeout_sec)
        records = self._engine.records_from(raw)
        attachment.records.extend(records)
        self._engine.append_transcript(attachment.transcript_path, "-exec-interrupt", records)
        stop = self.wait_for_stop(attachment, timeout_sec=INTERRUPT_STOP_TIMEOUT_SEC)
        return self._engine.redact_stop(stop) if stop is not None else None

    def resume(
        self, attachment: GdbMiAttachment, verb: str, *, timeout_sec: float
    ) -> GdbStopRecord:
        if timeout_sec < 0 or not math.isfinite(timeout_sec):
            raise CategorizedError(
                "gdb/MI continue timeout must be a finite non-negative number",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"code": "bad_continue_timeout", "timeout_sec": timeout_sec},
            )
        requested = math.ceil(timeout_sec) if timeout_sec else MAX_INTERACTIVE_WAIT_SEC
        bounded = max(1, min(requested, MAX_INTERACTIVE_WAIT_SEC))
        self._engine.execute_mi_command(attachment, verb)
        stop = self.wait_for_stop(attachment, timeout_sec=bounded)
        if stop is not None:
            return self._engine.redact_stop(stop)
        interrupted = self.interrupt(attachment)
        if interrupted is None:
            raise CategorizedError(
                "gdb/MI RSP went silent: interrupt issued but no *stopped arrived; link stalled",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"code": "transport_stall", "verb": verb},
            )
        return self._engine.redact_stop(interrupted.model_copy(update={"timed_out": True}))
