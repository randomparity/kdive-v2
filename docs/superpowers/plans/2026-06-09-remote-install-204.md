# Remote Install (issue #204) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The three tasks edit one module + its wiring and are **tightly coupled** (Task 2 extends the class Task 1 creates; Task 3 wires it), so inline execution in one session is appropriate.

**Goal:** Implement `RemoteLibvirtInstall` — the remote-libvirt Install + Boot plane that pulls the built vmlinuz+modules bundle into a provisioned remote System in-guest, writes the method-conditional crashkernel cmdline into the guest grub, and reboots into it, confirming a fresh boot by boot_id change.

**Architecture:** A single provider module (`providers/remote_libvirt/install.py`) realizing the unchanged `Installer` + `Booter` ports (ADR-0082). `install()` mints one presigned GET (ADR-0081 bundle) and runs an allowlisted in-guest helper (`/usr/local/sbin/kdive-install-kernel`) through the issue-3 `InTargetArtifactChannel` (register-URL → exec → redact). `boot()` reads the guest boot_id, runs the helper's atomic select-`kdive`-slot + detached reboot, and polls boot_id until it changes. All host seams (qemu+tls connection, guest-agent round-trip, object store, clock, sleep) are injected; real curl/tar/grub/reboot run only under `live_vm`.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `ruff`, `ty`. Reuses existing remote modules: `guest_agent.GuestAgentExec`, `artifact_channel.InTargetArtifactChannel`, `transport.remote_connection`, `config.remote_config_from_env`, `store.objectstore.presign_get`.

**Guardrails (run before every commit):** `just lint` · `just type` · `uv run python -m pytest tests/providers/remote_libvirt -q`. Full gate before push: `just ci` and `just m2-gate` (the portability gate — this work must touch only `providers/remote_libvirt/` + `composition.py`, never core).

---

## File Structure

