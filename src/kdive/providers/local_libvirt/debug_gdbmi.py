"""The gdb-MI tier: a persistent ``gdb --interpreter=mi3`` engine over the gdbstub (ADR-0034).

Ported (trimmed) from v1 ``providers/local/debug/gdb_mi.py`` to the seven Debug-plane ops
issue #21 scopes: breakpoints (set/clear/list), ``read_registers``, ``read_memory`` (4096-byte
cap, **bytes verbatim**), ``continue_``, ``interrupt``. The attach/symbol-resolution/module-
loading/stack/watchpoint/evaluate surface is intentionally **not** ported (out of #21 scope).

All **textual** MI transcript/record output passes through the :class:`Redactor` before it is
persisted to the per-session transcript or returned in a response. The exception is
``read_memory``: the raw guest bytes are returned **verbatim** under the 4096 cap — the
redactor masks text/structure, and masking opaque binary memory would corrupt the requested
dump (ADR-0034 decision 3).

The ``MiController`` subprocess seam is injectable: the real :class:`PygdbmiController` drives
a ``gdb`` child via pygdbmi (``live_vm``-only); tests inject a scripted fake. The live
:class:`GdbMiAttachment` objects are held in an in-process registry keyed on ``session_id`` —
server-process-scoped and non-durable (v1 ADR-0021): a restart strands the
attachment and the next op gets ``no_live_session``.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import math
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from pygdbmi.constants import GdbTimeoutError
from pygdbmi.gdbmiparser import parse_response

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.redaction import Redactor

MAX_MEMORY_READ_BYTES = 4096
MAX_INTERACTIVE_WAIT_SEC = 60

# Per-command MI write timeout. 10s bounds a healthy localhost RSP connect/read. The resume
# path uses ASYNC continue (mi-async on), so `-exec-continue` returns `^running` immediately
# rather than blocking until a stop a free-running kernel never produces.
_MI_COMMAND_TIMEOUT_SEC = 10.0
# gdb's RSP read timeout (`set remotetimeout`): generous-but-finite so a slow/silent stub
# yields a clean gdb-reported disconnect rather than an opaque hang.
RSP_REMOTE_TIMEOUT_SEC = 30
# Bounded retry for the RSP connect (`-target-select remote`): the connect is idempotent until
# `^connected`, so retrying any connect error a fixed small number of times is sound.
_CONNECT_RETRY_COUNT = 3
_CONNECT_RETRY_BACKOFF_SEC = 0.5
# Fixed bound for the post-timeout `-exec-interrupt` to land its `*stopped` (SIGINT).
_INTERRUPT_STOP_TIMEOUT_SEC = 10.0
# Poll slice when looping read() toward the deadline.
_STOP_POLL_SLICE_SEC = 0.5

# The literal MI prompt terminator gdb emits between command results; not a record.
_MI_PROMPT = "(gdb)"
# Keys pygdbmi may emit on a parsed record; whitelist so an unexpected extra key is dropped
# rather than tripping the extra="forbid" model boundary.
_KNOWN_KEYS = ("type", "message", "payload", "token", "stream")

# A bare C identifier. The name-shape gate keeps a breakpoint location an address-of-a-name,
# never an arbitrary expression — so `-break-insert` is non-injectable.
_SYMBOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A register name (passed to -data-list-register-names lookup).
_REGISTER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# A breakpoint location: a bare C identifier (function/symbol).
_BREAK_LOCATION_RE = _SYMBOL_NAME_RE
# A gdb breakpoint id is a bare integer.
_BREAK_ID_RE = re.compile(r"^[0-9]+$")
# gdb stop reasons meaning the inferior is gone (not a debuggable HALT).
_TERMINAL_STOP_REASONS = frozenset({"exited", "exited-normally", "exited-signalled"})


class _MiModel(BaseModel):
    """Frozen wire shape for parsed gdb/MI records (``extra="forbid"``)."""

    model_config = ConfigDict(extra="forbid")


class MiRecord(_MiModel):
    """One parsed gdb/MI record (gdb manual "GDB/MI Output Syntax").

    ``type`` is the MI record class (``result``/``notify``/``exec``/``console``/``log``/
    ``output``/``target``); ``message`` is the result class (``done``/``running``/
    ``connected``/``error``/``exit``) or async class; ``payload`` is the parsed value tree.
    """

    type: str
    message: str | None = None
    payload: dict[str, Any] | list[Any] | str | None = None
    token: int | None = None
    stream: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MiRecord:
        return cls(**{key: raw[key] for key in _KNOWN_KEYS if key in raw})

    @staticmethod
    def first_result(records: list[MiRecord]) -> MiRecord | None:
        """The first ``result``-class record (``^done``/``^running``/``^error``/...), or None."""
        return next((record for record in records if record.type == "result"), None)


class Frame(_MiModel):
    """One stack frame from a gdb/MI ``frame={...}`` payload (optional fields gdb may omit)."""

    level: int | None = None
    func: str | None = None
    addr: str | None = None
    file: str | None = None
    line: int | None = None


class StopRecord(_MiModel):
    """A parsed ``*stopped`` async record.

    ``reason`` is gdb's stop reason (``breakpoint-hit``, ``end-stepping-range``, ``exited``,
    ...); ``frame`` is the stop frame. ``timed_out`` is True when the wait expired and the
    handler had to ``-exec-interrupt``.
    """

    reason: str | None = None
    bkptno: str | None = None
    stopped_thread: str | None = None
    frame: Frame | None = None
    timed_out: bool = False


class BreakpointRef(_MiModel):
    """One breakpoint from ``-break-insert``/``-break-list``; ``number`` is gdb's bp id."""

    number: str
    type: str | None = None
    addr: str | None = None
    func: str | None = None
    what: str | None = None
    enabled: bool | None = None


