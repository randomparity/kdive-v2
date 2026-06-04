"""Tests for the local-libvirt Retrieve plane (ADR-0031)."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.providers.local_libvirt.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashResult,
    LocalLibvirtRetrieve,
    crash_command_rejection_reason,
)
from kdive.store.objectstore import StoredArtifact

_ALLOW = frozenset({"bt", "log", "ps", "p", "rd"})

_SYS = UUID("33333333-3333-3333-3333-333333333333")
_TENANT = "local"


@pytest.mark.parametrize("command", ["bt", "  log ", "ps -A", "p jiffies"])
def test_allowed_commands_pass(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is None


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "bt | sh",
        "log > /etc/passwd",
        "rd `whoami`",
        "ps; reboot",
        "log $(id)",
        "!touch x",
        "log\nbt",
        "nuke now",
    ],
)
def test_rejected_commands_have_a_reason(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is not None


@dataclass
class _FakeStore:
    puts: list[tuple[str, str, Sensitivity, bytes]] = field(default_factory=list)
    fail_on: str | None = None

    def put_artifact(
        self,
        tenant: str,
        kind: str,
        object_id: str,
        name: str,
        *,
        data: bytes,
        sensitivity: Sensitivity,
        retention_class: str,
    ) -> StoredArtifact:
        if self.fail_on == name:
            raise CategorizedError(
                "synthetic put failure", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        key = f"{tenant}/{kind}/{object_id}/{name}"
        self.puts.append((key, name, sensitivity, data))
        return StoredArtifact(key, "etag-" + name, sensitivity, retention_class)


def _retriever(store: _FakeStore, *, core: bytes | None) -> LocalLibvirtRetrieve:
    return LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=lambda: store,
        wait_for_vmcore=lambda system_id: core,
        read_vmcore_build_id=lambda data: "deadbeef",
        extract_redacted=lambda data: b"dmesg: password=[REDACTED]",
    )


def test_capture_stores_two_artifacts_and_returns_build_id() -> None:
    store = _FakeStore()
    out = _retriever(store, core=b"RAWCORE").capture(_SYS)
    assert isinstance(out, CaptureOutput)
    assert out.raw.key == f"{_TENANT}/systems/{_SYS}/vmcore"
    assert out.redacted.key == f"{_TENANT}/systems/{_SYS}/vmcore-redacted"
    assert out.vmcore_build_id == "deadbeef"
    names = {(name, sens) for _, name, sens, _ in store.puts}
    assert ("vmcore", Sensitivity.SENSITIVE) in names
    assert ("vmcore-redacted", Sensitivity.REDACTED) in names
    redacted_data = next(d for _, name, _, d in store.puts if name == "vmcore-redacted")
    assert b"hunter2" not in redacted_data and b"[REDACTED]" in redacted_data


def test_capture_no_core_is_readiness_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        _retriever(_FakeStore(), core=None).capture(_SYS)
    assert exc.value.category is ErrorCategory.READINESS_FAILURE


def test_capture_store_failure_is_infrastructure_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        _retriever(_FakeStore(fail_on="vmcore"), core=b"X").capture(_SYS)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def _crash_retriever(*, observed_build_id: str, crash: CrashResult) -> LocalLibvirtRetrieve:
    return LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=_FakeStore,
        wait_for_vmcore=lambda s: None,
        read_vmcore_build_id=lambda data: observed_build_id,
        extract_redacted=lambda data: b"",
        fetch_object=lambda ref: b"BYTES",
        run_crash=lambda vmlinux, vmcore, script: crash,
    )


def test_run_returns_redacted_crash_output() -> None:
    crash = CrashResult(exit_status=0, stdout=b"$ log\npassword=hunter2\nok", stderr=b"")
    out = _crash_retriever(observed_build_id="deadbeef", crash=crash).run(
        vmcore_ref="k/systems/s/vmcore",
        debuginfo_ref="k/runs/r/vmlinux",
        expected_build_id="deadbeef",
        commands=["log"],
    )
    assert isinstance(out, CrashOutput)
    assert "hunter2" not in out.transcript and "[REDACTED]" in out.transcript


def test_run_build_id_mismatch_is_configuration_error() -> None:
    crash = CrashResult(exit_status=0, stdout=b"", stderr=b"")
    with pytest.raises(CategorizedError) as exc:
        _crash_retriever(observed_build_id="aaaa", crash=crash).run(
            vmcore_ref="v",
            debuginfo_ref="d",
            expected_build_id="bbbb",
            commands=["log"],
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
