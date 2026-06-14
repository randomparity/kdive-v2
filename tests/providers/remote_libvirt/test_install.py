"""Unit tests for the remote-libvirt Install + Boot plane (issue #204, ADR-0082).

Drive the full install/boot orchestration with a scripted guest-agent double and a fake
store; no libvirt host, no MinIO. The scripted agent implements GuestAgentExec's two-phase
guest-exec / guest-exec-status protocol so the tests exercise the real seam, not a mock of it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.ports import InstallRequest
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult
from kdive.providers.remote_libvirt.lifecycle.install import RemoteLibvirtInstall
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend, libvirt_error

_URL = "https://store.example/t/runs/r1/kernel?X-Amz-Signature=deadbeefcafe&X-Amz-Expires=3600"

type _Handler = Callable[[list[str]], AgentExecResult]


class _ScriptedAgent:
    """A qemu_agent_command double implementing the two-phase guest-exec protocol.

    ``handler(argv)`` returns the command's AgentExecResult or raises libvirt.libvirtError
    to simulate an unreachable agent (raised on the guest-exec spawn, the way a real torn-down
    agent surfaces). Records every argv it ran.
    """

    def __init__(self, handler: _Handler) -> None:
        self._handler = handler
        self._pending: dict[int, AgentExecResult] = {}
        self._next_pid = 1
        self.argvs: list[list[str]] = []

    def __call__(self, domain: object, command: str, timeout: int, flags: int) -> str:
        payload = json.loads(command)
        if payload["execute"] == "guest-exec":
            args = payload["arguments"]
            argv = [args["path"], *args["arg"]]
            result = self._handler(argv)  # may raise libvirtError
            self.argvs.append(argv)
            pid = self._next_pid
            self._next_pid += 1
            self._pending[pid] = result
            return json.dumps({"return": {"pid": pid}})
        if payload["execute"] == "guest-exec-status":
            result = self._pending.pop(payload["arguments"]["pid"])
            return json.dumps(
                {
                    "return": {
                        "exited": True,
                        "exitcode": result.exit_status,
                        "out-data": base64.b64encode(result.stdout).decode(),
                        "err-data": base64.b64encode(result.stderr).decode(),
                    }
                }
            )
        raise AssertionError(payload)


class _FakeDomain:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _FakeStore:
    """Mints a fixed presigned GET and records the persisted redacted transcript."""

    def __init__(self) -> None:
        self.writes: list[ArtifactWriteRequest] = []
        self.presigned: list[tuple[str, int]] = []

    def presign_get(self, key: str, *, expires_in: int) -> str:
        self.presigned.append((key, expires_in))
        return _URL

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.writes.append(request)
        return StoredArtifact(request.key(), "etag", request.sensitivity, request.retention_class)


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802 - libvirt binding name
        return _FakeDomain(name)

    def close(self) -> None:
        self.closed = True


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("cc", "ck", "ca"),
        concurrent_allocation_cap=1,
        gdb_addr="127.0.0.1",
    )


def _backend() -> RecordingBackend:
    # The conftest SecretBackend double; materialized_pkipath resolves the cert refs through it
    # into a real temp pkipath (created+deleted per op) without touching a real secrets root.
    return RecordingBackend()


def _install(
    handler: _Handler, store: _FakeStore, registry: SecretRegistry
) -> RemoteLibvirtInstall:
    return RemoteLibvirtInstall(
        secret_registry=registry,
        config_factory=_config,
        open_connection=lambda _uri: _FakeConn(),
        store_factory=lambda: store,
        agent_command=_ScriptedAgent(handler),
        secret_backend_factory=_backend,
        sleep=lambda _s: None,
    )


def _request(method: CaptureMethod, cmdline: str) -> InstallRequest:
    return InstallRequest(
        system_id=uuid4(),
        run_id=uuid4(),
        kernel_ref="remote-libvirt/runs/r1/kernel",
        cmdline=cmdline,
        method=method,
    )


def test_install_composes_helper_argv_with_url_cmdline_and_method() -> None:
    seen: list[list[str]] = []

    def handler(argv: list[str]) -> AgentExecResult:
        seen.append(argv)
        return AgentExecResult(0, b"installed", b"")

    store = _FakeStore()
    agent = _ScriptedAgent(handler)
    inst = RemoteLibvirtInstall(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda _uri: _FakeConn(),
        store_factory=lambda: store,
        agent_command=agent,
        secret_backend_factory=_backend,
        sleep=lambda _s: None,
    )
    inst.install(_request(CaptureMethod.HOST_DUMP, "console=ttyS0 root=/dev/vda"))
    assert seen[0] == [
        "/usr/local/sbin/kdive-install-kernel",
        "install",
        "--url",
        _URL,
        "--cmdline",
        "console=ttyS0 root=/dev/vda",
        "--method",
        "host_dump",
    ]
    assert store.presigned == [("remote-libvirt/runs/r1/kernel", 3600)]


def test_install_carries_crashkernel_cmdline_only_for_kdump() -> None:
    captured: dict[str, list[str]] = {}

    def handler(argv: list[str]) -> AgentExecResult:
        captured["argv"] = argv
        return AgentExecResult(0, b"ok", b"")

    store = _FakeStore()
    kdump_cmdline = "console=ttyS0 root=/dev/vda crashkernel=256M"
    _install(handler, store, SecretRegistry()).install(_request(CaptureMethod.KDUMP, kdump_cmdline))
    assert any("crashkernel=256M" in tok for tok in captured["argv"])
    captured.clear()
    _install(handler, store, SecretRegistry()).install(
        _request(CaptureMethod.HOST_DUMP, "console=ttyS0 root=/dev/vda")
    )
    assert not any("crashkernel" in tok for tok in captured["argv"])


def test_install_registers_and_redacts_the_capability_url() -> None:
    registry = SecretRegistry()

    def handler(argv: list[str]) -> AgentExecResult:
        # The helper echoes the URL on stderr (a curl progress line).
        return AgentExecResult(0, b"ok", f"GET {_URL}".encode())

    store = _FakeStore()
    _install(handler, store, registry).install(_request(CaptureMethod.HOST_DUMP, "console=ttyS0"))
    assert len(store.writes) == 1
    persisted = store.writes[0].data.decode("utf-8")
    assert _URL not in persisted
    assert REDACTION in persisted
    assert _URL not in registry.snapshot()  # released after persist


def test_install_loopback_endpoint_fails_before_touching_the_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A loopback object-store endpoint the remote guest cannot reach must fail fast as a
    # configuration_error, before the presigned GET is minted or the guest agent is run
    # (ADR-0105, #375).
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    agent = _ScriptedAgent(lambda _argv: AgentExecResult(0, b"ok", b""))
    store = _FakeStore()
    inst = RemoteLibvirtInstall(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda _uri: _FakeConn(),
        store_factory=lambda: store,
        agent_command=agent,
        secret_backend_factory=_backend,
        sleep=lambda _s: None,
    )
    with pytest.raises(CategorizedError) as excinfo:
        inst.install(_request(CaptureMethod.HOST_DUMP, "console=ttyS0"))
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["env_var"] == "KDIVE_S3_ENDPOINT_URL"
    assert agent.argvs == []  # never reached the guest
    assert store.presigned == []  # never minted the GET


def test_install_nonzero_helper_exit_is_install_failure() -> None:
    def handler(argv: list[str]) -> AgentExecResult:
        return AgentExecResult(3, b"", b"curl: (22) 404")

    with pytest.raises(CategorizedError) as excinfo:
        _install(handler, _FakeStore(), SecretRegistry()).install(
            _request(CaptureMethod.HOST_DUMP, "console=ttyS0")
        )
    assert excinfo.value.category is ErrorCategory.INSTALL_FAILURE


def test_install_unreachable_agent_is_transport_failure() -> None:
    def handler(argv: list[str]) -> AgentExecResult:
        raise libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)

    with pytest.raises(CategorizedError) as excinfo:
        _install(handler, _FakeStore(), SecretRegistry()).install(
            _request(CaptureMethod.HOST_DUMP, "console=ttyS0")
        )
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE


class _FakeClock:
    """A monotonic fake advancing a fixed step per read (shared by GuestAgentExec + the poller)."""

    def __init__(self, step: float = 1.0) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


def _boot_install(handler: _Handler, *, boot_timeout_s: float = 60.0) -> RemoteLibvirtInstall:
    return RemoteLibvirtInstall(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda _uri: _FakeConn(),
        store_factory=_FakeStore,
        agent_command=_ScriptedAgent(handler),
        secret_backend_factory=_backend,
        boot_timeout_s=boot_timeout_s,
        boot_poll_s=0.0,
        sleep=lambda _s: None,
        monotonic=_FakeClock(),
    )


def test_boot_ready_when_boot_id_changes_after_reboot() -> None:
    state = {"reboots": 0, "down": 0}

    def handler(argv: list[str]) -> AgentExecResult:
        sub = argv[1]
        if sub == "boot":
            state["reboots"] += 1
            raise libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)  # reboot tears down the agent
        # boot-id
        if state["reboots"] == 0:
            return AgentExecResult(0, b"BASELINE-ID\n", b"")
        state["down"] += 1
        if state["down"] <= 2:
            raise libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)  # still rebooting
        return AgentExecResult(0, b"FRESH-ID\n", b"")

    _boot_install(handler).boot(uuid4())  # returns -> ready
    assert state["reboots"] == 1


def test_boot_times_out_when_boot_id_never_changes() -> None:
    def handler(argv: list[str]) -> AgentExecResult:
        if argv[1] == "boot":
            return AgentExecResult(0, b"scheduled", b"")  # clean detached return
        return AgentExecResult(0, b"SAME-ID\n", b"")  # never changes

    with pytest.raises(CategorizedError) as excinfo:
        _boot_install(handler, boot_timeout_s=5.0).boot(uuid4())
    assert excinfo.value.category is ErrorCategory.BOOT_TIMEOUT


def test_boot_tolerates_clean_nonzero_reboot_exit() -> None:
    state = {"reboots": 0}

    def handler(argv: list[str]) -> AgentExecResult:
        if argv[1] == "boot":
            state["reboots"] += 1
            return AgentExecResult(1, b"", b"reboot returned 1 but took effect")
        return AgentExecResult(0, (b"BASELINE-ID" if state["reboots"] == 0 else b"FRESH-ID"), b"")

    _boot_install(handler).boot(uuid4())  # tolerated -> ready on boot-id change


def test_boot_unreachable_agent_at_baseline_is_transport_failure() -> None:
    def handler(argv: list[str]) -> AgentExecResult:
        raise libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)

    with pytest.raises(CategorizedError) as excinfo:
        _boot_install(handler).boot(uuid4())
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
