# Retrieve plane (vmcore capture/fetch + crash postmortem) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture a crashed System's kdump vmcore as an idempotent `vmcore.fetch` job that stores a raw `sensitive` core and a `redacted` derivative, expose them through `vmcore.*`/`artifacts.*` (redacted-only), and port crash postmortem (`postmortem.crash`/`.triage`) symbolizing the core against the Run's `debuginfo_ref`.

**Architecture:** A seam-injected `Retriever`/`CrashPostmortem` provider (`providers/local_libvirt/retrieve.py`) mirrors `LocalLibvirtBuild`: waiting for kdump, reading the core, extracting its build-id, building the redacted dmesg derivative, and running `crash` are `live_vm`-gated seams; orchestration is unit-tested with fakes. `vmcore.fetch(system_id)` admits a `JobKind.CAPTURE_VMCORE` job (dedup `{system_id}:capture_vmcore`) under the per-System advisory lock; `capture_handler` captures **with no DB transaction held**, then inserts two `artifacts` rows under the lock, skipping re-capture if a `vmcore` row already exists. `vmcore.list`/`artifacts.*` are synchronous `redacted`-only reads. `postmortem.*` are synchronous, ungated, redaction-on-return reads. No schema migration.

**Tech Stack:** Python 3.13 · `psycopg` 3 (async) · Pydantic v2 · FastMCP 3.x · `boto3` (object store) · `pytest` (testcontainers Postgres) · `ruff`/`ty`.

**Design source:** [`../specs/2026-06-04-retrieve-plane-design.md`](../specs/2026-06-04-retrieve-plane-design.md) · [`../../adr/0031-retrieve-plane-vmcore-postmortem.md`](../../adr/0031-retrieve-plane-vmcore-postmortem.md). The spec's Components / Error contract / Idempotency sections are the authoritative contracts; each task references its slice.

---

## File structure

- **Create** `src/kdive/providers/local_libvirt/retrieve.py` — `CaptureOutput`/`Retriever`, `CrashOutput`/`CrashPostmortem`, the crash-command allowlist + validator (ported from v1 `crash/commands.py`), `LocalLibvirtRetrieve.{from_env,capture,run}`, the redacted-dmesg derivative seam, the `live_vm` real seams. DB-free.
- **Create** `src/kdive/mcp/tools/vmcore.py` — `fetch_vmcore` (the `vmcore.fetch` tool body), `capture_handler`, `list_vmcores`, `postmortem_crash`, `postmortem_triage`, `register`, `register_handlers`.
- **Create** `src/kdive/mcp/tools/artifacts.py` — `artifacts_list`, `artifacts_get`, `register`. Redacted-only.
- **Modify** `src/kdive/mcp/app.py:21-29,32-51` — import `artifacts`, `vmcore`; append `artifacts.register`, `vmcore.register` to `_PLANE_REGISTRARS` and `vmcore.register_handlers` to `_HANDLER_REGISTRARS`.
- **Create** `tests/providers/local_libvirt/test_retrieve.py` — provider unit tests (fake store / fake seams); `live_vm`-gated real path test.
- **Create** `tests/mcp/_seed.py` — shared `seed_crashed_system`/`seed_run_on_system` helpers imported by both MCP test modules (keeps each test module self-contained).
- **Create** `tests/mcp/test_vmcore_tools.py` — `vmcore.fetch`/`capture_handler`/`list`/`postmortem.*` tests + the surface-wide redaction guard (real Postgres, fake `Retriever`/`CrashPostmortem`/store).
- **Create** `tests/mcp/test_artifacts_tools.py` — `artifacts.list`/`.get` redacted-only tests (real Postgres).
- **Modify** `tests/mcp/test_app.py` — assert the two new tool registrars and the capture handler are registered.

> Each commit keeps all guardrails green: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`, `uv run python -m pytest -q`. Verify `git log -1 --oneline` after every commit (prek may roll back a `ruff format` rewrite). Type any SQL helper text passed to `cur.execute` as `LiteralString` where ty flags it.

> **Shared-file edits to call out in NOTES:** `src/kdive/mcp/app.py` (two `_PLANE_REGISTRARS` appends + one `_HANDLER_REGISTRARS` append + two imports). No edits to `errors.py`, `db/schema/0001_init.sql`, `domain/models.py` (`JobKind.CAPTURE_VMCORE`, `ErrorCategory.READINESS_FAILURE`, `Sensitivity` already exist). `docs/adr/README.md` already updated.

---

## Task 1: The crash-command validator (ported security control, DB-free)

**Files:** Create `src/kdive/providers/local_libvirt/retrieve.py` (first slice), `tests/providers/local_libvirt/test_retrieve.py` (first slice)

The load-bearing security control for `postmortem.crash`: every caller command is sanitized (deny shell-reaching metacharacters) and checked against a read-only allowlist before any `crash` invocation (ported from v1 `postmortem/crash/commands.py`).

- [ ] **Step 1 (test first):** Create `tests/providers/local_libvirt/test_retrieve.py` with:

```python
"""Tests for the local-libvirt Retrieve plane (ADR-0031)."""

from __future__ import annotations

import pytest