- **Create:** `src/kdive/providers/remote_libvirt/install.py` — `RemoteLibvirtInstall` (Installer + Booter). One responsibility: drive the in-guest install/boot orchestration over injected seams.
- **Create:** `tests/providers/remote_libvirt/test_install.py` — unit tests with a scripted guest-agent + fake store; no host.
- **Modify:** `src/kdive/providers/composition.py` — `build_remote_runtime` uses `RemoteLibvirtInstall` for `installer`/`booter`; drop the `UnimplementedInstaller` import.
- **Modify:** `src/kdive/providers/remote_libvirt/planes.py` — remove the now-dead `UnimplementedInstaller` (replace, don't deprecate) and update the module docstring.
- **Modify:** `tests/providers/remote_libvirt/test_planes.py` — drop the two `UnimplementedInstaller` parametrize cases.

---

## Task 1: `RemoteLibvirtInstall.install()` — in-guest pull + install via the registered-URL seam

**Files:**
- Create: `src/kdive/providers/remote_libvirt/install.py`
- Test: `tests/providers/remote_libvirt/test_install.py`

- [ ] **Step 1: Write the failing tests (install path)**

Create `tests/providers/remote_libvirt/test_install.py`:

```python
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
from kdive.providers.remote_libvirt.guest_agent import AgentExecResult
from kdive.providers.remote_libvirt.install import RemoteLibvirtInstall
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error

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
        return StoredArtifact(
            request.key(), "etag", request.sensitivity, request.retention_class
        )


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


def _backend():
    class _B:
        def resolve(self, ref: str) -> str:
            return f"PEM::{ref}"

    return _B()


def _install(handler: _Handler, store: _FakeStore, registry: SecretRegistry) -> RemoteLibvirtInstall:
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
    assert "crashkernel=256M" in captured["argv"]
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
    persisted = store.writes[0].data.decode("utf-8")  # type: ignore[attr-defined]
    assert _URL not in persisted
    assert REDACTION in persisted
    assert _URL not in registry.snapshot()  # released after persist


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_install.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.providers.remote_libvirt.install`.

- [ ] **Step 3: Write `install.py` (class skeleton + `install()`)**

Create `src/kdive/providers/remote_libvirt/install.py`:

```python
"""Remote-libvirt Install + Boot plane: in-guest kernel install + boot-id readiness (ADR-0082).

`RemoteLibvirtInstall` realizes the `Installer` + `Booter` ports against a provisioned remote
disk-image System (ADR-0080). `install()` mints a single presigned GET for the built
vmlinuz+modules bundle (ADR-0081), then runs a constrained in-guest helper through the issue-3
registered-URL artifact channel (ADR-0078) to pull, extract, and add-or-replace the single
deterministic ``kdive`` grub slot with the method-conditional crashkernel cmdline (already
composed upstream by ``cmdline_for``). `boot()` reads the guest boot_id, runs the helper's
atomic select-slot + detached reboot, and confirms a fresh boot by the boot_id changing — the
readiness signal a console-less remote target affords.

Independent of ``local_libvirt`` (ADR-0076). All slow/host seams — the qemu+tls connection
opener, the guest-agent round-trip, the object store, the clock, sleep — are injected, so unit
tests drive the full orchestration and every error path with no libvirt host; the real
curl/tar/grub/reboot mechanics run only under the ``live_vm`` gate.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.ports import InstallRequest
from kdive.providers.remote_libvirt.artifact_channel import InTargetArtifactChannel
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest_agent import (
    AgentCommand,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

# The single allowlisted in-guest helper the base image carries (ADR-0082 §1); the only program
# this plane lets through GuestAgentExec.
_HELPER = "/usr/local/sbin/kdive-install-kernel"
_TRANSCRIPT_OWNER_KIND = "systems"
# The presigned GET must outlive a worst-case in-guest download of a hundreds-of-MB bundle
# (ADR-0081), not the shortest possible window (ADR-0082 §2).
_DEFAULT_GET_EXPIRY_S = 3600
# The install command downloads + extracts the bundle in-guest; allow well beyond the guest-agent
# default so a large bundle does not time out mid-install.
_DEFAULT_INSTALL_TIMEOUT_S = 1800.0
_DEFAULT_BOOT_TIMEOUT_S = 300.0
_DEFAULT_BOOT_POLL_S = 2.0
# Reboot tears down the guest agent, so the boot command and the post-reboot boot-id polls expect
# the agent to be unreachable / its reply truncated; those categories are swallowed as "still
# rebooting" rather than treated as failures (ADR-0082 §3).
_REBOOT_EXPECTED = frozenset(
    {ErrorCategory.TRANSPORT_FAILURE, ErrorCategory.INFRASTRUCTURE_FAILURE}
)


class _StorePort(Protocol):
    # Both methods: install() mints the GET and the InTargetArtifactChannel persists the
    # redacted transcript via put_artifact, and the one injected factory serves both.
    def presign_get(self, key: str, *, expires_in: int) -> str: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class _Domain(Protocol):
    def name(self) -> str: ...


class _InstallConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenInstallConnection = Callable[[str], _InstallConn]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


def open_libvirt_install(uri: str) -> _InstallConn:
    """The production opener (live-host path; unit tests inject a fake)."""
    # libvirt ships no type stubs; ty infers virConnect, which does not structurally match the
    # protocol. Duck-typed at the seam, as in transport.open_libvirt / provisioning.
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


class RemoteLibvirtInstall:
    """The realized remote `Installer` + `Booter` (ADR-0082).

    Buildable without operator config (ADR-0076): ``KDIVE_REMOTE_LIBVIRT_*`` is read per op via
    ``config_factory``, never at construction.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenInstallConnection = open_libvirt_install,
        store_factory: Callable[[], _StorePort] = object_store_from_env,
        agent_command: AgentCommand = qemu_agent_command,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        get_expiry_s: int = _DEFAULT_GET_EXPIRY_S,
        install_timeout_s: float = _DEFAULT_INSTALL_TIMEOUT_S,
        boot_timeout_s: float = _DEFAULT_BOOT_TIMEOUT_S,
        boot_poll_s: float = _DEFAULT_BOOT_POLL_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._agent_command = agent_command
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._get_expiry_s = get_expiry_s
        self._install_timeout_s = install_timeout_s
        self._boot_timeout_s = boot_timeout_s
        self._boot_poll_s = boot_poll_s
        self._sleep = sleep
        self._monotonic = monotonic

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtInstall:
        """Build from the shared worker env; opens no connection and mints no URL here."""
        return cls(secret_registry=secret_registry)

    def install(self, request: InstallRequest) -> None:
        """Pull the built bundle in-guest, install it, and write the boot entry.

        Mints one presigned GET for ``request.kernel_ref``, runs the allowlisted helper's
        ``install`` subcommand through the registered-URL artifact channel (so the bearer URL is
        masked in any persisted transcript), and replaces the deterministic ``kdive`` grub slot.
        Does not reboot — ``boot`` owns the power transition.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` for a non-zero helper exit (incl. an in-guest
                curl 403/404 from a vanished object — the worker only mints the URL, it never
                fetches), ``TRANSPORT_FAILURE`` for an unreachable guest agent,
                ``INFRASTRUCTURE_FAILURE`` from the object store or a malformed agent reply,
                ``CONFIGURATION_ERROR`` for missing operator config, propagated from the seams.
        """
        config = self._config_factory()
        url = self._store_factory().presign_get(request.kernel_ref, expires_in=self._get_expiry_s)
        argv = [
            _HELPER,
            "install",
            "--url",
            url,
            "--cmdline",
            request.cmdline,
            "--method",
            request.method.value,
        ]
        channel = InTargetArtifactChannel(
            registry=self._secret_registry,
            agent_exec=self._agent_exec(self._install_timeout_s),
            store_factory=self._store_factory,
            scope=object(),
        )
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(request.system_id))
            output = channel.exec_with_capability(
                domain,
                capability_url=url,
                argv=argv,
                owner_kind=_TRANSCRIPT_OWNER_KIND,
                owner_id=str(request.system_id),
            )
        if output.result.exit_status != 0:
            raise CategorizedError(
                "in-guest kernel install exited non-zero",
                category=ErrorCategory.INSTALL_FAILURE,
                details={
                    "system_id": str(request.system_id),
                    "exit_status": output.result.exit_status,
                },
            )

    def _agent_exec(self, timeout_s: float) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_HELPER}),
            timeout_s=timeout_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    def _connection(self, config: RemoteLibvirtConfig):  # noqa: ANN202 - contextmanager passthrough
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _lookup(conn: _InstallConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote domain lookup failed for install/boot",
                category=ErrorCategory.INSTALL_FAILURE,
                details={"domain": domain_name},
            ) from exc


__all__ = ["RemoteLibvirtInstall"]
```

- [ ] **Step 4: Run the install tests to verify they pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_install.py -q`
Expected: PASS (5 install tests; boot tests come in Task 2).

- [ ] **Step 5: Run guardrails and commit**

```bash
just lint && just type && uv run python -m pytest tests/providers/remote_libvirt -q
git add src/kdive/providers/remote_libvirt/install.py tests/providers/remote_libvirt/test_install.py
git commit -m "feat: add RemoteLibvirtInstall.install in-guest pull+install (ADR-0082)"
```

---

## Task 2: `RemoteLibvirtInstall.boot()` — atomic select+reboot + boot-id readiness

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/install.py`
- Test: `tests/providers/remote_libvirt/test_install.py`

- [ ] **Step 1: Write the failing tests (boot path)**

Append to `tests/providers/remote_libvirt/test_install.py`:

```python
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
```

- [ ] **Step 2: Run the boot tests to verify they fail**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_install.py -k boot -q`
Expected: FAIL — `AttributeError: 'RemoteLibvirtInstall' object has no attribute 'boot'`.

- [ ] **Step 3: Add `boot()` and its helpers to `install.py`**

Insert after `install()` (before `_agent_exec`):

```python
    def boot(self, system_id: UUID) -> None:
        """Reboot into the installed kernel and confirm a fresh boot by boot_id change.

        Reads the guest's pre-reboot boot_id, runs the helper's atomic select-``kdive``-slot +
        detached reboot, then polls boot_id until it differs from the baseline — proving a real
        boot transition (a stale agent connection cannot fake a new boot_id, ADR-0082 §3).

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` for a domain lookup fault or a non-zero
                boot-id baseline read; ``TRANSPORT_FAILURE`` when the guest agent is unreachable
                before the reboot; ``BOOT_TIMEOUT`` when no fresh boot_id appears within the boot
                window (a panic/hang manifests as the agent never reconnecting).
        """
        config = self._config_factory()
        agent_exec = self._agent_exec(self._boot_timeout_s)
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(system_id))
            baseline = self._read_boot_id(agent_exec, domain, system_id)
            self._trigger_reboot(agent_exec, domain)
            self._await_fresh_boot(agent_exec, domain, baseline, system_id)

    def _read_boot_id(self, agent_exec: GuestAgentExec, domain: _Domain, system_id: UUID) -> str:
        result = agent_exec.run(domain, [_HELPER, "boot-id"])
        if result.exit_status != 0:
            raise CategorizedError(
                "could not read the guest boot-id baseline",
                category=ErrorCategory.INSTALL_FAILURE,
                details={"system_id": str(system_id), "exit_status": result.exit_status},
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _trigger_reboot(self, agent_exec: GuestAgentExec, domain: _Domain) -> None:
        """Run the helper's atomic select+detached-reboot; a lost agent is the expected signal."""
        try:
            agent_exec.run(domain, [_HELPER, "boot"])
        except CategorizedError as exc:
            if exc.category not in _REBOOT_EXPECTED:
                raise

    def _await_fresh_boot(
        self, agent_exec: GuestAgentExec, domain: _Domain, baseline: str, system_id: UUID
    ) -> None:
        deadline = self._monotonic() + self._boot_timeout_s
        while True:
            current = self._poll_boot_id(agent_exec, domain)
            if current is not None and current != baseline:
                return
            if self._monotonic() >= deadline:
                raise CategorizedError(
                    "system did not reboot into a fresh kernel within the boot window",
                    category=ErrorCategory.BOOT_TIMEOUT,
                    details={"system_id": str(system_id), "timeout_s": self._boot_timeout_s},
                )
            self._sleep(self._boot_poll_s)

    def _poll_boot_id(self, agent_exec: GuestAgentExec, domain: _Domain) -> str | None:
        """One post-reboot boot-id read; ``None`` means "agent down / not ready, keep polling"."""
        try:
            result = agent_exec.run(domain, [_HELPER, "boot-id"])
        except CategorizedError as exc:
            if exc.category in _REBOOT_EXPECTED:
                return None
            raise
        if result.exit_status != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace").strip()
```

- [ ] **Step 4: Run the full install/boot suite to verify it passes**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_install.py -q`
Expected: PASS (all install + boot tests).

- [ ] **Step 5: Run guardrails and commit**

```bash
just lint && just type && uv run python -m pytest tests/providers/remote_libvirt -q
git add src/kdive/providers/remote_libvirt/install.py tests/providers/remote_libvirt/test_install.py
git commit -m "feat: add RemoteLibvirtInstall.boot boot-id readiness (ADR-0082)"
```

---

## Task 3: Wire `RemoteLibvirtInstall` into composition; remove the `UnimplementedInstaller` stub

**Files:**
- Modify: `src/kdive/providers/composition.py:217-267` (`build_remote_runtime`) and the imports block (`:65-75`)
- Modify: `src/kdive/providers/remote_libvirt/planes.py` (remove `UnimplementedInstaller`, update docstring)
- Modify: `tests/providers/remote_libvirt/test_planes.py` (drop the two install/boot cases)

- [ ] **Step 1: Update the planes-stub test first (it must drop the now-removed cases)**

In `tests/providers/remote_libvirt/test_planes.py`, delete these two parametrize lines:

```python
        lambda: planes.UnimplementedInstaller().install(_INSTALL),
        lambda: planes.UnimplementedInstaller().boot(uuid4()),
```

`_INSTALL`/`InstallRequest` are now unused in that test — remove the `InstallRequest` import and the `_INSTALL` sentinel too.

- [ ] **Step 2: Run the planes test to verify it still passes (and references nothing removed yet)**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_planes.py -q`
Expected: PASS (the remaining stub planes still raise `MISSING_DEPENDENCY`).

- [ ] **Step 3: Remove `UnimplementedInstaller` from `planes.py`**

Delete the `UnimplementedInstaller` class. Update the module docstring's "provisioning is real … and build is real" sentence to also name install:

```python
The M2 foundation lands the package, kind, transport, and discovery; provisioning
(``remote_libvirt.provisioning``, ADR-0080), build (``remote_libvirt.build``, ADR-0081), and
install/boot (``remote_libvirt.install``, ADR-0082) are real; the connect/debug and
control/retrieve planes land in the later M2 issues.
```

Remove `InstallRequest` from the `kdive.providers.ports` import if no remaining stub uses it (the connector/controller/retriever stubs do not — verify and trim).

- [ ] **Step 4: Wire `RemoteLibvirtInstall` into `build_remote_runtime`**

In `src/kdive/providers/composition.py`, change the import:

```python
from kdive.providers.remote_libvirt.install import RemoteLibvirtInstall
from kdive.providers.remote_libvirt.planes import (
    UnimplementedConnector,
    UnimplementedController,
    UnimplementedIntrospector,
    UnimplementedRetriever,
)
```

(`UnimplementedInstaller` is dropped from this import.)

In `build_remote_runtime`, replace:

```python
    installer = UnimplementedInstaller()
```

with:

```python
    installer = RemoteLibvirtInstall.from_env(secret_registry=secret_registry)
```

The existing `installer=installer, booter=installer` lines in the `ProviderRuntime(...)` constructor are unchanged (one object realizes both ports, as local does).

- [ ] **Step 5: Add a positive wiring test in `tests/providers/test_composition.py`**

Mirror the existing `test_remote_runtime_has_real_builder` / `_has_real_provisioner` pattern. Add the import `from kdive.providers.remote_libvirt.install import RemoteLibvirtInstall` and this test (the env vars match the sibling tests' opt-in setup):

```python
def test_remote_runtime_has_real_installer_and_booter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_URI", "qemu+tls://host.example/system")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF", "cc")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF", "ck")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_CA_CERT_REF", "ca")
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())
    assert isinstance(runtime.installer, RemoteLibvirtInstall)
    assert runtime.booter is runtime.installer  # one object realizes both ports, as local does
```

Verify the exact env-var setup against the sibling tests (`test_remote_runtime_has_real_builder` at `tests/providers/test_composition.py`) and copy whatever opt-in fixture they use; `build_remote_runtime` itself is buildable without config, so the setenv may be unnecessary — match the siblings.

- [ ] **Step 6: Run guardrails + the composition/runtime tests**

Run:
```bash
just lint && just type
uv run python -m pytest tests/providers -q
```
Expected: PASS. Also grep `tests/` for `UnimplementedInstaller` to confirm no remaining reference to the removed stub: `grep -rn UnimplementedInstaller tests/` → no hits.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/composition.py src/kdive/providers/remote_libvirt/planes.py \
        tests/providers/remote_libvirt/test_planes.py tests/providers/test_composition.py
git commit -m "feat: wire RemoteLibvirtInstall into the remote runtime; drop install stub"
```

---

## Final verification (before the branch review)

- [ ] Run the full local gate: `just ci`
- [ ] Run the portability gate: `just m2-gate` — confirm only `providers/` + `composition.py` (no core) is touched; the measurement should show no new allowlisted/violation core files beyond the pre-existing migration/`models.py`/`objectstore.py`.
- [ ] Confirm the `live_vm` install/boot integration test is **not** un-gated or run here — its real-host mechanics (curl/tar/grub/reboot, `/proc/cmdline` + kernel-identity acceptance check, ADR-0082 §4) run only under the operator-dispatched `live_vm` job; note this limitation in the PR body.

---

## Self-review notes

- **Spec coverage:** issue #204 acceptance — "a built kernel becomes the booted kernel; crashkernel present for kdump only." Unit coverage: `test_install_carries_crashkernel_cmdline_only_for_kdump` (crashkernel iff kdump at the install boundary) + the boot-id readiness tests. The "becomes the booted kernel" identity check is `live_vm` (ADR-0082 §4), called out as a PR limitation — not un-gated.
- **Error taxonomy:** every raised category is an existing `ErrorCategory` value (`INSTALL_FAILURE`, `TRANSPORT_FAILURE`, `BOOT_TIMEOUT`, `INFRASTRUCTURE_FAILURE`); no new strings (M2 §Error taxonomy delta: none).
- **Redaction:** the bearer URL flows through `InTargetArtifactChannel` (register-before-exec, redact-and-persist, release-after) — proven by `test_install_registers_and_redacts_the_capability_url`.
- **Portability:** no file outside `providers/remote_libvirt/` + `composition.py` is touched.