def parse_mi_records(text: str) -> list[MiRecord]:
    """Parse newline-delimited MI output into typed records.

    Skips blank lines and the literal ``(gdb)`` prompt terminator. Used both for the
    controller's returned dicts and for raw transcript text in tests.
    """
    records: list[MiRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == _MI_PROMPT:
            continue
        records.append(MiRecord.from_raw(parse_response(stripped)))
    return records


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


def _timeout_error(command: str, timeout_sec: float) -> CategorizedError:
    """The error an MI write timeout raises.

    Tagged ``transport_stall`` / INFRASTRUCTURE_FAILURE: a timeout through the per-op path
    (post-``^connected`` by construction) means the RSP link stalled.
    """
    return CategorizedError(
        f"gdb/MI command timed out after {timeout_sec}s: {command}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"code": "transport_stall", "command": command, "timeout_seconds": timeout_sec},
    )


@runtime_checkable
class MiController(Protocol):
    """The injectable subprocess seam.

    The real impl drives a ``gdb --interpreter=mi3`` child via pygdbmi; tests inject a scripted
    fake. ``write`` returns the raw pygdbmi record dicts for the command; ``read`` polls for
    further out-of-band records (the async ``*stopped`` after a ``^running``); ``exit``
    terminates the child.
    """

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]: ...

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]: ...

    def exit(self) -> None: ...


class PygdbmiController:  # pragma: no cover - live_vm
    """Real ``MiController``: a managed ``gdb --interpreter=mi3`` subprocess via ``pygdbmi``."""

    def __init__(self, command: list[str]) -> None:
        from pygdbmi.gdbcontroller import GdbController

        self._controller = GdbController(command=command)

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        try:
            return self._controller.write(
                command, timeout_sec=timeout_sec, raise_error_on_timeout=True
            )
        except GdbTimeoutError as exc:
            raise _timeout_error(command, timeout_sec) from exc

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        try:
            return self._controller.get_gdb_response(
                timeout_sec=timeout_sec, raise_error_on_timeout=False
            )
        except GdbTimeoutError:
            return []

    def exit(self) -> None:
        self._controller.exit()


@dataclass
class GdbMiAttachment:
    """A live attach: the controller, its RSP endpoint, transcript path, and records so far."""

    controller: MiController
    rsp_host: str
    rsp_port: int
    transcript_path: Path
    records: list[MiRecord] = field(default_factory=list)


