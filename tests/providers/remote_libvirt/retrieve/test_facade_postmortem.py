"""Remote-libvirt retrieve facade and postmortem wiring tests."""

from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import StoredArtifact
from kdive.providers.ports import CaptureOutput, CrashOutput, CrashResult
from kdive.providers.remote_libvirt.retrieve import postmortem
from kdive.providers.remote_libvirt.retrieve.facade import RemoteLibvirtRetrieve
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import HostDumpCapturer
from kdive.providers.remote_libvirt.retrieve.kdump_capture import KdumpCapturer
from kdive.security.secrets.secret_registry import SecretRegistry


class _Capturer:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[UUID] = []

    def capture(self, system_id: UUID) -> CaptureOutput:
        self.calls.append(system_id)
        artifact = StoredArtifact(
            f"{self.label}/{system_id}", "etag", Sensitivity.SENSITIVE, "vmcore"
        )
        return CaptureOutput(raw=artifact, redacted=artifact, vmcore_build_id=self.label)


def test_facade_dispatches_supported_capture_methods() -> None:
    system_id = UUID("00000000-0000-0000-0000-00000000faca")
    kdump = _Capturer("kdump")
    host_dump = _Capturer("host")
    retrieve = RemoteLibvirtRetrieve(
        secret_registry=SecretRegistry(),
        kdump_capturer=cast(KdumpCapturer, kdump),
        host_dump_capturer=cast(HostDumpCapturer, host_dump),
    )

    assert retrieve.capture(system_id, CaptureMethod.KDUMP).vmcore_build_id == "kdump"
    assert retrieve.capture(system_id, CaptureMethod.HOST_DUMP).vmcore_build_id == "host"
    assert kdump.calls == [system_id]
    assert host_dump.calls == [system_id]


def test_facade_rejects_unsupported_capture_method() -> None:
    retrieve = RemoteLibvirtRetrieve(
        secret_registry=SecretRegistry(),
        kdump_capturer=cast(KdumpCapturer, _Capturer("kdump")),
        host_dump_capturer=cast(HostDumpCapturer, _Capturer("host")),
    )

    with pytest.raises(CategorizedError) as exc:
        retrieve.capture(UUID("00000000-0000-0000-0000-00000000facb"), CaptureMethod.CONSOLE)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_crash_postmortem_adapter_passes_injected_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SecretRegistry()
    calls: list[dict[str, object]] = []

    def fake_run_crash_postmortem(**kwargs: object) -> CrashOutput:
        calls.append(kwargs)
        return CrashOutput(results={"ok": True}, transcript="done", truncated=False)

    monkeypatch.setattr(postmortem, "_run_crash_postmortem", fake_run_crash_postmortem)
    adapter = postmortem.CrashPostmortemAdapter(
        secret_registry=registry,
        fetch_object=lambda ref: b"object",
        read_build_id=lambda data: "build-id",
        run_crash=lambda _vmlinux, _vmcore, _script: CrashResult(0, b"stdout", b"stderr"),
    )

    output = adapter.run(
        vmcore_ref="vmcore",
        debuginfo_ref="vmlinux",
        expected_build_id="build-id",
        commands=["bt"],
    )

    assert output.results == {"ok": True}
    assert calls[0]["vmcore_ref"] == "vmcore"
    assert calls[0]["debuginfo_ref"] == "vmlinux"
    assert calls[0]["expected_build_id"] == "build-id"
    assert calls[0]["commands"] == ["bt"]
    assert calls[0]["secret_registry"] is registry
