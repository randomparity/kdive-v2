"""gdb-MI engine tests — every op driven against a scripted fake `MiController` (no gdb).

The engine surface ported for issue #21 (breakpoints, read_registers, read_memory cap +
bytes-verbatim, continue/interrupt) is exercised directly; the real `PygdbmiController` and
the `attach()` subprocess path are `live_vm`-gated and not unit-tested here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools.debug.session_registry import GdbMiSessionRegistry
from kdive.providers.debug_common import gdbmi
from kdive.providers.debug_common.execution import ExecutionControl
from kdive.providers.debug_common.gdbmi import (
    MAX_MEMORY_READ_BYTES,
    GdbMiEngine,
    MiRecord,
    PygdbmiController,
    parse_mi_records,
)
from kdive.providers.debug_common.transcript import append_transcript
from kdive.providers.local_libvirt.debug import debug_gdbmi
from kdive.providers.ports import GdbMiAttachment, GdbStopRecord
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry


class _FakeMiController:
    """Maps each MI command to a canned list of pygdbmi record dicts; scripts async reads."""

    def __init__(
        self,
        *,
        responses: dict[str, list[dict[str, object]]] | None = None,
        reads: list[list[dict[str, object]]] | None = None,
        response_timeout: bool = False,
    ) -> None:
        self._responses = responses or {}
        self._reads = list(reads or [])
        self._response_timeout = response_timeout
        self.written: list[str] = []
        self.read_timeouts: list[float] = []
        self.exited = False

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        del timeout_sec
        self.written.append(command)
        return self._responses.get(
            command, [{"type": "result", "message": "done", "payload": None}]
        )

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        self.read_timeouts.append(timeout_sec)
        return self._reads.pop(0) if self._reads else []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        if self._response_timeout and raise_error_on_timeout:
            raise gdbmi._timeout_error("get_gdb_response", timeout_sec)
        return []

    def exit(self) -> None:
        self.exited = True


class _TimeoutGdbController:
    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        from pygdbmi.constants import GdbTimeoutError

        del timeout_sec, raise_error_on_timeout
        raise GdbTimeoutError("timed out")

    def exit(self) -> None:
        pass


def _pygdbmi_controller(controller: object) -> PygdbmiController:
    wrapped = object.__new__(PygdbmiController)
    wrapped._controller = controller
    return wrapped


def _attachment(controller: _FakeMiController, tmp_path: Path) -> GdbMiAttachment:
    return GdbMiAttachment(
        controller=controller,
        rsp_host="127.0.0.1",
        rsp_port=1234,
        transcript_path=tmp_path / "transcript.jsonl",
    )


def _engine(redactor: Redactor | None = None) -> GdbMiEngine:
    return GdbMiEngine(redactor=redactor or Redactor())


class _ExecutionEngine:
    """Minimal engine fake for ExecutionControl's direct helper behavior."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.transcript_commands: list[str] = []

    def records_from(self, raw: list[dict[str, object]]) -> list[MiRecord]:
        return [MiRecord.from_raw(record) for record in raw]

    def append_transcript(
        self, transcript_path: Path, command: str, records: list[MiRecord]
    ) -> None:
        del transcript_path, records
        self.transcript_commands.append(command)

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        del attachment
        self.executed.append(command)
        return [MiRecord(type="result", message="running")]

    def stop_record_from(self, record: MiRecord) -> GdbStopRecord:
        payload = record.payload if isinstance(record.payload, dict) else {}
        reason = payload.get("reason")
        bkptno = payload.get("bkptno")
        return GdbStopRecord(
            reason=reason if isinstance(reason, str) else None,
            bkptno=bkptno if isinstance(bkptno, str) else None,
        )

    def redact_stop(self, stop: GdbStopRecord) -> GdbStopRecord:
        return stop


# --- parsing -------------------------------------------------------------------------------


def test_parse_mi_records_skips_blank_and_prompt() -> None:
    records = parse_mi_records("\n(gdb)\n^done\n")
    assert [r.type for r in records] == ["result"]


def test_mi_record_from_raw_whitelists_keys() -> None:
    record = MiRecord.from_raw({"type": "result", "message": "done", "extra": "dropped"})
    assert record.type == "result"
    assert record.message == "done"