from kdive.providers.local_libvirt.retrieve import crash_command_rejection_reason

_ALLOW = frozenset({"bt", "log", "ps", "p", "rd"})


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
```

- [ ] **Step 2:** Run `uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py -q`. Expected: FAIL (`ImportError`: module/function absent).

- [ ] **Step 3:** Create `src/kdive/providers/local_libvirt/retrieve.py` with the module docstring and the validator (port v1 logic verbatim, re-homed):

```python
"""Local-libvirt Retrieve plane: capture a kdump vmcore and run crash postmortem (ADR-0031).

`LocalLibvirtRetrieve` realizes two seam-injected ports, mirroring `LocalLibvirtBuild`:
`Retriever.capture(system_id)` waits for kdump, stores the raw `sensitive` core and a
`redacted` dmesg derivative, and returns both refs plus the core's build-id;
`CrashPostmortem.run(...)` symbolizes the core against the Run's `debuginfo_ref` over an
injected `crash` subprocess. The slow, host-bound operations are `live_vm`-gated seams, so
the orchestration and the full error contract are unit-tested with fakes. The crash-command
validator is the load-bearing security control: the postmortem path is never gated, so every
caller command is sanitized and allowlist-checked before any `crash` invocation.
"""

from __future__ import annotations

import re

# Pipe-to-shell, redirection, command substitution, chaining, backgrounding.
_DENY_CHARS = ("|", ">", "<", "`", "$(", ";", "&")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def crash_command_rejection_reason(command: str, allowlist: frozenset[str]) -> str | None:
    """Return ``None`` if the command is permitted, else a human-readable rejection reason.

    Two layers: a security-critical denylist (newline/control chars, a leading ``!`` shell
    escape, and the shell metacharacters in ``_DENY_CHARS``) and an allowlist of read-only
    leading verbs. The denylist is the boundary the ungated postmortem path relies on.
    """
    stripped = command.strip()
    if not stripped:
        return "empty command"
    if _CONTROL.search(command):
        return "command contains a newline or control character"
    if stripped[0] == "!":
        return "shell escape ('!') is not permitted"
    for token in _DENY_CHARS:
        if token in command:
            return f"disallowed metacharacter {token!r}"
    verb = stripped.split()[0].lower()
    if verb not in allowlist:
        return f"verb {verb!r} is not in the crash command allowlist"
    return None
```

- [ ] **Step 4:** Run `uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py -q`. Expected: PASS.

- [ ] **Step 5:** Guardrails: `uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest -q`. Commit:

```bash
git add src/kdive/providers/local_libvirt/retrieve.py tests/providers/local_libvirt/test_retrieve.py
git commit -m "feat(retrieve): port crash-command allowlist validator (#24)"
git log -1 --oneline
```

## Task 2: The `Retriever` port and `LocalLibvirtRetrieve.capture`

**Files:** Modify `src/kdive/providers/local_libvirt/retrieve.py`, `tests/providers/local_libvirt/test_retrieve.py` (spec §Components → `retrieve.py`)

`capture(system_id)` waits for a complete core, reads its build-id, and stores two artifacts (raw `sensitive`, redacted `derivative`). The slow ops are injected seams; the unit tests use a fake store + fake seams and assert the `readiness_failure`/`infrastructure_failure` contract and the deterministic object keys.

- [ ] **Step 1 (tests first):** Append to `tests/providers/local_libvirt/test_retrieve.py`:

```python
from dataclasses import dataclass, field
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.providers.local_libvirt.retrieve import CaptureOutput, LocalLibvirtRetrieve
from kdive.store.objectstore import StoredArtifact

_SYS = UUID("33333333-3333-3333-3333-333333333333")
_TENANT = "local"