class _ExecutionControl:
    """Resume/wait/interrupt machinery for the interactive ops."""

    def __init__(self, engine: GdbMiEngine) -> None:
        self._engine = engine

    def wait_for_stop(
        self, attachment: GdbMiAttachment, *, timeout_sec: float
    ) -> StopRecord | None:
        slices = max(1, int(timeout_sec / _STOP_POLL_SLICE_SEC) + 1)
        for _ in range(slices):
            records = self._engine.records_from(
                attachment.controller.read(timeout_sec=_STOP_POLL_SLICE_SEC)
            )
            attachment.records.extend(records)
            if records:
                self._engine.append_transcript(attachment.transcript_path, "<read>", records)
            stop = next((record for record in records if record.message == "stopped"), None)
            if stop is not None:
                return self._engine.stop_record_from(stop)
        return None

    def interrupt(self, attachment: GdbMiAttachment) -> StopRecord | None:
        raw = attachment.controller.write("-exec-interrupt", timeout_sec=_MI_COMMAND_TIMEOUT_SEC)
        records = self._engine.records_from(raw)
        attachment.records.extend(records)
        self._engine.append_transcript(attachment.transcript_path, "-exec-interrupt", records)
        stop = self.wait_for_stop(attachment, timeout_sec=_INTERRUPT_STOP_TIMEOUT_SEC)
        return self._redact_stop(stop) if stop is not None else None

    def resume(self, attachment: GdbMiAttachment, verb: str, *, timeout_sec: float) -> StopRecord:
        # Round fractional requests up: a sub-second request still waits its full span (and the
        # floor of 1s below), never truncating toward zero (5.7 -> 6, not 5).
        requested = math.ceil(timeout_sec) if timeout_sec else MAX_INTERACTIVE_WAIT_SEC
        bounded = max(1, min(requested, MAX_INTERACTIVE_WAIT_SEC))
        self._engine.run(attachment, verb)  # ^running under mi-async on
        stop = self.wait_for_stop(attachment, timeout_sec=bounded)
        if stop is not None:
            return self._redact_stop(stop)
        # The wait timed out. Fall back to -exec-interrupt: a reachable kernel cannot ignore a
        # delivered SIGINT, so if the interrupt is accepted but no *stopped arrives, the link is
        # dead (silence-path stall) — distinct from a benign no-breakpoint timeout where the
        # SIGINT stop does arrive.
        interrupted = self.interrupt(attachment)
        if interrupted is None:
            raise CategorizedError(
                "gdb/MI RSP went silent: interrupt issued but no *stopped arrived; link stalled",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"code": "transport_stall", "verb": verb},
            )
        return self._redact_stop(interrupted.model_copy(update={"timed_out": True}))

    def _redact_stop(self, stop: StopRecord) -> StopRecord:
        return StopRecord.model_validate(
            self._engine.redactor.redact_value(stop.model_dump(mode="json"))
        )


