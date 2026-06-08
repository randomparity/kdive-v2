"""Provider port value-object contract tests."""

from __future__ import annotations

from typing import cast

from kdive.domain.models import Sensitivity
from kdive.providers.ports.build import BuildOutput, ValidatedUpload
from kdive.providers.ports.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashResult,
    IntrospectOutput,
)
from kdive.store.objectstore import HeadResult, StoredArtifact


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
