# Remote Control + Retrieve (issue #206) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Realize the remote-libvirt Control plane (power/force_crash over `qemu+tls://`) and the two-phase vmcore Retrieve plane (in-guest inspect → worker `presign_put` → in-guest upload → `head` reference), replacing the `Unimplemented*` stubs, per [ADR-0084](../../adr/0084-remote-control-two-phase-vmcore-retrieve.md).

**Architecture:** Two new provider classes in `providers/remote_libvirt/` (`RemoteLibvirtControl`, `RemoteLibvirtRetrieve`) driving the existing TLS connection + guest-agent + object-store seams, plus a provider-neutral worker-side `crash` postmortem extracted into `providers/debug_common/` that both local and remote delegate to. Composition swaps the stubs for the real classes and widens `supported_capture_methods` to `{KDUMP}`. All host/slow seams (TLS opener, guest-agent, object store, clock, `crash`) are injected so unit tests need no host or S3.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `ruff`, `ty`. libvirt/`crash`/`curl` mechanics are `live_vm`-gated; unit tests use fakes.

**Guardrails (run before every commit):** `just lint` · `just type` · `uv run python -m pytest <focused test> -q`. Before the final commit also run `just m2-gate` (must stay green — this work touches only `providers/`, which is outside the gate's core set). Conventional-commit subjects ≤72 chars, ending with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

**Issue acceptance criteria:**
- `force_crash` panics; the vmcore lands in the object store after reboot and matches the Run build-id.
- Power/reset transitions reflected in System state (the generic `control.*` handlers already own the state edge; the provider supplies the seam).

---

## File Structure

- `src/kdive/providers/debug_common/crash_postmortem.py` — **new.** Provider-neutral worker-side `crash` postmortem: fetch core+debuginfo, build-id provenance, run `crash`, redact. Plus default `live_vm` seams.
- `src/kdive/providers/local_libvirt/retrieve.py` — **modify.** Delegate `run_crash_postmortem` to the shared helper; drop the now-shared private seams.
- `src/kdive/providers/remote_libvirt/control.py` — **new.** `RemoteLibvirtControl` (Controller port over `remote_connection`).
- `src/kdive/providers/remote_libvirt/retrieve.py` — **new.** `RemoteLibvirtRetrieve` (Retriever two-phase capture + CrashPostmortem delegate).
- `src/kdive/providers/remote_libvirt/planes.py` — **delete.** Stubs replaced.
- `src/kdive/providers/composition.py` — **modify.** Wire the real classes; widen `supported_capture_methods`.
- Tests: `tests/providers/debug_common/test_crash_postmortem.py` (new), `tests/providers/remote_libvirt/test_control.py` (new), `tests/providers/remote_libvirt/test_retrieve.py` (new), `tests/providers/remote_libvirt/fakes.py` (new — remote control/capture fakes), delete `tests/providers/remote_libvirt/test_planes.py`, update `tests/providers/test_composition.py` and `tests/providers/local_libvirt/test_retrieve.py` (delegation stays green).

---

## Task 1: Extract the shared worker-side `crash` postmortem into `debug_common`

**Why / where it fits:** ADR-0084 §3 — `run_crash_postmortem` is byte-identical for local and remote (fetch from S3, build-id provenance, run `crash`, redact). Extract it so both providers delegate; remote's Retriever then reuses it without a private copy.

**Files:**
- Create: `src/kdive/providers/debug_common/crash_postmortem.py`
- Create: `tests/providers/debug_common/test_crash_postmortem.py`
- Modify: `src/kdive/providers/local_libvirt/retrieve.py`

- [ ] **Step 1: Write the failing test for the shared helper**

Create `tests/providers/debug_common/test_crash_postmortem.py`:

```python
"""Provider-neutral worker-side crash postmortem (ADR-0031/0083/0084)."""

from __future__ import annotations

from pathlib import Path

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
```

- [ ] **Step 2: Run it; confirm it fails on the missing module**

Run: `uv run python -m pytest tests/providers/debug_common/test_crash_postmortem.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.providers.debug_common.crash_postmortem`.

- [ ] **Step 3: Create the shared helper** (lifted verbatim from `local_libvirt/retrieve.py:138-245`)

Create `src/kdive/providers/debug_common/crash_postmortem.py`:

```python
"""Provider-neutral worker-side `crash` postmortem over a captured vmcore (ADR-0084).

The worker-side half of the Retrieve plane is identical for every provider: fetch the
core + debuginfo from the object store, verify the core's build-id matches the Run's,
run a validated `crash` command batch over an injected subprocess, and return the
redacted transcript. Lifted out of `local_libvirt/retrieve.py` so `remote_libvirt`
reuses it without a private copy (the ADR-0083 `debug_common` home for shared
worker-side postmortem code). Slow seams (`fetch_object`, `run_crash`, `read_build_id`)
are injected; the defaults are `live_vm`-only.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import CrashOutput, CrashResult
from kdive.security.artifacts.crash_commands import validate_crash_commands
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

type FetchObject = Callable[[str], bytes]
type ReadBuildId = Callable[[bytes], str]
type RunCrash = Callable[[Path, Path, str], CrashResult]


def run_crash_postmortem(
    *,
    vmcore_ref: str,
    debuginfo_ref: str,
    expected_build_id: str,
    commands: list[str],
    fetch_object: FetchObject,
    read_build_id: ReadBuildId,
    run_crash: RunCrash,
    secret_registry: SecretRegistry,
) -> CrashOutput:
    """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a rejected crash command or a
            build-id provenance mismatch; ``STALE_HANDLE`` when a referenced object is
            missing; ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
    """
    rejected = validate_crash_commands(commands)
    if rejected is not None:
        raise CategorizedError(
            "crash command batch rejected",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": rejected},
        )
    vmcore_bytes = fetch_object(vmcore_ref)
    observed = read_build_id(vmcore_bytes)
    if observed != expected_build_id:
        raise CategorizedError(
            "captured vmcore build-id does not match the Run's debuginfo build-id",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"vmcore_ref": vmcore_ref},
        )
    vmlinux_bytes = fetch_object(debuginfo_ref)
    with (
        tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
        tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
    ):
        core_file.write(vmcore_bytes)
        core_file.flush()
        vmlinux_file.write(vmlinux_bytes)
        vmlinux_file.flush()
        script = "\n".join(commands) + "\nquit\n"
        crash = run_crash(Path(vmlinux_file.name), Path(core_file.name), script)
    redactor = Redactor(registry=secret_registry)
    transcript = redactor.redact_text(crash.stdout.decode("utf-8", "replace"))
    return CrashOutput(
        results={cmd: {"ran": True} for cmd in commands},
        transcript=transcript,
        truncated=False,
    )


def default_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    # The ref is a key the system itself produced; no client etag handle, so the read
    # is unconditional (ADR-0054). A missing object raises STALE_HANDLE in get_artifact.
    return object_store_from_env().get_artifact(ref, None).data


def default_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def default_run_crash(  # pragma: no cover - live_vm
    vmlinux: Path, vmcore: Path, script: str
) -> CrashResult:
    raise CategorizedError(
        "the crash subprocess runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = [
    "FetchObject",
    "ReadBuildId",
    "RunCrash",
    "default_fetch_object",
    "default_read_vmcore_build_id",
    "default_run_crash",
    "run_crash_postmortem",
]
```

Add `tests/providers/debug_common/__init__.py` if the directory does not already exist (check with `ls tests/providers/debug_common/`).

- [ ] **Step 4: Run the new test; confirm pass**

Run: `uv run python -m pytest tests/providers/debug_common/test_crash_postmortem.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Refactor `local_libvirt/retrieve.py` to delegate (keep its tests green)**

In `src/kdive/providers/local_libvirt/retrieve.py`:

1. Replace the postmortem-specific imports. **Remove** exactly these three (now only used by the extracted helper): `import tempfile`, `from kdive.security.artifacts.crash_commands import validate_crash_commands`, and `from kdive.security.secrets.redaction import Redactor`. **Keep** `from pathlib import Path` and the `CrashResult` import and the `_RunCrash` / `_FetchObject` / `_ReadBuildId` type aliases — they still type the constructor's injected seams. `Step 6`'s `just lint`/`just type` is the backstop: if it flags any of these as unused, remove that one; if it flags `Path`/`CrashResult` as *still needed*, keep it. Add:

```python
from kdive.providers.debug_common.crash_postmortem import (
    default_fetch_object,
    default_read_vmcore_build_id,
    default_run_crash,
    run_crash_postmortem as _run_crash_postmortem,
)
```

2. In `from_env`, replace the three now-shared seam defaults: use `fetch_object=default_fetch_object`, `read_vmcore_build_id=default_read_vmcore_build_id`, `run_crash=default_run_crash`. Delete the module-level `_real_fetch_object`, `_real_read_vmcore_build_id`, and `_real_run_crash` functions (they now live in `debug_common`). Keep `_real_wait_for_vmcore`, `_real_host_dump_capture`, `_real_extract_redacted` (capture-specific, not shared).

3. Replace the body of `LocalLibvirtRetrieve.run_crash_postmortem` with a delegation, preserving the `MISSING_DEPENDENCY` guard for an unconfigured Retriever:

```python
    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

        Delegates to the provider-neutral helper (ADR-0084); raises the same categories.
        """
        if self._fetch_object is None or self._run_crash is None:
            raise CategorizedError(
                "crash seams not configured on this Retriever",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        return _run_crash_postmortem(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            read_build_id=self._read_vmcore_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )
```

- [ ] **Step 6: Run local retrieve tests + lint + type; confirm green**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py tests/providers/debug_common/ -q && just lint && just type`
Expected: PASS, zero lint/type warnings. (If `just type` flags an unused `Path`/`Redactor` import in `retrieve.py`, remove it.)

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/debug_common/crash_postmortem.py tests/providers/debug_common/ src/kdive/providers/local_libvirt/retrieve.py
git commit -m "refactor: extract worker-side crash postmortem to debug_common (#206)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `RemoteLibvirtControl` (Controller over `qemu+tls://`)

**Why / where it fits:** ADR-0084 §1 — power/force_crash over the mutual-TLS connection. Mirrors `LocalLibvirtControl`'s domain logic but opens the connection through `remote_connection`. Satisfies the acceptance's power/reset + force_crash.

**Files:**
- Create: `src/kdive/providers/remote_libvirt/control.py`
- Create: `tests/providers/remote_libvirt/fakes.py`
- Create: `tests/providers/remote_libvirt/test_control.py`

- [ ] **Step 1: Write the remote control fakes**

Create `tests/providers/remote_libvirt/fakes.py`:

```python
"""Remote-libvirt control/capture test doubles (duplicated, no shared layer — ADR-0076)."""

from __future__ import annotations

import libvirt

from tests.providers.remote_libvirt.conftest import libvirt_error


class FakeDomain:
    """The domain slice the remote Control plane drives, recording calls."""

    def __init__(self, name: str, *, raise_on: dict[str, int] | None = None) -> None:
        self._name = name
        self._raise_on = raise_on or {}
        self.calls: list[str] = []

    def name(self) -> str:  # noqa: N802 - libvirt binding name
        return self._name

    def _maybe_raise(self, call: str) -> None:
        if call in self._raise_on:
            raise libvirt_error(self._raise_on[call])

    def create(self) -> int:
        self.calls.append("create")
        self._maybe_raise("create")
        return 0

    def destroy(self) -> int:
        self.calls.append("destroy")
        self._maybe_raise("destroy")
        return 0

    def reset(self, flags: int) -> int:
        self.calls.append("reset")
        self._maybe_raise("reset")
        return 0

    def reboot(self, flags: int) -> int:
        self.calls.append("reboot")
        self._maybe_raise("reboot")
        return 0

    def injectNMI(self, flags: int) -> int:  # noqa: N802 - libvirt binding name
        self.calls.append("injectNMI")
        self._maybe_raise("injectNMI")
        return 0


class FakeControlConn:
    """A libvirt connection slice with lookupByName + close for the control fakes."""

    def __init__(self, lookup: dict[str, FakeDomain]) -> None:
        self._lookup = lookup
        self.closed = False

    def lookupByName(self, name: str) -> FakeDomain:  # noqa: N802 - libvirt binding name
        try:
            return self._lookup[name]
        except KeyError as exc:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN) from exc

    def close(self) -> None:
        self.closed = True
```

- [ ] **Step 2: Write the failing control tests**

Create `tests/providers/remote_libvirt/test_control.py`:

```python
"""RemoteLibvirtControl tests — injected TLS opener + fake conn, no live host."""

from __future__ import annotations

from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.control import RemoteLibvirtControl
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_SYSTEM_ID = __import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa")


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
    )


def _control(domain: FakeDomain | None, tmp_path: Path) -> RemoteLibvirtControl:
    name = domain_name_for(_SYSTEM_ID)
    conn = FakeControlConn({name: domain} if domain is not None else {})
    return RemoteLibvirtControl(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (PowerAction.ON, "create"),
        (PowerAction.OFF, "destroy"),
        (PowerAction.RESET, "reset"),
        (PowerAction.CYCLE, "reboot"),
    ],
)
def test_power_maps_to_libvirt_call(action: PowerAction, expected: str, tmp_path: Path) -> None:
    domain = FakeDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), action)
    assert domain.calls == [expected]


def test_power_on_already_running_swallowed(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"create": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.ON)  # no raise


def test_power_absent_domain_is_control_failure(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _control(None, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.ON)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_power_other_error_is_control_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"reset": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.RESET)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_force_crash_injects_nmi(tmp_path: Path) -> None:
    domain = FakeDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).force_crash(domain_name_for(_SYSTEM_ID))
    assert domain.calls == ["injectNMI"]


def test_force_crash_libvirt_error_is_control_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"injectNMI": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).force_crash(domain_name_for(_SYSTEM_ID))
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
```

> Note: `FakeDomain(domain_name_for(...))` passes the name positionally; the fake's first param is `name`. The test constructs the name the control plane will look up.

- [ ] **Step 3: Run; confirm fails on missing module**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_control.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.providers.remote_libvirt.control`.

- [ ] **Step 4: Implement `RemoteLibvirtControl`**

Create `src/kdive/providers/remote_libvirt/control.py`:

```python
"""Remote-libvirt Control plane: power + force_crash over qemu+tls (ADR-0084).

`RemoteLibvirtControl` realizes the `Controller` port against the remote host. The
domain operations (create/destroy/reset/reboot/injectNMI) match `LocalLibvirtControl`;
only the connection lifecycle differs — the mutual-TLS materialize->connect->cleanup of
`remote_connection` (ADR-0077). DB-free, keyed on the provider domain name. No shared
layer with `local_libvirt` (ADR-0076). All host seams are injected; `libvirt.open` runs
only under the live gate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction
from kdive.providers.ports import Controller as Controller
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)