class GdbMiEngine:
    """Persistent ``gdb --interpreter=mi3`` engine for the seven Debug-plane ops (ADR-0034)."""

    def __init__(
        self,
        *,
        controller_factory: Callable[[list[str]], MiController] | None = None,
        gdb_path_finder: Callable[[str], str | None] = shutil.which,
        redactor: Redactor | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._controller_factory = controller_factory or (
            lambda command: PygdbmiController(command)
        )
        self._gdb_path_finder = gdb_path_finder
        self.redactor = redactor or Redactor()
        self._sleep = sleep
        self._execution = _ExecutionControl(self)

    # --- attach (live_vm) -----------------------------------------------------------------

    def attach(  # pragma: no cover - live_vm
        self, *, host: str, port: int, vmlinux_path: Path, transcript_path: Path
    ) -> GdbMiAttachment:
        """Spawn gdb, load symbols, and connect RSP. Live-only; tests inject a fake attachment."""
        self._validate_rsp_host(host)
        gdb_path = self._gdb_path_finder("gdb")
        if gdb_path is None:
            raise CategorizedError(
                "missing required gdb tool",
                category=ErrorCategory.MISSING_DEPENDENCY,
                details={"missing_tools": ["gdb"]},
            )
        resolved_vmlinux = vmlinux_path.expanduser().resolve()
        if not resolved_vmlinux.is_file():
            raise _config_error(
                "vmlinux symbol file does not exist",
                code="bad_vmlinux_path",
                details={"vmlinux_path": str(vmlinux_path)},
            )
        controller = self._controller_factory([gdb_path, "--nx", "--quiet", "--interpreter=mi3"])
        attachment = GdbMiAttachment(
            controller=controller, rsp_host=host, rsp_port=port, transcript_path=transcript_path
        )
        try:
            self.run(attachment, "-gdb-set confirm off")
            self.run(attachment, "-gdb-set pagination off")
            self.run(attachment, "-gdb-set mi-async on")
            self.run(attachment, f"-file-exec-and-symbols {self._mi_path(resolved_vmlinux)}")
            self.run(attachment, f"-gdb-set remotetimeout {RSP_REMOTE_TIMEOUT_SEC}")
            self._connect_with_retry(attachment, host, port)
        except CategorizedError as exc:
            with contextlib.suppress(Exception):
                controller.exit()
            raise self._as_attach_failure(exc) from exc
        return attachment

    def _connect_with_retry(
        self, attachment: GdbMiAttachment, host: str, port: int
    ) -> None:  # pragma: no cover - live_vm
        command = f"-target-select remote {host}:{port}"
        last_exc: CategorizedError | None = None
        for attempt in range(_CONNECT_RETRY_COUNT):
            try:
                self.run(attachment, command)
                return
            except CategorizedError as exc:
                last_exc = self._as_attach_failure(exc)
                if attempt + 1 < _CONNECT_RETRY_COUNT:
                    self._sleep(_CONNECT_RETRY_BACKOFF_SEC)
        raise (
            last_exc
            if last_exc is not None
            else CategorizedError(
                "gdb/MI RSP connect failed", category=ErrorCategory.DEBUG_ATTACH_FAILURE
            )
        )

    def _as_attach_failure(
        self, exc: CategorizedError
    ) -> CategorizedError:  # pragma: no cover - live_vm
        if (
            exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE
            and exc.details.get("code") != "transport_stall"
        ):
            return exc
        details = {key: value for key, value in exc.details.items() if key != "code"}
        return CategorizedError(
            str(exc), category=ErrorCategory.DEBUG_ATTACH_FAILURE, details=details
        )

    # --- breakpoints ----------------------------------------------------------------------

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> BreakpointRef:
        if not _BREAK_LOCATION_RE.match(location):
            raise _config_error(
                f"breakpoint location must be a bare C identifier, got {location!r}",
                code="bad_location",
                details={"location": location},
            )
        # Hardware breakpoint (-h): a software breakpoint's 0xCC write does not survive a frozen
        # boot's reset-vector insertion and can fail on read-only kernel .text.
        return self._breakpoint_ref(
            self.run(attachment, f"-break-insert -h {location}"), key="bkpt"
        )

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        if not _BREAK_ID_RE.match(number):
            raise _config_error(
                f"breakpoint id must be numeric, got {number!r}",
                code="bad_breakpoint_id",
                details={"number": number},
            )
        self.run(attachment, f"-break-delete {number}")

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[BreakpointRef]:
        records = self.run(attachment, "-break-list")
        result = MiRecord.first_result(records)
        payload = result.payload if result is not None and isinstance(result.payload, dict) else {}
        table = (
            payload.get("BreakpointTable")
            if isinstance(payload.get("BreakpointTable"), dict)
            else {}
        )
        body = table.get("body") if isinstance(table, dict) else None
        rows = body if isinstance(body, list) else []
        refs: list[BreakpointRef] = []
        for row in rows:
            entry = row.get("bkpt") if isinstance(row, dict) else None
            if isinstance(entry, dict):
                refs.append(self._breakpoint_ref_from(entry))
        return refs

    def _breakpoint_ref(self, records: list[MiRecord], *, key: str) -> BreakpointRef:
        result = MiRecord.first_result(records)
        payload = result.payload if result is not None and isinstance(result.payload, dict) else {}
        entry = payload.get(key)
        if not isinstance(entry, dict):
            raise CategorizedError(
                f"gdb/MI {key} response had no breakpoint record",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"command_key": key},
            )
        return self._breakpoint_ref_from(entry)

    def _breakpoint_ref_from(self, entry: dict[str, Any]) -> BreakpointRef:
        return BreakpointRef.model_validate(
            self.redactor.redact_value(
                {
                    "number": str(entry.get("number")),
                    "type": entry.get("type") if isinstance(entry.get("type"), str) else None,
                    "addr": entry.get("addr") if isinstance(entry.get("addr"), str) else None,
                    "func": entry.get("func") if isinstance(entry.get("func"), str) else None,
                    "what": entry.get("what") if isinstance(entry.get("what"), str) else None,
                }
            )
        )

    # --- registers / memory ---------------------------------------------------------------

    def read_registers(
        self, attachment: GdbMiAttachment, register_names: list[str]
    ) -> dict[str, object]:
        if not isinstance(register_names, list) or not register_names:
            raise _config_error("registers must be a non-empty list", code="bad_register")
        requested: list[str] = []
        for name in register_names:
            if not isinstance(name, str) or not _REGISTER_RE.match(name):
                raise _config_error(f"invalid register name {name!r}", code="bad_register")
            requested.append(name)
        # gdb keys register VALUES by ordinal number; map names->ordinals via
        # -data-list-register-names, then return only the requested names.
        names_result = MiRecord.first_result(self.run(attachment, "-data-list-register-names"))
        names_payload = names_result.payload if names_result is not None else None
        ordered = names_payload.get("register-names") if isinstance(names_payload, dict) else None
        ordered_names = ordered if isinstance(ordered, list) else []
        values_result = MiRecord.first_result(self.run(attachment, "-data-list-register-values x"))
        values_payload = values_result.payload if values_result is not None else None
        rows = values_payload.get("register-values") if isinstance(values_payload, dict) else None
        by_number = {
            row.get("number"): row.get("value")
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
        }
        registers: dict[str, object] = {}
        for name in requested:
            if name in ordered_names:
                ordinal = str(ordered_names.index(name))
                if ordinal in by_number:
                    registers[name] = by_number[ordinal]
        return self.redactor.redact_value({"registers": registers})

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        """Read ``byte_count`` bytes from ``address``, returned **verbatim** (not redacted).

        Enforces the ported 4096-byte cap and a 64-bit address range. The gdb/MI
        ``-data-read-memory-bytes`` ``memory=[{contents:...}]`` segments are hex-decoded and
        concatenated; the raw bytes are returned unmasked (ADR-0034 decision 3). The transcript
        line for the command is still redacted (it is text).
        """
        if not isinstance(address, int) or not isinstance(byte_count, int):
            raise _config_error("address and byte_count must be integers", code="bad_read_range")
        if address < 0 or address > 0xFFFFFFFFFFFFFFFF:
            raise _config_error(
                "address out of range", code="bad_read_range", details={"address": address}
            )
        if byte_count < 1 or byte_count > MAX_MEMORY_READ_BYTES:
            raise _config_error(
                f"byte_count must be between 1 and {MAX_MEMORY_READ_BYTES}",
                code="bad_read_range",
                details={"byte_count": byte_count},
            )
        records = self.run(attachment, f"-data-read-memory-bytes 0x{address:x} {byte_count}")
        result = MiRecord.first_result(records)
        payload = result.payload if result is not None and isinstance(result.payload, dict) else {}
        memory = payload.get("memory")
        segments = memory if isinstance(memory, list) else []
        try:
            blob = b"".join(
                bytes.fromhex(str(seg.get("contents", "")))
                for seg in segments
                if isinstance(seg, dict)
            )
        except ValueError as exc:
            # A non-hex / odd-length `contents` is a malformed stub reply, not a verbatim dump;
            # surface it as an attach-level failure rather than letting ValueError escape uncaught.
            raise CategorizedError(
                "gdb/MI returned non-hex memory contents",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "bad_memory_contents"},
            ) from exc
        return blob[:byte_count]

    # --- interactive execution ------------------------------------------------------------

    def continue_(self, attachment: GdbMiAttachment, *, timeout_sec: float) -> StopRecord:
        """Resume, wait for the stop, return a redacted StopRecord (interrupt back on timeout)."""
        return self._execution.resume(attachment, "-exec-continue", timeout_sec=timeout_sec)

    def interrupt(self, attachment: GdbMiAttachment) -> StopRecord | None:
        """Idempotent 'ensure HALTED': -exec-interrupt then wait the short fixed bound."""
        return self._execution.interrupt(attachment)

    def wait_for_stop(
        self, attachment: GdbMiAttachment, *, timeout_sec: float
    ) -> StopRecord | None:
        return self._execution.wait_for_stop(attachment, timeout_sec=timeout_sec)

    # --- record helpers (public to _ExecutionControl) -------------------------------------

    def stop_record_from(self, record: MiRecord) -> StopRecord:
        payload = record.payload if isinstance(record.payload, dict) else {}
        reason = payload.get("reason")
        if isinstance(reason, str) and reason in _TERMINAL_STOP_REASONS:
            raise CategorizedError(
                f"gdb/MI inferior exited ({reason}); the debug session is dead",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "session_exited", "reason": reason},
            )
        frame_payload = payload.get("frame")
        frame = self._frame_from(frame_payload) if isinstance(frame_payload, dict) else None
        thread = payload.get("stopped-threads")
        return StopRecord(
            reason=reason if isinstance(reason, str) else None,
            bkptno=payload.get("bkptno") if isinstance(payload.get("bkptno"), str) else None,
            stopped_thread=thread if isinstance(thread, str) else None,
            frame=frame,
        )

    def _frame_from(self, payload: dict[str, Any]) -> Frame:
        def _int(value: object) -> int | None:
            return int(value) if isinstance(value, str) and value.lstrip("-").isdigit() else None

        return Frame(
            level=_int(payload.get("level")),
            func=payload.get("func") if isinstance(payload.get("func"), str) else None,
            addr=payload.get("addr") if isinstance(payload.get("addr"), str) else None,
            file=payload.get("file") if isinstance(payload.get("file"), str) else None,
            line=_int(payload.get("line")),
        )

    def run(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        """Write one MI command, accumulate + transcribe its records, raise on ``^error``."""
        records = self.records_from(
            attachment.controller.write(command, timeout_sec=_MI_COMMAND_TIMEOUT_SEC)
        )
        attachment.records.extend(records)
        self.append_transcript(attachment.transcript_path, command, records)
        result = MiRecord.first_result(records)
        if result is not None and result.message == "error":
            raise CategorizedError(
                f"gdb/MI command failed: {command}",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"command": command, "payload": self.redactor.redact_value(result.payload)},
            )
        return records

    def records_from(self, raw: list[dict[str, object]]) -> list[MiRecord]:
        return [MiRecord.from_raw(item) for item in raw]

    def _validate_rsp_host(self, host: str) -> None:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise _config_error(
                f"gdb/MI RSP host must be a loopback IP literal, got {host!r}", code="bad_rsp_host"
            )

    def _mi_path(self, path: Path) -> str:  # pragma: no cover - live_vm
        text = str(path)
        if any(char in text for char in "\t\r\n"):
            raise _config_error(
                "vmlinux path must not contain control whitespace", code="bad_vmlinux_path"
            )
        return text.replace("\\", "\\\\").replace(" ", "\\ ")

    def append_transcript(
        self, transcript_path: Path, command: str, records: list[MiRecord]
    ) -> None:
        """Append one redacted JSON-lines record per MI command to the session transcript."""
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "observed_at": datetime.now(UTC).isoformat(),
            "command": command,
            "records": [record.model_dump(mode="json") for record in records],
        }
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.redactor.redact_value(entry), default=str))
            handle.write("\n")


def _resolve_debuginfo_ref(run_id: str) -> str:  # pragma: no cover - live_vm
    """Resolve the Run's debuginfo (vmlinux) object key, mirroring the Retrieve plane's lookup.

    Raises ``MISSING_DEPENDENCY`` in M0 (no live host); the handler re-tags it
    ``DEBUG_ATTACH_FAILURE`` so a Debug-plane op without a reachable host fails as an attach
    failure rather than leaking the gate seam.
    """
    raise CategorizedError(
        "resolving a Run's debuginfo object runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"run_id": run_id},
    )


def default_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:  # pragma: no cover - live_vm
    """The real ``live_vm`` attach: resolve+materialize debuginfo, spawn gdb, connect RSP."""
    import tempfile

    debuginfo_ref = _resolve_debuginfo_ref(run_id)
    del debuginfo_ref  # the live path fetches it to a temp file before attach
    vmlinux_path = Path(tempfile.gettempdir()) / f"kdive-debuginfo-{run_id}"
    return GdbMiEngine().attach(
        host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
    )