@dataclass
class _FakeStore:
    puts: list[tuple[str, str, Sensitivity, bytes]] = field(default_factory=list)
    fail_on: str | None = None

    def put_artifact(
        self, tenant: str, kind: str, object_id: str, name: str, *,
        data: bytes, sensitivity: Sensitivity, retention_class: str,
    ) -> StoredArtifact:
        if self.fail_on == name:
            raise CategorizedError("synthetic put failure", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
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
```

- [ ] **Step 2:** Run `uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py -q`. Expected: FAIL (`CaptureOutput`/`LocalLibvirtRetrieve` absent).

- [ ] **Step 3:** Add to `src/kdive/providers/local_libvirt/retrieve.py` (after the validator) only the **capture** imports, types, and class. This task is self-contained: it imports nothing it does not use and forward-references no symbol from a later task (the crash seams `fetch_object`/`run_crash` and the `from_env`/`_real_*` helpers all land in Task 3, which widens `__init__`). All Task-2 guardrails (`ruff`, `ty`, `pytest`) must be green at this commit.

```python
from collections.abc import Callable
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import StoredArtifact

_RETENTION_CLASS = "vmcore"


class CaptureOutput(NamedTuple):
    """A capture result: the raw + redacted StoredArtifacts and the core's GNU build-id."""

    raw: StoredArtifact
    redacted: StoredArtifact
    vmcore_build_id: str


class Retriever(Protocol):
    """The handler-facing capture port (realized M0 contract), keyed on the System."""

    def capture(self, system_id: UUID) -> CaptureOutput: ...


class _StorePort(Protocol):
    def put_artifact(
        self, tenant: str, kind: str, object_id: str, name: str, *,
        data: bytes, sensitivity: Sensitivity, retention_class: str,
    ) -> StoredArtifact: ...


type _WaitForVmcore = Callable[[UUID], bytes | None]
type _ReadBuildId = Callable[[bytes], str]
type _ExtractRedacted = Callable[[bytes], bytes]
```

Then the class with a **capture-only** `__init__` (Task 3 widens it to add the crash seams and `from_env`):

```python
class LocalLibvirtRetrieve:
    """The realized Retrieve port: kdump capture + crash postmortem (ADR-0031)."""

    def __init__(
        self, *, tenant: str,
        store_factory: Callable[[], _StorePort],
        wait_for_vmcore: _WaitForVmcore,
        read_vmcore_build_id: _ReadBuildId,
        extract_redacted: _ExtractRedacted,
    ) -> None:
        self._tenant = tenant
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._wait_for_vmcore = wait_for_vmcore
        self._read_vmcore_build_id = read_vmcore_build_id
        self._extract_redacted = extract_redacted

    def capture(self, system_id: UUID) -> CaptureOutput:
        """Wait for kdump, store the raw + redacted core, return both refs and the build-id.

        Raises:
            CategorizedError: ``READINESS_FAILURE`` if no complete core appears in the
                window; ``INFRASTRUCTURE_FAILURE`` propagated from a failed artifact store.
        """
        data = self._wait_for_vmcore(system_id)
        if data is None:
            raise CategorizedError(
                "no complete vmcore appeared within the capture window",
                category=ErrorCategory.READINESS_FAILURE,
                details={"system_id": str(system_id)},
            )
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, "vmcore", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id, "vmcore-redacted", self._extract_redacted(data), Sensitivity.REDACTED
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)

    def _put(self, system_id: UUID, name: str, data: bytes, sens: Sensitivity) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            self._tenant, "systems", str(system_id), name,
            data=data, sensitivity=sens, retention_class=_RETENTION_CLASS,
        )
```

- [ ] **Step 4:** Run `uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py -q`. Expected: PASS. No crash types or seams are referenced yet, so `ty check src` and `ruff check` are clean at this commit (no forward-refs, no unused imports).

- [ ] **Step 5:** Guardrails green; commit:

```bash
git add src/kdive/providers/local_libvirt/retrieve.py tests/providers/local_libvirt/test_retrieve.py
git commit -m "feat(retrieve): add Retriever port + LocalLibvirtRetrieve.capture (#24)"
git log -1 --oneline
```

## Task 3: The `CrashPostmortem` port and `LocalLibvirtRetrieve.run`

**Files:** Modify `src/kdive/providers/local_libvirt/retrieve.py`, `tests/providers/local_libvirt/test_retrieve.py` (spec §Components, ADR-0031 §7)

`run(...)` stages the core + vmlinux from the store, verifies the core's build-id matches the expected one (provenance), builds the crash command script, runs `crash`, and returns parsed/redacted output. Provenance mismatch and a bad command are caller-validated upstream (in the tool, Task 5); the provider verifies the build-id and runs the batch.

- [ ] **Step 1 (tests first):** Append to `tests/providers/local_libvirt/test_retrieve.py`:

```python
from kdive.providers.local_libvirt.retrieve import CrashOutput, CrashResult


def _crash_retriever(*, observed_build_id: str, crash: CrashResult) -> LocalLibvirtRetrieve:
    return LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=lambda: _FakeStore(),
        wait_for_vmcore=lambda s: None,
        read_vmcore_build_id=lambda data: observed_build_id,
        extract_redacted=lambda data: b"",
        fetch_object=lambda ref: b"BYTES",
        run_crash=lambda vmlinux, vmcore, script: crash,
    )


def test_run_returns_redacted_crash_output() -> None:
    crash = CrashResult(exit_status=0, stdout=b"$ log\npassword=hunter2\nok", stderr=b"")
    out = _crash_retriever(observed_build_id="deadbeef", crash=crash).run(
        vmcore_ref="k/systems/s/vmcore", debuginfo_ref="k/runs/r/vmlinux",
        expected_build_id="deadbeef", commands=["log"],
    )
    assert isinstance(out, CrashOutput)
    assert "hunter2" not in out.transcript and "[REDACTED]" in out.transcript


