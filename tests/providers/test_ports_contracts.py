"""Provider port value-object contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import HeadResult, StoredArtifact
from kdive.providers.ports._common import ProviderModel, config_error
from kdive.providers.ports.build import BuildOutput, ValidatedUpload
from kdive.providers.ports.debug import (
    GdbBreakpointRef,
    GdbFrame,
    GdbMiAttachment,
    GdbStopRecord,
)
from kdive.providers.ports.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashResult,
    IntrospectOutput,
)


class _ProviderRecord(ProviderModel):
    name: str


class _NoopGdbController:
    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        del command, timeout_sec
        return []

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        del timeout_sec
        return []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        del timeout_sec, raise_error_on_timeout
        return []

    def exit(self) -> None:
        return None


def test_provider_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _ProviderRecord.model_validate({"name": "domain", "ignored": True})


def test_config_error_uses_provider_configuration_taxonomy() -> None:
    error = config_error("bad provider input")

    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(error) == "bad provider input"


def test_debug_port_records_reject_extra_fields_and_serialize_defaults() -> None:
    frame = GdbFrame(level=0, func="panic", addr="0xffffffff81000000", file="panic.c", line=42)
    stop = GdbStopRecord(reason="breakpoint-hit", bkptno="1", frame=frame)
    breakpoint = GdbBreakpointRef(number="1", type="hw breakpoint", func="panic", enabled=True)

    assert stop.model_dump(mode="json") == {
        "reason": "breakpoint-hit",
        "bkptno": "1",
        "stopped_thread": None,
        "frame": {
            "level": 0,
            "func": "panic",
            "addr": "0xffffffff81000000",
            "file": "panic.c",
            "line": 42,
        },
        "timed_out": False,
    }
    assert breakpoint.enabled is True
    with pytest.raises(ValidationError):
        GdbStopRecord.model_validate({"reason": "stopped", "unknown": "field"})


def test_gdb_mi_attachment_records_are_not_shared_between_instances(tmp_path: Path) -> None:
    first = GdbMiAttachment(
        controller=_NoopGdbController(),
        rsp_host="127.0.0.1",
        rsp_port=1234,
        transcript_path=tmp_path / "first.jsonl",
    )
    second = GdbMiAttachment(
        controller=_NoopGdbController(),
        rsp_host="127.0.0.1",
        rsp_port=1235,
        transcript_path=tmp_path / "second.jsonl",
    )

    first.records.append({"type": "result"})

    assert first.records == [{"type": "result"}]
    assert second.records == []


def test_build_output_and_validated_upload_are_stable_namedtuples() -> None:
    output = BuildOutput(kernel_ref="kernel", debuginfo_ref="vmlinux", build_id="deadbeef")
    head = HeadResult(size_bytes=10, checksum_sha256="sha256", etag="etag")
    validated = ValidatedUpload(output=output, heads={"kernel": head})

    assert output._asdict() == {
        "kernel_ref": "kernel",
        "debuginfo_ref": "vmlinux",
        "build_id": "deadbeef",
    }
    assert validated.output is output
    assert validated.heads["kernel"].etag == "etag"


def test_retrieve_port_outputs_are_stable_namedtuples() -> None:
    raw = StoredArtifact("raw-key", "raw-etag", Sensitivity.SENSITIVE, "vmcore")
    redacted = StoredArtifact("redacted-key", "redacted-etag", Sensitivity.REDACTED, "vmcore")

    capture = CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id="deadbeef")
    crash_result = CrashResult(exit_status=0, stdout=b"ok", stderr=b"")
    crash = CrashOutput(results={"log": crash_result._asdict()}, transcript="ok", truncated=False)
    introspect = IntrospectOutput(
        tasks={"tasks": []},
        modules={"modules": []},
        sysinfo={"release": "6.9"},
        truncated=False,
    )

    assert capture.raw.key == "raw-key"
    assert capture.redacted.sensitivity is Sensitivity.REDACTED
    assert capture.vmcore_build_id == "deadbeef"
    log_result = cast("dict[str, object]", crash.results["log"])
    assert log_result["exit_status"] == 0
    assert crash.transcript == "ok"
    assert introspect.sysinfo == {"release": "6.9"}
