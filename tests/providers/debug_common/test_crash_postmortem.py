"""Provider-neutral worker-side crash postmortem (ADR-0031/0083/0084)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.crash_postmortem import run_crash_postmortem
from kdive.providers.ports import CrashResult
from kdive.security.secrets.secret_registry import SecretRegistry


def _run(stdout: bytes) -> CrashResult:
    return CrashResult(exit_status=0, stdout=stdout, stderr=b"")


def test_runs_commands_and_redacts() -> None:
    fetched = {"core-ref": b"CORE", "debug-ref": b"VMLINUX"}
    out = run_crash_postmortem(
        vmcore_ref="core-ref",
        debuginfo_ref="debug-ref",
        expected_build_id="deadbeef",
        commands=["bt", "ps"],
        fetch_object=lambda ref: fetched[ref],
        read_build_id=lambda data: "deadbeef",
        run_crash=lambda vmlinux, core, script: _run(b"OK"),
        secret_registry=SecretRegistry(),
    )
    assert out.results == {"bt": {"ran": True}, "ps": {"ran": True}}
    assert out.transcript == "OK"
    assert out.truncated is False


def test_build_id_mismatch_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref",
            debuginfo_ref="debug-ref",
            expected_build_id="aaaa",
            commands=["bt"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "bbbb",
            run_crash=lambda vmlinux, core, script: _run(b"OK"),
            secret_registry=SecretRegistry(),
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rejected_command_batch_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref",
            debuginfo_ref="debug-ref",
            expected_build_id="deadbeef",
            commands=["rm -rf /"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "deadbeef",
            run_crash=lambda vmlinux, core, script: _run(b"OK"),
            secret_registry=SecretRegistry(),
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