class _Domain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def reset(self, flags: int) -> int: ...
    def reboot(self, flags: int) -> int: ...
    def injectNMI(self, flags: int) -> int: ...  # noqa: N802 - libvirt binding name


class _ControlConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenControlConnection = Callable[[str], _ControlConn]


def open_libvirt_control(uri: str) -> _ControlConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


class RemoteLibvirtControl:
    """The `Controller` for the remote libvirt host (power + force_crash)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenControlConnection = open_libvirt_control,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtControl:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    def power(self, domain_name: str, action: PowerAction) -> None:
        """Drive the domain's power state; idempotent ``on``/``off`` swallow the post-state.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or a
                non-idempotent libvirt error occurs.
        """
        with self._connection() as conn:
            domain = self._lookup(conn, domain_name)
            self._apply_power(domain, domain_name, action)

    def force_crash(self, domain_name: str) -> None:
        """Panic the guest via NMI (``injectNMI``); the base OS panics on unknown NMI.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or libvirt errors.
        """
        with self._connection() as conn:
            domain = self._lookup(conn, domain_name)
            try:
                domain.injectNMI(0)
            except libvirt.libvirtError as exc:
                raise self._control_failure("injecting NMI into", domain_name) from exc

    def _connection(self):  # noqa: ANN202 - contextmanager passthrough
        return remote_connection(
            self._config_factory(),
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _lookup(conn: _ControlConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise RemoteLibvirtControl._control_failure("looking up", domain_name) from exc

    def _apply_power(self, domain: _Domain, domain_name: str, action: PowerAction) -> None:
        try:
            if action is PowerAction.ON:
                self._idempotent(domain.create, "starting", domain_name)
            elif action is PowerAction.OFF:
                self._idempotent(domain.destroy, "stopping", domain_name)
            elif action is PowerAction.RESET:
                domain.reset(0)
            else:  # PowerAction.CYCLE
                domain.reboot(0)
        except libvirt.libvirtError as exc:
            raise self._control_failure(f"{action.value}-ing", domain_name) from exc

    @staticmethod
    def _idempotent(call: Callable[[], int], verb: str, domain_name: str) -> None:
        """Run an on/off call, swallowing the "already in target state" error as success."""
        try:
            call()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise
            _log.info("%s domain %s: already in target state; treating as ok", verb, domain_name)

    @staticmethod
    def _control_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.CONTROL_FAILURE,
            details={"domain": domain_name},
        )


__all__ = ["RemoteLibvirtControl"]
```

- [ ] **Step 5: Run control tests + lint + type; confirm green**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_control.py -q && just lint && just type`
Expected: PASS, zero warnings.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/remote_libvirt/control.py tests/providers/remote_libvirt/fakes.py tests/providers/remote_libvirt/test_control.py
git commit -m "feat: add RemoteLibvirtControl power/force_crash over TLS (#206)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `RemoteLibvirtRetrieve` (two-phase capture + crash postmortem)

**Why / where it fits:** ADR-0084 §2/§3. `capture()` runs the readiness-wait → inspect → presign → upload → head reference flow and persists the inline redacted dmesg; `run_crash_postmortem()` delegates to the Task-1 shared helper.

**Files:**
- Create: `src/kdive/providers/remote_libvirt/retrieve.py`
- Create: `tests/providers/remote_libvirt/test_retrieve.py`

- [ ] **Step 1: Write the failing capture tests** (happy path + readiness + edges)

Create `tests/providers/remote_libvirt/test_retrieve.py`:

```python
"""RemoteLibvirtRetrieve tests — injected agent/store/opener, no host or S3."""

from __future__ import annotations

import base64
import json
from pathlib import Path
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
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.guest_agent import AgentExecResult
from kdive.providers.remote_libvirt.retrieve import RemoteLibvirtRetrieve
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_SID = UUID("00000000-0000-0000-0000-0000000000bb")
_SHA = base64.b64encode(b"\x11" * 32).decode()


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


class FakeAgent:
    """Returns a scripted AgentExecResult per call; optionally raises N times first."""

    def __init__(self, *, inspect: bytes, unreachable_before: int = 0) -> None:
        self._inspect = inspect
        self._unreachable = unreachable_before
        self.argvs: list[list[str]] = []

    def __call__(self, domain: object, command: str, timeout: int, flags: int) -> str:
        # Used as the agent_command seam; GuestAgentExec wraps it. Simpler: inject agent_exec.
        raise NotImplementedError


class FakeAgentExec:
    """Stands in for GuestAgentExec.run: scripts inspect/upload, simulates a rebooting agent."""

    def __init__(self, *, inspect: bytes, unreachable_before: int = 0, upload_exit: int = 0) -> None:
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
        return StoredArtifact(request.key(), "etag-red", request.sensitivity, request.retention_class)


def _retrieve(agent_exec: FakeAgentExec, store: FakeStore, tmp_path: Path) -> RemoteLibvirtRetrieve:
    conn = FakeControlConn({_domain_name(): FakeDomain(_domain_name())})
    return RemoteLibvirtRetrieve(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        store_factory=lambda: store,
        agent_exec_factory=lambda timeout_s: agent_exec,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
        readiness_poll_s=0.0,
        sleep=lambda _s: None,
    )


def _domain_name() -> str:
    from kdive.providers.runtime_paths import domain_name_for

    return domain_name_for(_SID)


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
    rt = RemoteLibvirtRetrieve(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: FakeControlConn({_domain_name(): FakeDomain(_domain_name())}),
        store_factory=lambda: store,
        agent_exec_factory=lambda timeout_s: agent,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
        readiness_timeout_s=0.0,  # one attempt, then the window is past
        readiness_poll_s=0.0,
        sleep=lambda _s: None,
    )
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
    rt = _retrieve(agent, store, tmp_path)  # type: ignore[arg-type]  # structural _AgentExec
    with pytest.raises(CategorizedError) as exc:
        rt.capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert agent.calls == 1  # no readiness spin on a non-rebooting error


def test_capture_nonzero_inspect_exit_is_infrastructure_failure(tmp_path: Path) -> None:
    # A reachable agent whose inspect command exits non-zero is an infra fault, not a
    # readiness failure (the _parse_inspect exit-status branch).
    class _NonZeroInspect:
        def run(self, domain: object, argv: list[str]) -> AgentExecResult:
            return AgentExecResult(exit_status=3, stdout=b"", stderr=b"boom")

    store = FakeStore(head=None)
    rt = _retrieve(_NonZeroInspect(), store, tmp_path)  # type: ignore[arg-type]
    with pytest.raises(CategorizedError) as exc:
        rt.capture(_SID, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_run_crash_postmortem_delegates(tmp_path: Path) -> None:
    from kdive.providers.ports import CrashResult

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
```

- [ ] **Step 2: Run; confirm fails on missing module**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_retrieve.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.providers.remote_libvirt.retrieve`.

- [ ] **Step 3: Implement `RemoteLibvirtRetrieve`**

Create `src/kdive/providers/remote_libvirt/retrieve.py`:

```python
"""Remote-libvirt Retrieve plane: two-phase vmcore capture + crash postmortem (ADR-0084).