def test_pygdbmi_response_timeout_raises_by_default() -> None:
    controller = _pygdbmi_controller(_TimeoutGdbController())

    with pytest.raises(CategorizedError) as exc:
        controller.get_gdb_response(timeout_sec=0.25)

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["code"] == "transport_stall"


def test_pygdbmi_response_timeout_can_return_empty_when_requested() -> None:
    controller = _pygdbmi_controller(_TimeoutGdbController())

    assert controller.get_gdb_response(timeout_sec=0.25, raise_error_on_timeout=False) == []


# --- breakpoints ---------------------------------------------------------------------------


def test_set_breakpoint_uses_hardware_insert_and_parses_ref(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-insert -h panic": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"bkpt": {"number": "1", "func": "panic"}},
                }
            ]
        }
    )
    ref = _engine().set_breakpoint(_attachment(controller, tmp_path), "panic")
    assert ref.number == "1"
    assert ref.func == "panic"
    assert "-break-insert -h panic" in controller.written


def test_set_breakpoint_rejects_non_identifier(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_breakpoint(_attachment(controller, tmp_path), "panic; rm -rf /")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_location"
    assert controller.written == []


def test_clear_breakpoint_requires_numeric_id(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().clear_breakpoint(_attachment(controller, tmp_path), "abc")
    assert exc.value.details["code"] == "bad_breakpoint_id"
    assert controller.written == []


def test_clear_breakpoint_deletes(tmp_path: Path) -> None:
    controller = _FakeMiController()
    _engine().clear_breakpoint(_attachment(controller, tmp_path), "3")
    assert "-break-delete 3" in controller.written


def test_list_breakpoints_parses_table_body(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-list": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "BreakpointTable": {
                            "body": [
                                {"bkpt": {"number": "1", "func": "panic"}},
                                {"bkpt": {"number": "2", "func": "oops"}},
                            ]
                        }
                    },
                }
            ]
        }
    )
    refs = _engine().list_breakpoints(_attachment(controller, tmp_path))
    assert [r.number for r in refs] == ["1", "2"]


# --- registers -----------------------------------------------------------------------------


def _register_controller() -> _FakeMiController:
    return _FakeMiController(
        responses={
            "-data-list-register-names": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"register-names": ["rax", "rbx", "rcx"]},
                }
            ],
            "-data-list-register-values x": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "register-values": [
                            {"number": "0", "value": "0xdead"},
                            {"number": "1", "value": "0xbeef"},
                            {"number": "2", "value": "0xcafe"},
                        ]
                    },
                }
            ],
        }
    )


def test_read_registers_maps_names_to_values(tmp_path: Path) -> None:
    result = _engine().read_registers(_attachment(_register_controller(), tmp_path), ["rax", "rcx"])
    assert result == {"rax": "0xdead", "rcx": "0xcafe"}


def test_read_registers_rejects_empty_list(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(_FakeMiController(), tmp_path), [])
    assert exc.value.details["code"] == "bad_register"


def test_read_registers_rejects_bad_name(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(_FakeMiController(), tmp_path), ["rax; drop"])
    assert exc.value.details["code"] == "bad_register"


def test_read_registers_rejects_empty_gdb_payload(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-list-register-names": [
                {"type": "result", "message": "done", "payload": {"register-names": ["rax"]}}
            ],
            "-data-list-register-values x": [
                {"type": "result", "message": "done", "payload": {"register-values": []}}
            ],
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(controller, tmp_path), ["rax"])
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {
        "code": "missing_registers",
        "requested": ["rax"],
        "missing": ["rax"],
    }


def test_read_registers_rejects_partial_gdb_payload(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-list-register-names": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"register-names": ["rax", "rbx", "rcx"]},
                }
            ],
            "-data-list-register-values x": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "register-values": [
                            {"number": "0", "value": "0xdead"},
                            {"number": "2", "value": "0xcafe"},
                        ]
                    },
                }
            ],
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(controller, tmp_path), ["rax", "rbx", "rcx"])
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "missing_registers"
    assert exc.value.details["requested"] == ["rax", "rbx", "rcx"]
    assert exc.value.details["missing"] == ["rbx"]


