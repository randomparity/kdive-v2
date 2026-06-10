"""RemoteLibvirtRetrieve tests — injected agent/store/opener, no host or S3."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Protocol
from uuid import UUID

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    HeadResult,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.providers.ports import CrashResult
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.guest_agent import AgentExecResult
from kdive.providers.remote_libvirt.retrieve import RemoteLibvirtRetrieve
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_SID = UUID("00000000-0000-0000-0000-0000000000bb")
_SHA = base64.b64encode(b"\x11" * 32).decode()


def _domain_name() -> str:
    return domain_name_for(_SID)


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
    )


def _inspect_json(*, present: bool = True, size: int = 4096) -> bytes:
    return json.dumps(
        {
            "present": present,
            "sha256": _SHA,
            "size_bytes": size,
            "build_id": "deadbeef",
            "dmesg_b64": base64.b64encode(b"kernel panic\n").decode(),
        }
    ).encode()


class _AgentRun(Protocol):
    def run(self, domain: object, argv: list[str]) -> AgentExecResult: ...


class FakeAgentExec:
    """Stands in for GuestAgentExec.run: scripts inspect/upload, simulates a rebooting agent."""

    def __init__(
        self, *, inspect: bytes, unreachable_before: int = 0, upload_exit: int = 0
    ) -> None:
        self._inspect = inspect
        self._unreachable = unreachable_before
        self._upload_exit = upload_exit
        self.argvs: list[list[str]] = []

    def run(self, domain: object, argv: list[str]) -> AgentExecResult:
        self.argvs.append(argv)
        if argv[1] == "inspect":
            if self._unreachable > 0:
                self._unreachable -= 1
                raise CategorizedError(
                    "agent unreachable", category=ErrorCategory.TRANSPORT_FAILURE
                )
            return AgentExecResult(exit_status=0, stdout=self._inspect, stderr=b"")
        return AgentExecResult(exit_status=self._upload_exit, stdout=b"", stderr=b"")


class FakeStore:
    """presign_put + head + put_artifact recorder."""

    def __init__(self, *, head: HeadResult | None) -> None:
        self._head = head
        self.put_requests: list[ArtifactWriteRequest] = []
        self.presigned: list[PresignPutRequest] = []

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        self.presigned.append(request)
        return PresignedUpload(url="https://s3/put?sig=SECRET", required_headers={"h": "v"})

    def head(self, key: str) -> HeadResult | None:
        return self._head

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.put_requests.append(request)
        return StoredArtifact(
            request.key(), "etag-red", request.sensitivity, request.retention_class
        )


def _retrieve(
    agent_exec: _AgentRun,
    store: FakeStore,
    tmp_path: Path,
    *,
    readiness_timeout_s: float = 300.0,
) -> RemoteLibvirtRetrieve:
    conn = FakeControlConn({_domain_name(): FakeDomain(_domain_name())})
    return RemoteLibvirtRetrieve(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        store_factory=lambda: store,
        agent_exec_factory=lambda timeout_s: agent_exec,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
        readiness_timeout_s=readiness_timeout_s,
        readiness_poll_s=0.0,
        sleep=lambda _s: None,
    )


def test_capture_two_phase_happy_path(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json())
    store = FakeStore(head=HeadResult(size_bytes=4096, checksum_sha256=_SHA, etag="etag-raw"))
    out = _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.KDUMP)

    assert out.vmcore_build_id == "deadbeef"
    assert out.raw.etag == "etag-raw"
    assert out.raw.sensitivity is Sensitivity.SENSITIVE
    assert out.raw.key.endswith("/vmcore-kdump")
    assert out.redacted.key.endswith("/vmcore-kdump-redacted")
    assert out.redacted.sensitivity is Sensitivity.REDACTED
    # presign signed the inspected digest + deterministic key.
    assert store.presigned[0].sha256 == _SHA
    assert store.presigned[0].key.endswith("/vmcore-kdump")
    # the upload argv ran after inspect, carrying the bearer URL.
    assert agent.argvs[0][1] == "inspect"
    assert any(a[1] == "upload" for a in agent.argvs)


def test_capture_waits_out_a_rebooting_agent(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json(), unreachable_before=2)
    store = FakeStore(head=HeadResult(size_bytes=4096, checksum_sha256=_SHA, etag="etag-raw"))
    out = _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.KDUMP)
    assert out.vmcore_build_id == "deadbeef"


def test_capture_readiness_window_exhausted_is_readiness_failure(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json(), unreachable_before=10_000)
    store = FakeStore(head=None)
    rt = _retrieve(agent, store, tmp_path, readiness_timeout_s=0.0)
    with pytest.raises(CategorizedError) as exc:
        rt.capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.READINESS_FAILURE


def test_capture_no_core_present_is_readiness_failure(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json(present=False))
    store = FakeStore(head=None)
    with pytest.raises(CategorizedError) as exc:
        _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.READINESS_FAILURE


def test_capture_oversized_core_is_configuration_error(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json(size=6 * 1024**3))
    store = FakeStore(head=None)
    with pytest.raises(CategorizedError) as exc:
        _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_capture_upload_failure_is_infrastructure_failure(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json(), upload_exit=22)
    store = FakeStore(head=None)
    with pytest.raises(CategorizedError) as exc:
        _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_capture_missing_object_after_upload_is_infrastructure_failure(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json())
    store = FakeStore(head=None)  # head returns None despite an exit-0 upload
    with pytest.raises(CategorizedError) as exc:
        _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_capture_rejects_non_kdump_method(tmp_path: Path) -> None:
    agent = FakeAgentExec(inspect=_inspect_json())
    store = FakeStore(head=None)
    with pytest.raises(CategorizedError) as exc:
        _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


class _RaisingInspectAgent:
    """Agent whose inspect raises a fixed non-rebooting CategorizedError, every call."""

    def __init__(self, category: ErrorCategory) -> None:
        self._category = category
        self.calls = 0

    def run(self, domain: object, argv: list[str]) -> AgentExecResult:
        self.calls += 1
        raise CategorizedError("inspect blew up", category=self._category)


def test_capture_non_rebooting_inspect_error_propagates_immediately(tmp_path: Path) -> None:
    # An INFRASTRUCTURE_FAILURE during readiness is NOT a still-rebooting signal: it must
    # propagate as-is on the first call, never be swallowed as reboot-wait nor downgraded
    # to READINESS_FAILURE after the window (the _await_inspect non-rebooting branch).
    agent = _RaisingInspectAgent(ErrorCategory.INFRASTRUCTURE_FAILURE)
    store = FakeStore(head=None)
    with pytest.raises(CategorizedError) as exc:
        _retrieve(agent, store, tmp_path).capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert agent.calls == 1  # no readiness spin on a non-rebooting error


def test_capture_nonzero_inspect_exit_is_infrastructure_failure(tmp_path: Path) -> None:
    # A reachable agent whose inspect command exits non-zero is an infra fault, not a
    # readiness failure (the _parse_inspect exit-status branch).
    class _NonZeroInspect:
        def run(self, domain: object, argv: list[str]) -> AgentExecResult:
            return AgentExecResult(exit_status=3, stdout=b"", stderr=b"boom")

    store = FakeStore(head=None)
    with pytest.raises(CategorizedError) as exc:
        _retrieve(_NonZeroInspect(), store, tmp_path).capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_run_crash_postmortem_delegates() -> None:
    rt = RemoteLibvirtRetrieve(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        fetch_object=lambda ref: b"CORE",
        read_build_id=lambda data: "deadbeef",
        run_crash=lambda vmlinux, core, script: CrashResult(0, b"OK", b""),
    )
    out = rt.run_crash_postmortem(
        vmcore_ref="r", debuginfo_ref="d", expected_build_id="deadbeef", commands=["bt"]
    )
    assert out.transcript == "OK"