`capture()` runs after a crash against a System whose guest must have rebooted out of the
kdump capture kernel: it waits out a still-rebooting agent (readiness), inspects the local
core (digest/size/build-id + a bounded inline redacted dmesg), mints a single presigned PUT
for a deterministic key, runs the in-guest upload through the registered-URL artifact channel
(ADR-0078 §2), and references the uploaded object via `head` (presence + etag; the signed
checksum is the integrity binding). The redacted dmesg is redacted again worker-side and
persisted inline. `run_crash_postmortem()` delegates to the shared `debug_common` helper.
KDUMP-only (host-dump is host-coupled). All host/S3/clock seams are injected.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from uuid import UUID

import libvirt

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    HeadResult,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
    artifact_key,
)
from kdive.providers.debug_common.crash_postmortem import (
    default_fetch_object,
    default_read_vmcore_build_id,
    default_run_crash,
    run_crash_postmortem as _run_crash_postmortem,
)
from kdive.providers.debug_common.crash_postmortem import (
    FetchObject,
    ReadBuildId,
    RunCrash,
)
from kdive.providers.ports import CaptureOutput, CrashOutput
from kdive.providers.remote_libvirt.artifact_channel import InTargetArtifactChannel
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest_agent import (
    AgentCommand,
    AgentExecResult,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

_HELPER = "/usr/local/sbin/kdive-capture-vmcore"
_TENANT = "remote-libvirt"
_RETENTION = "vmcore"
_OWNER_KIND = "systems"
# One object + one checksum; lifetime must cover the in-guest upload of a multi-hundred-MB core.
_DEFAULT_PUT_EXPIRY_S = 3600
# S3 single-PUT ceiling (ADR-0048); larger cores are a multipart follow-up.
_MAX_CORE_BYTES = 5 * 1024**3
_DEFAULT_READINESS_TIMEOUT_S = 300.0
_DEFAULT_READINESS_POLL_S = 2.0
# The inspect command hashes the whole core in-guest; 120s comfortably covers a sha256 of
# the 5 GiB ceiling (~10s at commodity disk/CPU rates). GuestAgentExec folds a command that
# does not exit within this bound into TRANSPORT_FAILURE, which _await_inspect treats as
# "still rebooting" — acceptable because a working inspect finishes well inside the bound, so
# a TRANSPORT_FAILURE in practice means an unreachable agent, not a slow hash.
_DEFAULT_INSPECT_TIMEOUT_S = 120.0
_DEFAULT_UPLOAD_TIMEOUT_S = 1800.0
# An unreachable agent during readiness is "still rebooting out of the kdump kernel". A
# non-rebooting CategorizedError (a malformed reply -> INFRASTRUCTURE_FAILURE) is NOT in this
# set, so _await_inspect re-raises it immediately instead of spinning the readiness window.
_AGENT_REBOOTING = frozenset({ErrorCategory.TRANSPORT_FAILURE})


class _CoreInfo(NamedTuple):
    sha256: str
    size_bytes: int
    build_id: str
    dmesg: bytes


class _StorePort(Protocol):
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...
    def head(self, key: str) -> HeadResult | None: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class _Domain(Protocol):
    def name(self) -> str: ...


class _RetrieveConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


class _AgentExec(Protocol):
    def run(self, domain: Any, argv: list[str]) -> AgentExecResult: ...


type OpenRetrieveConnection = Callable[[str], _RetrieveConn]
type AgentExecFactory = Callable[[float], _AgentExec]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


def open_libvirt_capture(uri: str) -> _RetrieveConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


class RemoteLibvirtRetrieve:
    """The realized remote `Retriever` + `CrashPostmortem` (ADR-0084)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenRetrieveConnection = open_libvirt_capture,
        store_factory: Callable[[], _StorePort] = object_store_from_env,
        agent_command: AgentCommand = qemu_agent_command,
        agent_exec_factory: AgentExecFactory | None = None,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        put_expiry_s: int = _DEFAULT_PUT_EXPIRY_S,
        readiness_timeout_s: float = _DEFAULT_READINESS_TIMEOUT_S,
        readiness_poll_s: float = _DEFAULT_READINESS_POLL_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        fetch_object: FetchObject = default_fetch_object,
        read_build_id: ReadBuildId = default_read_vmcore_build_id,
        run_crash: RunCrash = default_run_crash,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._agent_command = agent_command
        self._agent_exec_factory = agent_exec_factory or self._default_agent_exec
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._put_expiry_s = put_expiry_s
        self._readiness_timeout_s = readiness_timeout_s
        self._readiness_poll_s = readiness_poll_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._fetch_object = fetch_object
        self._read_build_id = read_build_id
        self._run_crash = run_crash

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtRetrieve:
        """Build from the shared worker env; opens no connection and mints no URL here."""
        return cls(secret_registry=secret_registry)

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Two-phase vmcore capture: inspect -> presign -> in-guest upload -> reference.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a non-KDUMP method or an
                over-ceiling core; ``READINESS_FAILURE`` when the guest never becomes
                reachable or carries no core; ``TRANSPORT_FAILURE`` for an agent fault
                outside the readiness window; ``INFRASTRUCTURE_FAILURE`` for an upload
                failure, a malformed reply, or an object absent after a success-reporting
                upload.
        """
        if method is not CaptureMethod.KDUMP:
            raise CategorizedError(
                "remote-libvirt capture supports only the kdump method",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"method": method.value},
            )
        config = self._config_factory()
        raw_key = artifact_key(_TENANT, _OWNER_KIND, str(system_id), f"vmcore-{method.value}")
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(system_id))
            info = self._await_inspect(domain, system_id)
            upload = self._store_factory().presign_put(
                PresignPutRequest(
                    key=raw_key,
                    sha256=info.sha256,
                    size_bytes=info.size_bytes,
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class=_RETENTION,
                    expires_in=self._put_expiry_s,
                )
            )
            self._upload(domain, system_id, upload)
        raw = self._reference(raw_key, info.sha256, system_id)
        redacted = self._persist_redacted(system_id, method, info.dmesg)
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=info.build_id)

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Delegate to the provider-neutral worker-side crash postmortem (ADR-0084)."""
        return _run_crash_postmortem(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            read_build_id=self._read_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )

    def _await_inspect(self, domain: _Domain, system_id: UUID) -> _CoreInfo:
        agent_exec = self._agent_exec_factory(_DEFAULT_INSPECT_TIMEOUT_S)
        deadline = self._monotonic() + self._readiness_timeout_s
        while True:
            try:
                result = agent_exec.run(domain, [_HELPER, "inspect"])
            except CategorizedError as exc:
                if exc.category not in _AGENT_REBOOTING or self._monotonic() >= deadline:
                    if exc.category in _AGENT_REBOOTING:
                        raise self._readiness_failure(system_id, "guest agent never came back")
                    raise
                self._sleep(self._readiness_poll_s)
                continue
            return self._parse_inspect(result, system_id)

    def _parse_inspect(self, result: AgentExecResult, system_id: UUID) -> _CoreInfo:
        if result.exit_status != 0:
            raise CategorizedError(
                "in-guest vmcore inspect exited non-zero",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "exit_status": result.exit_status},
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8", "replace"))
            present = bool(payload["present"])
            sha256 = str(payload["sha256"])
            size_bytes = int(payload["size_bytes"])
            build_id = str(payload["build_id"])
            dmesg = base64.b64decode(payload["dmesg_b64"])
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise CategorizedError(
                "guest vmcore inspect returned a malformed reply",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc
        if not present:
            raise self._readiness_failure(system_id, "no kdump core in the guest's dump storage")
        if size_bytes > _MAX_CORE_BYTES:
            raise CategorizedError(
                "captured core exceeds the single-PUT 5 GiB ceiling",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "size_bytes": size_bytes},
            )
        return _CoreInfo(sha256=sha256, size_bytes=size_bytes, build_id=build_id, dmesg=dmesg)

    def _upload(self, domain: _Domain, system_id: UUID, upload: PresignedUpload) -> None:
        argv = [_HELPER, "upload", "--url", upload.url]
        for key, value in upload.required_headers.items():
            argv += ["--header", f"{key}:{value}"]
        channel = InTargetArtifactChannel(
            registry=self._secret_registry,
            agent_exec=self._agent_exec_factory(_DEFAULT_UPLOAD_TIMEOUT_S),
            store_factory=self._store_factory,
            scope=object(),
        )
        output = channel.exec_with_capability(
            domain,
            capability_url=upload.url,
            argv=argv,
            owner_kind=_OWNER_KIND,
            owner_id=str(system_id),
        )
        if output.result.exit_status != 0:
            raise CategorizedError(
                "in-guest vmcore upload exited non-zero",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "exit_status": output.result.exit_status},
            )

    def _reference(self, raw_key: str, sha256: str, system_id: UUID) -> StoredArtifact:
        head = self._store_factory().head(raw_key)
        if head is None:
            raise CategorizedError(
                "uploaded vmcore is absent after a success-reporting upload",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )
        if head.checksum_sha256 is not None and head.checksum_sha256 != sha256:
            raise CategorizedError(
                "uploaded vmcore checksum does not match the inspected core",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )
        return StoredArtifact(raw_key, head.etag, Sensitivity.SENSITIVE, _RETENTION)

    def _persist_redacted(
        self, system_id: UUID, method: CaptureMethod, dmesg: bytes
    ) -> StoredArtifact:
        text = dmesg.decode("utf-8", "replace")
        redacted = Redactor(registry=self._secret_registry).redact_text(text)
        return self._store_factory().put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=f"vmcore-{method.value}-redacted",
                data=redacted.encode("utf-8"),
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION,
            )
        )

    def _default_agent_exec(self, timeout_s: float) -> GuestAgentExec:
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
    def _lookup(conn: _RetrieveConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote domain lookup failed for capture",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"domain": domain_name},
            ) from exc

    @staticmethod
    def _readiness_failure(system_id: UUID, reason: str) -> CategorizedError:
        return CategorizedError(
            reason,
            category=ErrorCategory.READINESS_FAILURE,
            details={"system_id": str(system_id)},
        )


__all__ = ["RemoteLibvirtRetrieve"]
```

- [ ] **Step 4: Run retrieve tests; confirm pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_retrieve.py -q`
Expected: PASS (all cases). Fix any seam-signature mismatch the test exposes (e.g. `agent_exec_factory` arity) before moving on.

- [ ] **Step 5: lint + type**

Run: `just lint && just type`
Expected: PASS, zero warnings. Remove the unused `FakeAgent` class from the test if `just lint` flags it (it was a scaffolding stub; `FakeAgentExec` is the one used).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/remote_libvirt/retrieve.py tests/providers/remote_libvirt/test_retrieve.py
git commit -m "feat: add RemoteLibvirtRetrieve two-phase vmcore capture (#206)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire composition; widen capture methods; delete stubs

**Why / where it fits:** ADR-0084 §3 Consequences — replace `UnimplementedController`/`UnimplementedRetriever`, advertise `{KDUMP}`, delete `planes.py` (Replace-don't-deprecate).

**Files:**
- Modify: `src/kdive/providers/composition.py`
- Delete: `src/kdive/providers/remote_libvirt/planes.py`, `tests/providers/remote_libvirt/test_planes.py`
- Modify: `tests/providers/test_composition.py`

- [ ] **Step 1: Update the failing composition test first**

In `tests/providers/test_composition.py`, change `test_remote_runtime_advertises_no_capture_methods_yet` to assert the widened set, and add real-class assertions. Replace that test with:

```python
def test_remote_runtime_advertises_kdump_capture() -> None:
    # The retrieve issue widens the set to the two-phase kdump path (ADR-0084);
    # host-dump stays unsupported (host-coupled).
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.supported_capture_methods == frozenset({CaptureMethod.KDUMP})


def test_remote_runtime_has_real_control_and_retrieve() -> None:
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.controller, RemoteLibvirtControl)
    assert isinstance(runtime.retriever, RemoteLibvirtRetrieve)
    assert runtime.crash_postmortem is runtime.retriever
```

Add the imports near the top of the test module:

```python
from kdive.domain.capture import CaptureMethod
from kdive.providers.remote_libvirt.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.retrieve import RemoteLibvirtRetrieve
```

- [ ] **Step 2: Run; confirm the composition test fails**

Run: `uv run python -m pytest tests/providers/test_composition.py -q -k "remote_runtime"`
Expected: FAIL — the runtime still advertises `frozenset()` and wires the stubs.

- [ ] **Step 3: Wire the real classes in `composition.py`**

In `src/kdive/providers/composition.py`:

1. Add imports:

```python
from kdive.domain.capture import CaptureMethod
from kdive.providers.remote_libvirt.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.retrieve import RemoteLibvirtRetrieve
```

2. Remove the `from kdive.providers.remote_libvirt.planes import (UnimplementedController, UnimplementedRetriever)` block.

3. In `build_remote_runtime`, replace the stub lines:

```python
    retriever = RemoteLibvirtRetrieve.from_env(secret_registry=secret_registry)
```

(delete `retriever = UnimplementedRetriever()`), and in the `ProviderRuntime(...)` call:

```python
        controller=RemoteLibvirtControl.from_env(secret_registry=secret_registry),
        retriever=retriever,
        crash_postmortem=retriever,
        ...
        supported_capture_methods=frozenset({CaptureMethod.KDUMP}),
```

4. Update the `build_remote_runtime` docstring: drop "fail-fast stubs for the control/retrieve plane the later M2 issue supplies"; state control/retrieve are real (ADR-0084). Update the inline comment above `supported_capture_methods` to note the kdump two-phase path landed.

- [ ] **Step 4: Delete the stubs**

```bash
git rm src/kdive/providers/remote_libvirt/planes.py tests/providers/remote_libvirt/test_planes.py
```

- [ ] **Step 5: Run composition tests + lint + type**

Run: `uv run python -m pytest tests/providers/test_composition.py -q && just lint && just type`
Expected: PASS, zero warnings. (`just type` will fail if any import of `planes` remains — grep `rg -n "remote_libvirt.planes|Unimplemented" src tests` and fix.)

- [ ] **Step 6: Full provider suite + m2-gate**

Run: `uv run python -m pytest tests/providers/ -q && just m2-gate`
Expected: PASS; gate prints "gate passed: no core surface touched outside the ADR-0076 allowlist."

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: wire remote control/retrieve; advertise kdump capture (#206)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- ADR-0084 §1 Control → Task 2. ✓
- ADR-0084 §2 two-phase capture (readiness wait, inspect, presign, upload, head, inline redacted dmesg, KDUMP-only, 5 GiB ceiling) → Task 3. ✓
- ADR-0084 §3 shared crash postmortem + replace stubs → Task 1 (extract) + Task 4 (wire/delete). ✓
- ADR-0084 §4 force_crash→capture orchestration uses existing handlers → no new code (verified `vmcore.fetch`/`control.*` handlers unchanged). ✓
- Acceptance "power/reset reflected in System state" → the generic `control.*` handlers own the edge; Task 2 supplies the seam they call. ✓
- Acceptance "vmcore lands in object store after reboot, matches Run build-id" → Task 3 capture lands it; build-id match is the postmortem provenance gate (Task 1 helper), asserted by the `live_vm` test (gated, not in unit scope). ✓

**Placeholder scan:** No TBD/“add error handling”/“similar to Task N” — every code step shows full code. ✓

**Type consistency:** `_CoreInfo`, `RemoteLibvirtRetrieve.__init__` seam names (`agent_exec_factory`, `fetch_object`, `read_build_id`, `run_crash`), `run_crash_postmortem` keyword signature, and `HeadResult`/`PresignPutRequest`/`PresignedUpload`/`StoredArtifact` field names all match across tasks and the real `provider_components.artifacts` definitions read during planning. The shared helper's `FetchObject`/`ReadBuildId`/`RunCrash` type aliases are imported by remote rather than redefined. ✓

**Known live-only gaps (documented, not unit-tested):** the real NMI panic→kdump, the in-guest `kdive-capture-vmcore` helper, the actual S3 upload, and `crash` execution run only under `live_vm`; the `live_vm` acceptance test (operator-run, per AGENTS.md) closes the end-to-end and the build-id match. State this limitation in the PR body.