# --- read_memory: cap + bytes verbatim -----------------------------------------------------


def _memory_controller(address: int, byte_count: int, hex_contents: str) -> _FakeMiController:
    return _FakeMiController(
        responses={
            f"-data-read-memory-bytes 0x{address:x} {byte_count}": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"memory": [{"contents": hex_contents}]},
                }
            ]
        }
    )


def test_read_memory_returns_concatenated_bytes(tmp_path: Path) -> None:
    controller = _memory_controller(0x1000, 4, "deadbeef")
    blob = _engine().read_memory(_attachment(controller, tmp_path), address=0x1000, byte_count=4)
    assert blob == bytes.fromhex("deadbeef")


def test_read_memory_accepts_exactly_4096(tmp_path: Path) -> None:
    payload = "ab" * MAX_MEMORY_READ_BYTES
    controller = _memory_controller(0x2000, MAX_MEMORY_READ_BYTES, payload)
    blob = _engine().read_memory(
        _attachment(controller, tmp_path), address=0x2000, byte_count=MAX_MEMORY_READ_BYTES
    )
    assert len(blob) == MAX_MEMORY_READ_BYTES


def test_read_memory_rejects_over_4096_without_command(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(controller, tmp_path), address=0x3000, byte_count=MAX_MEMORY_READ_BYTES + 1
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_read_range"
    assert controller.written == []  # no MI command was issued


def test_read_memory_rejects_zero_byte_count(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(_FakeMiController(), tmp_path), address=0x10, byte_count=0
        )
    assert exc.value.details["code"] == "bad_read_range"


def test_read_memory_rejects_non_hex_contents(tmp_path: Path) -> None:
    controller = _memory_controller(0x6000, 4, "nothex!!")
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(_attachment(controller, tmp_path), address=0x6000, byte_count=4)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "bad_memory_contents"


def test_read_memory_rejects_short_contents(tmp_path: Path) -> None:
    controller = _memory_controller(0x6000, 4, "dead")
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(_attachment(controller, tmp_path), address=0x6000, byte_count=4)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {
        "code": "short_memory_read",
        "address": 0x6000,
        "requested": 4,
        "actual": 2,
    }


def test_read_memory_rejects_missing_memory_payload(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-read-memory-bytes 0x6000 4": [
                {"type": "result", "message": "done", "payload": {}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(_attachment(controller, tmp_path), address=0x6000, byte_count=4)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "short_memory_read"
    assert exc.value.details["requested"] == 4
    assert exc.value.details["actual"] == 0


def test_read_memory_rejects_out_of_range_address(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(_FakeMiController(), tmp_path),
            address=0x1_0000_0000_0000_0000,
            byte_count=4,
        )
    assert exc.value.details["code"] == "bad_read_range"


def test_read_memory_bytes_are_verbatim_not_redacted(tmp_path: Path) -> None:
    secret = "supersecrettoken"  # pragma: allowlist secret - fake test value
    secret_hex = secret.encode().hex()
    byte_count = len(secret)
    controller = _memory_controller(0x4000, byte_count, secret_hex)
    engine = _engine(Redactor(secret_values=[secret]))
    blob = engine.read_memory(
        _attachment(controller, tmp_path), address=0x4000, byte_count=byte_count
    )
    assert blob == secret.encode()  # bytes returned verbatim, NOT masked


def test_read_memory_transcript_line_is_redacted(tmp_path: Path) -> None:
    secret = "transcriptsecret"  # pragma: allowlist secret - fake test value
    attachment = _attachment(_memory_controller(0x5000, 4, "00112233"), tmp_path)
    engine = _engine(Redactor(secret_values=[secret]))
    engine.append_transcript(
        attachment.transcript_path,
        "-break-insert -h panic",
        [MiRecord(type="console", payload=f"loaded {secret}")],
    )
    transcript = attachment.transcript_path.read_text(encoding="utf-8")
    assert secret not in transcript
    assert "[REDACTED]" in transcript


def test_transcript_redactor_sees_secrets_registered_after_engine_creation(
    tmp_path: Path,
) -> None:
    secret = "lateprocesssecret"  # pragma: allowlist secret - fake test value
    scope = object()
    registry = SecretRegistry()
    engine = GdbMiEngine(redactor_factory=lambda: Redactor(registry=registry))
    attachment = _attachment(_memory_controller(0x5000, 4, "00112233"), tmp_path)
    registry.register(secret, scope=scope)
    try:
        engine.append_transcript(
            attachment.transcript_path,
            "-break-insert -h panic",
            [MiRecord(type="console", payload=f"loaded {secret}")],
        )
    finally:
        registry.release(scope)

    transcript = attachment.transcript_path.read_text(encoding="utf-8")
    assert secret not in transcript
    assert "[REDACTED]" in transcript


def test_append_transcript_creates_parent_and_redacts_jsonl(tmp_path: Path) -> None:
    secret = "helpertranscriptsecret"  # pragma: allowlist secret - fake test value
    transcript_path = tmp_path / "nested" / "debug" / "transcript.jsonl"

    append_transcript(
        transcript_path=transcript_path,
        command="<read>",
        records=[MiRecord(type="console", payload=f"loaded {secret}")],
        redactor=Redactor(secret_values=[secret]),
    )

    line = transcript_path.read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["command"] == "<read>"
    assert secret not in line
    assert "[REDACTED]" in line


# --- continue / interrupt ------------------------------------------------------------------


@pytest.mark.parametrize("timeout_sec", [-1.0, math.inf, math.nan])
def test_execution_control_rejects_bad_timeout_before_resume(
    timeout_sec: float, tmp_path: Path
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)

    with pytest.raises(CategorizedError) as exc:
        control.resume(
            _attachment(_FakeMiController(), tmp_path),
            "-exec-continue",
            timeout_sec=timeout_sec,
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_continue_timeout"
    assert engine.executed == []


def test_execution_control_wait_for_stop_records_reads_and_transcript(
    tmp_path: Path,
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    attachment = _attachment(
        _FakeMiController(
            reads=[
                [{"type": "notify", "message": "running", "payload": None}],
                [
                    {
                        "type": "notify",
                        "message": "stopped",
                        "payload": {"reason": "breakpoint-hit", "bkptno": "1"},
                    }
                ],
            ]
        ),
        tmp_path,
    )

    stop = control.wait_for_stop(attachment, timeout_sec=1.0)

    assert stop is not None
    assert stop.reason == "breakpoint-hit"
    assert stop.bkptno == "1"
    messages = [record.message for record in attachment.records if isinstance(record, MiRecord)]
    assert messages == ["running", "stopped"]
    assert engine.transcript_commands == ["<read>", "<read>"]


def test_execution_control_resume_raises_transport_stall_after_interrupt_timeout(
    tmp_path: Path,
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    controller = _FakeMiController(
        responses={"-exec-interrupt": [{"type": "result", "message": "done"}]},
    )
    attachment = _attachment(
        controller,
        tmp_path,
    )

    with pytest.raises(CategorizedError) as exc:
        control.resume(attachment, "-exec-continue", timeout_sec=1.0)

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["code"] == "transport_stall"
    assert engine.executed == ["-exec-continue"]
    assert controller.written == ["-exec-interrupt"]


def test_continue_returns_stop_on_breakpoint_hit(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-continue": [{"type": "result", "message": "running", "payload": None}]},
        reads=[
            [
                {
                    "type": "notify",
                    "message": "stopped",
                    "payload": {"reason": "breakpoint-hit", "bkptno": "1"},
                }
            ]
        ],
    )
    stop = _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert stop.reason == "breakpoint-hit"
    assert stop.bkptno == "1"
    assert stop.timed_out is False


def test_continue_interrupts_on_timeout(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-exec-continue": [{"type": "result", "message": "running", "payload": None}],
            "-exec-interrupt": [{"type": "result", "message": "done", "payload": None}],
        },
        # The resume wait (3 slices for timeout_sec=1) yields nothing; the post-interrupt wait
        # then yields the SIGINT stop.
        reads=[
            [],
            [],
            [],
            [{"type": "notify", "message": "stopped", "payload": {"reason": "signal-received"}}],
        ],
    )
    stop = _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert stop.timed_out is True
    assert "-exec-interrupt" in controller.written


def test_continue_zero_timeout_uses_interactive_wait_cap(tmp_path: Path) -> None:
    resume_reads = int(gdbmi.MAX_INTERACTIVE_WAIT_SEC / gdbmi._STOP_POLL_SLICE_SEC) + 1
    controller = _FakeMiController(
        responses={
            "-exec-continue": [{"type": "result", "message": "running", "payload": None}],
            "-exec-interrupt": [{"type": "result", "message": "done", "payload": None}],
        },
        reads=[
            *([] for _ in range(resume_reads)),
            [{"type": "notify", "message": "stopped", "payload": {"reason": "signal-received"}}],
        ],
    )

    stop = _engine().continue_(_attachment(controller, tmp_path), timeout_sec=0.0)

    assert stop.timed_out is True
    assert controller.written == ["-exec-continue", "-exec-interrupt"]
    assert len(controller.read_timeouts) == resume_reads + 1


@pytest.mark.parametrize("timeout_sec", [-1.0, math.inf, math.nan])
def test_continue_rejects_invalid_timeout(timeout_sec: float, tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-continue": [{"type": "result", "message": "running", "payload": None}]},
    )

    with pytest.raises(CategorizedError) as exc:
        _engine().continue_(_attachment(controller, tmp_path), timeout_sec=timeout_sec)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_continue_timeout"
    assert controller.written == []


def test_continue_raises_transport_stall_on_silent_link(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-exec-continue": [{"type": "result", "message": "running", "payload": None}],
            "-exec-interrupt": [{"type": "result", "message": "done", "payload": None}],
        },
        reads=[],  # never any stop, even after interrupt -> silent link
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["code"] == "transport_stall"


def test_interrupt_returns_stop(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-interrupt": [{"type": "result", "message": "done", "payload": None}]},
        reads=[
            [{"type": "notify", "message": "stopped", "payload": {"reason": "signal-received"}}]
        ],
    )
    stop = _engine().interrupt(_attachment(controller, tmp_path))
    assert stop is not None
    assert stop.reason == "signal-received"


def test_interrupt_returns_none_when_no_stop(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-interrupt": [{"type": "result", "message": "done", "payload": None}]},
        reads=[],
    )
    assert _engine().interrupt(_attachment(controller, tmp_path)) is None


# --- error mapping ------------------------------------------------------------------------


def test_run_maps_mi_error_to_debug_attach_failure(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-insert -h panic": [
                {"type": "result", "message": "error", "payload": {"msg": "no symbol"}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_breakpoint(_attachment(controller, tmp_path), "panic")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_continue_raises_session_exited_on_terminal_stop(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-continue": [{"type": "result", "message": "running", "payload": None}]},
        reads=[
            [{"type": "notify", "message": "stopped", "payload": {"reason": "exited-normally"}}]
        ],
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "session_exited"


# --- transcript ----------------------------------------------------------------------------


def test_execute_mi_command_appends_one_transcript_line_per_command(tmp_path: Path) -> None:
    attachment = _attachment(_FakeMiController(), tmp_path)
    _engine().execute_mi_command(attachment, "-break-list")
    lines = attachment.transcript_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["command"] == "-break-list"


# --- session registry ----------------------------------------------------------------------


def test_registry_register_get_reap(tmp_path: Path) -> None:
    registry = GdbMiSessionRegistry()
    attachment = _attachment(_FakeMiController(), tmp_path)
    registry.register("s1", attachment)
    assert registry.get("s1") is attachment
    assert registry.reap("s1") is attachment
    assert registry.get("s1") is None


def test_registry_require_raises_no_live_session() -> None:
    with pytest.raises(CategorizedError) as exc:
        GdbMiSessionRegistry().require("missing")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "no_live_session"


# --- attach seam (live_vm default) ---------------------------------------------------------


def test_debuginfo_resolver_default_raises_missing_dependency() -> None:
    # The M0 default (no live host) raises MISSING_DEPENDENCY; the handler re-tags it
    # DEBUG_ATTACH_FAILURE (asserted at the handler level).
    with pytest.raises(CategorizedError) as exc:
        debug_gdbmi._resolve_debuginfo_ref("run-1")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