def test_run_build_id_mismatch_is_configuration_error() -> None:
    crash = CrashResult(exit_status=0, stdout=b"", stderr=b"")
    with pytest.raises(CategorizedError) as exc:
        _crash_retriever(observed_build_id="aaaa", crash=crash).run(
            vmcore_ref="v", debuginfo_ref="d", expected_build_id="bbbb", commands=["log"],
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2:** Run the file. Expected: FAIL (`CrashOutput`/`CrashResult`/`run` absent).

- [ ] **Step 3a (widen `__init__`, additively):** Add the crash-seam imports and type aliases, and widen `LocalLibvirtRetrieve.__init__` to accept the two crash seams **as optional keyword params defaulting to `None`** — so Task-2's five-seam `_retriever(...)` call still type-checks and passes unchanged (no edit to the Task-2 test helper). Add to the import block: `import tempfile`, `from pathlib import Path`, `from kdive.security.redaction import Redactor`, and extend the objectstore import to `from kdive.store.objectstore import StoredArtifact, object_store_from_env`. Add the aliases:

```python
type _FetchObject = Callable[[str], bytes]
type _RunCrash = Callable[[Path, Path, str], "CrashResult"]
```

and extend `__init__` (the crash seams default to `None`; `run` raises a clear error if invoked without them, which never happens in the wired path because `from_env`/tests supply them):

```python
    def __init__(
        self, *, tenant: str,
        store_factory: Callable[[], _StorePort],
        wait_for_vmcore: _WaitForVmcore,
        read_vmcore_build_id: _ReadBuildId,
        extract_redacted: _ExtractRedacted,
        fetch_object: _FetchObject | None = None,
        run_crash: _RunCrash | None = None,
    ) -> None:
        self._tenant = tenant
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._wait_for_vmcore = wait_for_vmcore
        self._read_vmcore_build_id = read_vmcore_build_id
        self._extract_redacted = extract_redacted
        self._fetch_object = fetch_object
        self._run_crash = run_crash
```

In `run` (Step 3b), narrow the optionals at entry: `if self._fetch_object is None or self._run_crash is None: raise CategorizedError("crash seams not configured", category=ErrorCategory.MISSING_DEPENDENCY)` — keeps `ty` happy (no `None`-call) and documents the contract. The Task-2 helper is untouched; both task suites pass.

- [ ] **Step 3b (add the crash types + `run`):** Add to `retrieve.py`:

```python
class CrashResult(NamedTuple):
    """A raw `crash` subprocess result: exit status and captured streams."""

    exit_status: int
    stdout: bytes
    stderr: bytes


class CrashOutput(NamedTuple):
    """A parsed, redacted crash batch result."""

    results: dict[str, object]
    transcript: str
    truncated: bool


class CrashPostmortem(Protocol):
    """The handler-facing crash-postmortem port (realized M0 contract)."""

    def run(
        self, *, vmcore_ref: str, debuginfo_ref: str,
        expected_build_id: str, commands: list[str],
    ) -> CrashOutput: ...
```

And the `run` method on `LocalLibvirtRetrieve`:

```python
    def run(
        self, *, vmcore_ref: str, debuginfo_ref: str,
        expected_build_id: str, commands: list[str],
    ) -> CrashOutput:
        """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

        Stages both objects to temp files, verifies the core's build-id matches
        ``expected_build_id`` (provenance), runs ``crash`` over the injected seam, and
        returns the parsed, **redacted** transcript.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` on a build-id provenance mismatch;
                ``MISSING_DEPENDENCY`` if the crash seams were not configured.
        """
        if self._fetch_object is None or self._run_crash is None:
            raise CategorizedError(
                "crash seams not configured on this Retriever",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected_build_id:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            script = "\n".join(commands) + "\nquit\n"
            crash = self._run_crash(Path(vmlinux_file.name), Path(core_file.name), script)
        redactor = Redactor()
        transcript = redactor.redact_text(crash.stdout.decode("utf-8", "replace"))
        return CrashOutput(
            results={cmd: {"ran": True} for cmd in commands},
            transcript=transcript,
            truncated=False,
        )
```

If a `CrashResult` stub was added in Task 2 Step 4, replace it with this definition (do not duplicate).

- [ ] **Step 4:** Run the file. Expected: PASS.

- [ ] **Step 5:** Add `from_env` (the `live_vm` real seams) and `__all__`. The real seams raise `MISSING_DEPENDENCY` unless on a live host, exactly as `build.py`'s `_real_*` do:

```python
    @classmethod
    def from_env(cls) -> LocalLibvirtRetrieve:
        """Build from env; does not poll the host, open S3, or spawn `crash` (lazy seams)."""
        return cls(
            tenant="local",
            store_factory=object_store_from_env,
            wait_for_vmcore=_real_wait_for_vmcore,
            read_vmcore_build_id=_real_read_vmcore_build_id,
            extract_redacted=_real_extract_redacted,
            fetch_object=_real_fetch_object,
            run_crash=_real_run_crash,
        )


_CRASH_DIR_ENV = "KDIVE_CRASH_DIR"


def _real_wait_for_vmcore(system_id: UUID) -> bytes | None:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real kdump capture runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id), "crash_dir_env": _CRASH_DIR_ENV},
    )


def _real_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def _real_extract_redacted(data: bytes) -> bytes:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore dmesg extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    return object_store_from_env().get_artifact(ref, "").data


def _real_run_crash(vmlinux: Path, vmcore: Path, script: str) -> CrashResult:  # pragma: no cover - live_vm
    raise CategorizedError(
        "the crash subprocess runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = [
    "CaptureOutput", "CrashOutput", "CrashPostmortem", "CrashResult",
    "LocalLibvirtRetrieve", "Retriever", "crash_command_rejection_reason",
]
```

> Note: `_real_fetch_object` passes `""` as the etag, which `get_artifact` re-quotes for `If-Match`; under `live_vm` the caller should pass the row's etag. M0's live path stages by key; the etag-conditional fetch is exercised by the object-store tests. If ty/coverage flags the etag, thread the etag through `fetch_object` as a 2-arg seam in a follow-up — out of scope for the unit-tested path here.

- [ ] **Step 6:** Guardrails green; commit:

```bash
git add src/kdive/providers/local_libvirt/retrieve.py tests/providers/local_libvirt/test_retrieve.py
git commit -m "feat(retrieve): add CrashPostmortem port + live_vm seams (#24)"
git log -1 --oneline
```

## Task 4: shared seed helper + `artifacts.*` tools (redacted-only reads)

**Files:** Create `tests/mcp/_seed.py`, `src/kdive/mcp/tools/artifacts.py`, `tests/mcp/test_artifacts_tools.py` (spec §Components → `artifacts.py`)

`artifacts.list(system_id)` and `artifacts.get(artifact_id)` are synchronous reads that surface **only** `redacted` rows; a `sensitive` id is not-found-shaped. This task also lands the shared System/Run seeding helpers in a neutral `tests/mcp/_seed.py` so Task 4 and Task 5 are each self-contained and green at their own commit (no cross-test-module import).

- [ ] **Step 0 (shared seed helpers):** Create `tests/mcp/_seed.py` with `seed_crashed_system(pool) -> str` and `seed_run_on_system(pool, sys_id, *, debuginfo_ref, build_id) -> str`, ported from the `_granted_allocation`/`_seed_system` shape in `tests/mcp/test_control_tools.py` (granted allocation via `LocalLibvirtDiscovery`/`register_local_libvirt_resource` → System inserted `state=SystemState.CRASHED`; `seed_run_on_system` inserts an Investigation + a `succeeded` Run with the given `debuginfo_ref`, then a `run_steps` `build` row whose `result` jsonb carries `{"build_id": build_id, ...}`). Both `test_artifacts_tools.py` and `test_vmcore_tools.py` import from `tests.mcp._seed`. No test asserts on `_seed.py` itself; it is exercised transitively. Add a trivial import-smoke test in `test_artifacts_tools.py` Step 1 so this file is covered at the Task-4 commit.

- [ ] **Step 1 (tests first):** Create `tests/mcp/test_artifacts_tools.py`, importing the shared helpers from `tests.mcp._seed`, and insert one `sensitive` + one `redacted` `artifacts` row owned by the seeded System, then assert the read filter:

```python
"""artifacts.* tool tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import Sensitivity
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import artifacts as artifacts_tools
from kdive.security.rbac import Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    return RequestContext(principal="u", agent_session="s", projects=projects,
                          roles={"proj": Role.OPERATOR})


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system_with_artifacts(pool: AsyncConnectionPool) -> tuple[str, str, str]:
    """Insert a System row and a sensitive + redacted artifact owned by it.

    Returns (system_id, sensitive_artifact_id, redacted_artifact_id).
    Uses the shared seeding helper from tests.mcp._seed for the System; insert artifacts directly.
    """
    from tests.mcp._seed import seed_crashed_system

    sys_id = await seed_crashed_system(pool)
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = []
        for name, sens in (("vmcore", "sensitive"), ("vmcore-redacted", "redacted")):
            await cur.execute(
                "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                "retention_class) VALUES ('systems', %s, %s, 'e', %s, 'vmcore') RETURNING id",
                (sys_id, f"k/systems/{sys_id}/{name}", sens),
            )
            row = await cur.fetchone()
            rows.append(str(row["id"]))
    return sys_id, rows[0], rows[1]


def test_artifacts_list_returns_redacted_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_list(pool, _ctx(), system_id=sys_id)
        ids = {r.object_id for r in resp}
        assert ids == {red_id}  # the sensitive row is never surfaced

    asyncio.run(_run())


def test_artifacts_get_redacted_returns_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_get(pool, _ctx(), artifact_id=red_id)
        assert resp.status != "error" and resp.refs

    asyncio.run(_run())


def test_artifacts_get_sensitive_is_not_found_shaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, sens_id, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_get(pool, _ctx(), artifact_id=sens_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await artifacts_tools.artifacts_get(pool, _ctx(), artifact_id="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2:** Run `uv run python -m pytest tests/mcp/test_artifacts_tools.py -q`. Expected: FAIL (`artifacts` tool module absent; `tests.mcp._seed` exists from Step 0). This file is fully self-contained at this task — it depends only on `_seed.py`, not on `test_vmcore_tools.py`.

- [ ] **Step 3:** Create `src/kdive/mcp/tools/artifacts.py`:

```python
"""The `artifacts.*` MCP tools — redacted-only artifact reads (ADR-0031).

`artifacts.list(system_id)` and `artifacts.get(artifact_id)` surface **only** `redacted`
rows; a `sensitive` artifact id is shaped as not-found, so the raw vmcore is never
fetchable through the agent surface even by id. Project membership is enforced through the
owning System.
"""

from __future__ import annotations

import logging
from typing import LiteralString
from uuid import UUID

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse

_log = logging.getLogger(__name__)

_LIST_SQL: LiteralString = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND sensitivity = 'redacted' "
    "ORDER BY created_at DESC"
)
_GET_SQL: LiteralString = (
    "SELECT a.id, a.object_key, a.owner_id FROM artifacts a "
    "WHERE a.id = %s AND a.sensitivity = 'redacted'"
)
_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"


def _config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def artifacts_list(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> list[ToolResponse]:
    """Return the System's `redacted` artifacts as envelopes (empty list if none/absent)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return []
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return []
            await cur.execute(_LIST_SQL, (uid,))
            rows = await cur.fetchall()
    responses: list[ToolResponse] = []
    for row in rows:
        try:
            responses.append(
                ToolResponse.success(
                    str(row["id"]), "available",
                    suggested_next_actions=["artifacts.get"],
                    refs={"object": row["object_key"]},
                )
            )
        except ValueError:
            _log.warning("artifact %s violates the envelope invariant; degraded", row["id"])
    return responses


async def artifacts_get(
    pool: AsyncConnectionPool, ctx: RequestContext, *, artifact_id: str
) -> ToolResponse:
    """Return one `redacted` artifact's envelope, or a not-found-shaped config error.

    A missing artifact and a `sensitive` artifact are indistinguishable (both
    `configuration_error`), so the raw vmcore cannot be fetched even when its id is known.
    """
    uid = _as_uuid(artifact_id)
    if uid is None:
        return _config_error(artifact_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_GET_SQL, (uid,))
            row = await cur.fetchone()
            if row is None:
                return _config_error(artifact_id)
            await cur.execute(_PROJECT_SQL, (row["owner_id"],))
            owner = await cur.fetchone()
        if owner is None or owner["project"] not in ctx.projects:
            return _config_error(artifact_id)
        return ToolResponse.success(
            artifact_id, "available",
            suggested_next_actions=["artifacts.get"],
            refs={"object": row["object_key"]},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `artifacts.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="artifacts.list")
    async def artifacts_list_tool(system_id: str) -> list[ToolResponse]:
        return await artifacts_list(pool, current_context(), system_id=system_id)

    @app.tool(name="artifacts.get")
    async def artifacts_get_tool(artifact_id: str) -> ToolResponse:
        return await artifacts_get(pool, current_context(), artifact_id=artifact_id)
```

- [ ] **Step 4:** Run `uv run python -m pytest tests/mcp/test_artifacts_tools.py -q`. Expected: PASS (the module now exists and `_seed.py` supplies the System). This task is self-contained and green at its own commit.

- [ ] **Step 5:** Full guardrails (`uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest -q`), then commit the helper + module + tests together:

```bash
git add tests/mcp/_seed.py src/kdive/mcp/tools/artifacts.py tests/mcp/test_artifacts_tools.py
git commit -m "feat(artifacts): add redacted-only artifacts.list/.get + test seeds (#24)"
git log -1 --oneline
```

## Task 5: `vmcore.*` + `postmortem.*` tools and the capture handler

**Files:** Create `src/kdive/mcp/tools/vmcore.py`, `tests/mcp/test_vmcore_tools.py` (spec §Components → `vmcore.py`, §Error contract, §Idempotency)

`fetch_vmcore` admits on a `crashed` System; `capture_handler` captures under the per-System lock with the existing-row idempotency check; `list_vmcores` is redacted-only; `postmortem_crash`/`_triage` validate commands, run the port, redact, return.

- [ ] **Step 1 (tests first):** Create `tests/mcp/test_vmcore_tools.py`, importing `seed_crashed_system`/`seed_run_on_system` from `tests.mcp._seed` (created in Task 4 Step 0 — no import from another test module). Include:
  - `test_fetch_vmcore_crashed_enqueues_job` — `vmcore.fetch` on a `crashed` System returns `status=="queued"` and a job with `dedup_key == f"{sys_id}:capture_vmcore"` exists;
  - `test_fetch_vmcore_non_crashed_is_config_error` — a `ready` System → `configuration_error` with `current_status=="ready"`;
  - `test_fetch_vmcore_without_operator_raises` — `Role.VIEWER` → `AuthorizationError`;
  - `test_fetch_vmcore_malformed_uuid_is_config_error`;
  - `test_capture_handler_stores_rows_and_returns_ref` — with a `_FakeRetriever` returning a `CaptureOutput`, the handler inserts a `sensitive` + a `redacted` `artifacts` row owned by the System and returns the raw ref; the job's `result_ref` is the raw key;
  - `test_capture_handler_idempotent_skips_recapture` — pre-insert a `vmcore` row, run the handler with a retriever whose `.capture` raises if called; assert no second row and the existing key is returned;
  - `test_capture_handler_no_core_raises_readiness` — retriever raises `READINESS_FAILURE`; the handler re-raises it (worker dead-letters); no rows inserted;
  - `test_capture_handler_missing_system_is_infra_failure`;
  - `test_list_vmcores_redacted_only` — after a capture, `list_vmcores` returns only the `vmcore-redacted` row;
  - `test_postmortem_crash_bad_command_is_config_error` — `commands=["bt | sh"]` → synchronous `configuration_error`, the port is never called;
  - `test_postmortem_crash_runs_and_redacts` — a `_FakeCrashPostmortem` returning a transcript with `password=hunter2`; the response carries the redacted transcript (no `hunter2`), and the port received `expected_build_id` from the Run's build step;
  - `test_postmortem_crash_unbuilt_run_is_config_error` — a Run with `debuginfo_ref=None` → `configuration_error`.

  Use a `_FakeRetriever` (records `.capture` calls, returns a canned `CaptureOutput` built from a `_FakeStore`-style `StoredArtifact`) and a `_FakeCrashPostmortem` (returns a canned `CrashOutput`). Mirror the `_FakeControl` injection style from `test_control_tools.py`.

- [ ] **Step 2:** Run `uv run python -m pytest tests/mcp/test_vmcore_tools.py -q`. Expected: FAIL (`vmcore` tool module absent; `_seed.py` already exists from Task 4).

- [ ] **Step 3:** Create `src/kdive/mcp/tools/vmcore.py`. Structure (mirror `control.py` + `runs.py`):
  - imports: `asyncio`, `UUID`, `AsyncConnection`/`AsyncConnectionPool`, `LockScope`/`advisory_xact_lock`, `SYSTEMS`/`RUNS`/`ARTIFACTS`, `queue`, `HandlerRegistry`, `RequestContext`/`current_context`, `ToolResponse`, `audit`, `require_role`/`Role`, the provider `Retriever`/`CrashPostmortem`/`LocalLibvirtRetrieve`/`crash_command_rejection_reason`, `register_artifact_row`, `Redactor`.
  - `_CRASH_ALLOWLIST: frozenset[str]` — the ported v1 set (`bt ps log kmem sys mod struct union p rd vtop task files vm net dev irq mach runq mount swap timer dis sym list tree search foreach help`).
  - `_TRIAGE_COMMANDS = ("log", "bt")`.
  - `fetch_vmcore(pool, ctx, *, system_id)`: `_as_uuid`; load System project-scoped; `require_role(ctx, system.project, Role.OPERATOR)`; if `system.state is not SystemState.CRASHED` → `configuration_error` with `data={"current_status": system.state.value}`; `queue.enqueue(conn, JobKind.CAPTURE_VMCORE, {"system_id": system_id}, _authorizing(ctx, system.project), f"{system_id}:capture_vmcore")`; return `_system_job_envelope(job, uid)` (carry `system_id` in `data`, like `control.py`).
  - `capture_handler(conn, job, retriever)`: `system_id = UUID(job.payload["system_id"])`; `async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):` load System (None → `INFRASTRUCTURE_FAILURE`); query an existing `vmcore` row (`SELECT object_key FROM artifacts WHERE owner_kind='systems' AND owner_id=%s AND object_key LIKE %s` with `%/vmcore`, or store the raw object key deterministically and match exactly); if present return it. Otherwise — **release the lock before the slow capture**: read the System, then `out = await asyncio.to_thread(retriever.capture, system_id)` **outside** the transaction, then re-open a short `transaction()+lock`, re-check the existing row, insert both rows via `ARTIFACTS.insert(conn, register_artifact_row(out.raw, owner_kind="systems", owner_id=system_id))` and the redacted, audit `capture_vmcore`, return `out.raw.key`. (The capture seam holds no DB transaction — the worker contract, mirroring `build_handler`.) On `CategorizedError` from `capture`, let it propagate (worker dead-letters with the category).
  - `list_vmcores(pool, ctx, *, system_id)`: delegate to the same `redacted`-only filter `artifacts_list` uses, narrowed to `object_key LIKE '%/vmcore-redacted'` (or simply reuse `artifacts.artifacts_list` — `vmcore.list` is `artifacts.list` for the vmcore rows). Simplest: call `artifacts.artifacts_list(pool, ctx, system_id=system_id)` and filter to keys ending `vmcore-redacted`.
  - `postmortem_crash(pool, ctx, *, run_id, commands)`: load Run project-scoped; `if run.debuginfo_ref is None: return configuration_error`; read the build step's `build_id` from `run_steps` (`SELECT result FROM run_steps WHERE run_id=%s AND step='build'`; missing/!dict → `configuration_error`); for each command `if crash_command_rejection_reason(cmd, _CRASH_ALLOWLIST): return configuration_error` (synchronous, before any port call); resolve System; load its raw `vmcore` object key (`SELECT object_key FROM artifacts WHERE owner_kind='systems' AND owner_id=%s AND object_key LIKE '%/vmcore'`; none → `configuration_error`); `out = await asyncio.to_thread(crash.run, vmcore_ref=..., debuginfo_ref=run.debuginfo_ref, expected_build_id=build_id, commands=commands)`; build a fresh `Redactor()` and return `ToolResponse.success(run_id, "succeeded", suggested_next_actions=["postmortem.crash", "artifacts.list"], data={"transcript": redactor.redact_text(out.transcript)})`. (The provider already redacts; the tool re-redacts as the return-boundary belt-and-suspenders, per CLAUDE.md "redact before it is returned".)
  - `postmortem_triage(pool, ctx, *, run_id)`: call `postmortem_crash` with `list(_TRIAGE_COMMANDS)`, relabel the response.
  - `register(app, pool)`: `vmcore.fetch`, `vmcore.list`, `postmortem.crash`, `postmortem.triage`.
  - `register_handlers(registry, *, retriever=None)`: `r = retriever or LocalLibvirtRetrieve.from_env()`; bind `JobKind.CAPTURE_VMCORE` to a closure calling `capture_handler(conn, job, r)`.

  Keep each function ≤100 lines / complexity ≤8 (split the handler's capture-and-finalize into a `_finalize_capture` helper like `runs._finalize_build`). Type SQL strings as `LiteralString`.

- [ ] **Step 4:** Run `uv run python -m pytest tests/mcp/test_vmcore_tools.py -q`. Expected: PASS. Fix until green.

- [ ] **Step 5:** Guardrails green; commit:

```bash
git add src/kdive/mcp/tools/vmcore.py tests/mcp/test_vmcore_tools.py
git commit -m "feat(vmcore): add vmcore.*/postmortem.* tools + capture handler (#24)"
git log -1 --oneline
```

## Task 6: Wire the plane into `app.py`

**Files:** Modify `src/kdive/mcp/app.py:21-29,32-51`, `tests/mcp/test_app.py` (spec §Canonical surface; ADR-0031 §Consequences)

- [ ] **Step 1 (test first):** In `tests/mcp/test_app.py`, add (or extend the existing tool-listing test) assertions that `await app.list_tools()` includes `vmcore.fetch`, `vmcore.list`, `artifacts.list`, `artifacts.get`, `postmortem.crash`, `postmortem.triage`, and that `build_handler_registry().get(JobKind.CAPTURE_VMCORE) is not None`. Run; expect FAIL (tools/handler absent).

- [ ] **Step 1b (surface-wide redaction guard):** In `tests/mcp/test_vmcore_tools.py`, add `test_no_raw_vmcore_key_in_any_read_response`: seed a crashed System, run `capture_handler` to land both rows, then collect every ref from `list_vmcores`, `artifacts_list`, and `artifacts_get` over each listed id, and assert **no** returned `refs` value ends in `/vmcore` (the raw `sensitive` key) — only `/vmcore-redacted` keys appear. This is the load-bearing redaction property of the plane, asserted at the surface (not just per-tool). Run with the file; expect PASS after Task 5's code. (Placed here because it exercises the registered read surface end-to-end.)

- [ ] **Step 2:** Edit `src/kdive/mcp/app.py`:
  - add `artifacts,` and `vmcore,` to the `from kdive.mcp.tools import (...)` block (alphabetical: `artifacts` first, `vmcore` last);
  - append `artifacts.register,` and `vmcore.register,` to `_PLANE_REGISTRARS`;
  - append `vmcore.register_handlers,` to `_HANDLER_REGISTRARS`;
  - update the seam-comment to mention the retrieve plane (#24) registering the `capture_vmcore` handler.

- [ ] **Step 3:** Run `uv run python -m pytest tests/mcp/test_app.py -q`. Expected: PASS.

- [ ] **Step 4:** Full guardrails: `uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest -q`. All green.

- [ ] **Step 5:** Commit:

```bash
git add src/kdive/mcp/app.py tests/mcp/test_app.py
git commit -m "feat(mcp): register retrieve plane tools + capture handler (#24)"
git log -1 --oneline
```

## Task 7: Full-suite verification

- [ ] **Step 1:** `uv run python -m pytest -q` — entire suite green (the `live_vm` real-path tests skip).
- [ ] **Step 2:** `uv run ruff check && uv run ruff format --check && uv run ty check src` — zero warnings.
- [ ] **Step 3:** `git log --oneline main..HEAD` — confirm the commit series is clean and each subject ≤72 chars.

---

## Self-review notes

- **Spec coverage:** `vmcore.fetch`/capture (Tasks 2,3,5), `vmcore.list` redacted-only (Task 5), `artifacts.list`/`.get` redacted-only (Task 4), `postmortem.crash`/`.triage` (Tasks 1,3,5), `readiness_failure` no-core (Tasks 2,5), object-before-row + lock-guarded idempotency (Tasks 2,5), build-id provenance (Tasks 3,5), redaction before return + persist (Tasks 2,3,5) plus a surface-wide "no raw vmcore key in any read response" guard (Task 6 Step 1b), registration (Task 6). All spec sections map to a task.
- **Per-task green commits:** every task is self-contained — no committed file forward-references a later task's symbol (Task 2's `__init__` is capture-only; Task 3 widens it additively with `None`-defaulted crash seams), and the shared test seeds live in `tests/mcp/_seed.py` (Task 4 Step 0) so Tasks 4 and 5 are each green at their own commit with no cross-test-module import.
- **Idempotency note:** the capture handler runs the slow `capture` seam with no transaction held, then finalizes under the lock with an existing-row re-check — the `build_handler` shape. Both the admission dedup_key and the existing-row check are exercised in Task 5.
- **Shared-file edit:** only `app.py` (two registrar appends + one handler append). Flag in NOTES; a sibling (#19) may touch the same tuples — keep the appends minimal and rebase-friendly.
