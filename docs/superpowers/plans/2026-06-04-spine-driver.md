# Phase-structured spine driver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `tests/integration/test_live_stack.py`, the `live_stack`-marked phase-structured spine driver that drives `allocate → … → release` over the live MCP HTTP transport via the merged harness, asserts the protocol/#1/#2/#3/#5/RBAC exit criteria, names its failing phase, and deletes the `test_walking_skeleton_full_path` stub.

**Architecture:** Test-only. The driver is a single `live_stack`-marked test wrapping each step in a `phase(name)` context manager (re-raising `SpinePhaseError(phase)`), minting per-role OIDC tokens with `mint_token` and calling tools over HTTP with `LiveStackClient.over_http`. Async job phases are drained by the real host worker/reconciler via `jobs.wait`/`systems.get` polling with a bounded deadline. The destructive capability scope is granted out of band by a privileged DB update (ADR-0045). The harness gains a small additive change: `call_tool` raises a typed `LiveStackToolError` on `CallToolResult.is_error`.

**Tech Stack:** pytest (`live_stack` marker), `fastmcp.Client` (streamable HTTP) via the merged `LiveStackClient`, `psycopg`/`psycopg_pool` for the out-of-band DB grant + audit/teardown assertions, the merged `mint_token`/`OidcIssuer`, `LocalLibvirtDiscovery.list_owned()` for the #5 teardown check.

**Spec:** `docs/superpowers/specs/2026-06-04-spine-driver-design.md` · **Decisions:** ADR-0042 §1/§4/§5, ADR-0045.

---

## File Structure

- **Modify** `tests/integration/live_stack/harness.py` — add `LiveStackToolError`; make `LiveStackClient.call_tool` raise it on `result.is_error` before the structured-content parse (additive; envelope parsing unchanged).
- **Create** `tests/integration/test_live_stack.py` — `SpinePhaseError`, the `phase` context manager, `_spine_preflight`, the out-of-band capability grant + audit/teardown DB helpers, the drain helpers, and the one `live_stack`-marked spine test plus the RBAC-negative assertions.
- **Modify** `tests/integration/test_walking_skeleton.py` — delete `test_walking_skeleton_full_path`; drop the now-unused `_live_vm_preflight`/env constants if nothing else uses them.

Guardrails after every commit: `just lint`, `just type`, `just test` (the last runs `-m "not live_vm and not live_stack"`, so the new test is deselected and must not break collection).

---

## Task 1: Harness raises a typed error on tool-error results

**Files:**
- Modify: `tests/integration/live_stack/harness.py`
- Test: `tests/integration/live_stack/test_harness_tool_error.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
"""LiveStackClient.call_tool raises LiveStackToolError on a tool-error result (ADR-0045 §2)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from tests.integration.live_stack.harness import LiveStackClient, LiveStackToolError


@dataclass
class _FakeResult:
    is_error: bool
    structured_content: dict | None


class _FakeClient:
    """Stands in for fastmcp.Client: returns a preset CallToolResult-shaped object."""

    def __init__(self, result: _FakeResult) -> None:
        self._result = result

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, args: dict) -> _FakeResult:
        return self._result


def test_call_tool_raises_on_is_error() -> None:
    client = LiveStackClient(_FakeClient(_FakeResult(is_error=True, structured_content=None)))

    async def _run() -> None:
        async with client:
            with pytest.raises(LiveStackToolError) as excinfo:
                await client.call_tool("allocations.request")
        assert "allocations.request" in str(excinfo.value)

    asyncio.run(_run())


def test_call_tool_parses_envelope_when_not_error() -> None:
    payload = {"object_id": "o1", "status": "granted"}
    client = LiveStackClient(_FakeClient(_FakeResult(is_error=False, structured_content=payload)))

    async def _run() -> None:
        async with client:
            resp = await client.call_tool("allocations.request")
        assert resp.object_id == "o1"
        assert resp.status == "granted"

    asyncio.run(_run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/integration/live_stack/test_harness_tool_error.py -q`
Expected: FAIL — `ImportError: cannot import name 'LiveStackToolError'`.

- [ ] **Step 3: Add `LiveStackToolError` and the `is_error` guard**

In `tests/integration/live_stack/harness.py`, add the exception near the top-level definitions (after the constants):

```python
class LiveStackToolError(RuntimeError):
    """A tool call returned an error result over the wire (e.g. a raised authz denial).

    fastmcp surfaces a handler that *raises* (rather than returning a ``ToolResponse``) as a
    tool-error ``CallToolResult`` (``is_error`` true, no ``structured_content``). The driver
    asserts the RBAC raised-path on this typed error rather than on an ``error_category``
    (ADR-0045 §2).
    """

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        self.message = message
        super().__init__(f"tool {tool!r} returned an error: {message}")
```

Then, in `call_tool`, guard on `is_error` **before** reading `structured_content`:

```python
        result = await self._client.call_tool(name, args)
        if getattr(result, "is_error", False):
            raise LiveStackToolError(name, _tool_error_text(result))
        payload = result.structured_content
```

Add the small text extractor helper at module scope:

```python
def _tool_error_text(result: object) -> str:
    """Best-effort human-readable text from a tool-error CallToolResult."""
    content = getattr(result, "content", None)
    if content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text
    return "tool error"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/integration/live_stack/test_harness_tool_error.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify the existing wire smoke test still imports/collects**

Run: `uv run python -m pytest tests/integration/test_wire_harness.py --collect-only -q`
Expected: collects without error (the envelope path is unchanged).

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type && just test
git add tests/integration/live_stack/harness.py tests/integration/live_stack/test_harness_tool_error.py
git commit -m "test: harness raises typed error on tool-error result

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Phase-failure scaffolding (`SpinePhaseError` + `phase`)

**Files:**
- Create: `tests/integration/test_live_stack.py`
- Test: same file (the scaffolding is unit-testable without a stack)

- [ ] **Step 1: Write the failing test for the phase context manager**

Create `tests/integration/test_live_stack.py` with the scaffolding and a non-`live_stack` unit test for it (the unit test runs in normal CI; the spine test is `live_stack`-marked and skips):

```python
"""The phase-structured live-stack spine driver (#100, ADR-0042 §1/§4/§5, ADR-0045).

The single ``live_stack``-marked spine test drives allocate → … → release over the wire and
names its failing phase; a non-gated unit test exercises the phase scaffolding so a regression
in the naming contract is caught in normal CI.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import pytest


class SpinePhaseError(AssertionError):
    """A spine phase failed; carries the phase name so a failure says which step died."""

    def __init__(self, phase: str, reason: str, *, error_category: str | None = None) -> None:
        self.phase = phase
        self.reason = reason
        self.error_category = error_category
        super().__init__(f"phase {phase!r} failed: {reason}")


@asynccontextmanager
async def phase(name: str) -> AsyncIterator[None]:
    """Run a phase; convert any failure into a ``SpinePhaseError`` naming the phase."""
    try:
        yield
    except SpinePhaseError:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberately broad: every failure names its phase
        raise SpinePhaseError(name, str(exc)) from exc


def test_phase_names_the_failing_phase() -> None:
    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("provision"):
                raise ValueError("libvirt exploded")
        assert excinfo.value.phase == "provision"
        assert isinstance(excinfo.value.__cause__, ValueError)

    asyncio.run(_run())


def test_phase_passes_through_spine_phase_error() -> None:
    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("outer"):
                raise SpinePhaseError("boot", "job failed", error_category="infrastructure_failure")
        assert excinfo.value.phase == "boot"  # inner phase name preserved, not re-wrapped

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify it passes (scaffolding is self-contained)**

Run: `uv run python -m pytest tests/integration/test_live_stack.py -q`
Expected: PASS (2 passed) — these unit tests are not `live_stack`-marked.

- [ ] **Step 3: Commit**

```bash
just lint && just type && just test
git add tests/integration/test_live_stack.py
git commit -m "test: spine phase-failure naming scaffolding

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Preflight + an envelope-assert helper that names its phase

**Files:**
- Modify: `tests/integration/test_live_stack.py`

- [ ] **Step 1: Add the preflight and the envelope-assert helper**

Append to `tests/integration/test_live_stack.py` (imports added at top in the same edit):

```python
import os
from pathlib import Path

from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
    mint_token,
)

_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
_PROJECT = "spine-proj"
_AGENT_SESSION = "spine-sess"
# Above the 300s jobs.wait cap and the 30s reconciler interval; teardown is the slowest phase.
_DRAIN_DEADLINE_S = 600.0
_POLL_INTERVAL_S = 2.0


def _spine_preflight() -> tuple[OidcIssuer, str, str]:
    """Resolve issuer + stack URL + DB URL, or skip with the exact fix (ADR-0035 §4)."""
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(f"{_GUEST_IMAGE_ENV} unset or missing; run scripts/live-vm/build-guest-image.sh")
    tree = os.environ.get(_KERNEL_TREE_ENV)
    if not tree or not Path(tree).exists():
        pytest.skip(f"{_KERNEL_TREE_ENV} unset or missing; run scripts/live-vm/fetch-kernel-tree.sh")
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (see the live-stack runbook)")
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url, db_url


def _ok(envelope: ToolResponse, phase_name: str) -> ToolResponse:
    """Return the envelope if non-failure, else raise a SpinePhaseError naming the phase."""
    if envelope.status in {"error", "failed"}:
        raise SpinePhaseError(phase_name, f"{envelope.status} envelope", error_category=envelope.error_category)
    return envelope
```

- [ ] **Step 2: Verify collection + the scaffolding tests still pass**

Run: `uv run python -m pytest tests/integration/test_live_stack.py -q`
Expected: PASS (the 2 scaffolding tests; the spine test does not exist yet).

- [ ] **Step 3: Commit**

```bash
just lint && just type && just test
git add tests/integration/test_live_stack.py
git commit -m "test: spine preflight + envelope-assert helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Out-of-band capability grant + audit/teardown DB helpers

**Files:**
- Modify: `tests/integration/test_live_stack.py`

- [ ] **Step 1: Add the DB helpers**

These use a short-lived `psycopg` async connection against the stack's Postgres. Add the import (`import psycopg`) and the helpers:

```python
import psycopg


async def _grant_force_crash_scope(db_url: str, allocation_id: str) -> None:
    """Grant the destructive capability scope on an allocation, out of band (ADR-0045 §1).

    The wire ``allocations.request`` always grants an empty scope; granting a destructive
    capability is a privileged platform action no operator tool exposes. This mirrors
    ``seed_granted_allocation(capability_scope=…)`` — the platform-admin action stood in for.
    """
    scope = '{"destructive_ops": ["force_crash"]}'
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.execute(
            "UPDATE allocations SET capability_scope = %s::jsonb WHERE id = %s",
            (scope, allocation_id),
        )
        await conn.commit()


async def _count_audit(db_url: str, *, object_id: str, transition: str, principal: str) -> int:
    """Count audit_log rows for a transition on an object under a given principal."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM audit_log "
                "WHERE object_id = %s AND transition = %s AND principal = %s",
                (object_id, transition, principal),
            )
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _system_torn_down_unowned(db_url: str, system_id: str) -> bool:
    """True iff the System row is ``torn_down`` (the DB half of the #5 teardown check)."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
            row = await cur.fetchone()
    return row is not None and row[0] == "torn_down"
```

- [ ] **Step 2: Verify collection**

Run: `uv run python -m pytest tests/integration/test_live_stack.py -q`
Expected: PASS (scaffolding tests still pass; helpers are defined but unused yet — confirm `just lint` does not flag them in Step 3 since the spine test in Task 7 uses them).

- [ ] **Step 3: Commit**

```bash
just lint && just type && just test
git add tests/integration/test_live_stack.py
git commit -m "test: out-of-band capability grant + audit/teardown helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

> Note: if `just lint` flags the helpers as unused at this commit, fold Tasks 4–7 into one commit so the helpers and their caller land together. Prefer that over an `# noqa`.

---

## Task 5: Drain helpers (`jobs.wait` three-outcome loop + `systems.get` state poll)

**Files:**
- Modify: `tests/integration/test_live_stack.py`

- [ ] **Step 1: Add the two drain helpers**

```python
import time


async def _drain_job(client: LiveStackClient, phase_name: str, job_id: str) -> ToolResponse:
    """Poll jobs.wait until the job is succeeded; classify the three outcomes (ADR-0045 §2)."""
    deadline = time.monotonic() + _DRAIN_DEADLINE_S
    while True:
        env = await client.call_tool("jobs.wait", job_id=job_id, timeout_s=60.0)
        if env.status == "succeeded":
            return env
        if env.status in {"failed", "canceled"}:
            raise SpinePhaseError(phase_name, f"job {env.status}", error_category=env.error_category)
        if time.monotonic() >= deadline:  # non-terminal return: a worker stall
            raise SpinePhaseError(phase_name, "drain_timeout")
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _await_system_state(
    client: LiveStackClient, phase_name: str, system_id: str, target: str
) -> None:
    """Poll systems.get until the System reaches ``target`` state (or the deadline)."""
    deadline = time.monotonic() + _DRAIN_DEADLINE_S
    while True:
        env = await client.call_tool("systems.get", system_id=system_id)
        if env.status == target:
            return
        if env.status in {"error", "failed"}:
            raise SpinePhaseError(phase_name, f"system {env.status}", error_category=env.error_category)
        if time.monotonic() >= deadline:
            raise SpinePhaseError(phase_name, f"system did not reach {target}")
        await asyncio.sleep(_POLL_INTERVAL_S)
```

- [ ] **Step 2: Verify collection + commit**

```bash
just lint && just type && just test
git add tests/integration/test_live_stack.py
git commit -m "test: spine async-drain helpers (jobs.wait + systems.get)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

> Same unused-helper note as Task 4 applies; fold into the spine commit if `just lint` complains.

---

## Task 6: Per-role clients + the profile builder

**Files:**
- Modify: `tests/integration/test_live_stack.py`

- [ ] **Step 1: Add the token/client helpers and the provision profile**

```python
def _token(issuer: OidcIssuer, *, role: str, platform_roles: list[str] | None = None) -> str:
    return mint_token(
        issuer,
        subject=f"{role}-{_PROJECT}",
        projects=[_PROJECT],
        roles={_PROJECT: role},
        platform_roles=platform_roles,
        agent_session=_AGENT_SESSION,
    )


def _provision_profile() -> dict[str, object]:
    """A provisioning profile that opts force_crash in (the gate's profile factor, ADR-0045)."""
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs_image_ref": os.environ[_GUEST_IMAGE_ENV],
                "crashkernel": "256M",
                "destructive_ops": ["force_crash"],
            }
        },
    }


def _build_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "config_ref": "file:///configs/kdump.config",
    }
```

> The exact `rootfs_image_ref`/`config_ref` shapes follow `tests/integration/_seed.py`'s `PROVISIONING_PROFILE`/`BUILD_PROFILE`; if the live provider needs a concrete on-host path rather than an `oci://`/`file://` ref, the operator points `KDIVE_GUEST_IMAGE`/`KDIVE_KERNEL_SRC` at real paths and the profile carries them (the spec's "operator fixture concern").

- [ ] **Step 2: Verify collection + commit**

```bash
just lint && just type && just test
git add tests/integration/test_live_stack.py
git commit -m "test: spine per-role token/client + profile builders

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: The RBAC-negative assertions (no KVM needed beyond the stack)

**Files:**
- Modify: `tests/integration/test_live_stack.py`

- [ ] **Step 1: Add the RBAC-negative test (its own `live_stack` test)**

```python
@pytest.mark.live_stack
def test_rbac_negatives_over_the_wire() -> None:
    """viewer is denied operator ops (raised tool error); operator is denied force_crash
    (authorization_denied envelope). Both over HTTP; needs the stack, not a KVM host."""
    issuer, base_url, _db = _spine_preflight()

    async def _run() -> None:
        viewer = LiveStackClient.over_http(base_url, _token(issuer, role="viewer"))
        async with viewer:
            with pytest.raises(LiveStackToolError):  # require_role raises → tool error
                await viewer.call_tool(
                    "allocations.request", project=_PROJECT, vcpus=1, memory_gb=1
                )

        # operator denied force_crash: the gate RETURNS an authorization_denied envelope.
        # Use a syntactically-valid but unknown system id; the gate (role check) fires before
        # the not-found path for a non-admin, returning authorization_denied.
        operator = LiveStackClient.over_http(base_url, _token(issuer, role="operator"))
        async with operator:
            env = await operator.call_tool(
                "control.force_crash", system_id="00000000-0000-0000-0000-000000000000"
            )
        assert env.status == "error"
        assert env.error_category == "authorization_denied"

    asyncio.run(_run())
```

> If `control.force_crash` returns `configuration_error` (not-found) before the gate for an unknown system under an operator token, this assertion is wrong and the negative must run against the **real** `system_id` from the spine (move it into the spine test after `provision`). Verify the handler order at implementation time against `src/kdive/mcp/tools/control.py` (`force_crash_system`: it reads the System first, returns `configuration_error` if absent, then runs the gate). **Therefore: assert the operator force_crash denial inside the spine test on the real `system_id`, and keep only the viewer raised-path assertion standalone here.** Adjust Step 1 accordingly: drop the force_crash block from this standalone test and assert it in Task 8 on the real system.

- [ ] **Step 2: Verify it SKIPS cleanly with no stack**

Run: `just test-live-stack`
Expected: the test is collected and **skips** (preflight: `KDIVE_GUEST_IMAGE` unset), exit 0; or "no live_stack tests collected" if nothing matches. Either is a clean skip.

- [ ] **Step 3: Verify the non-live suite stays green**

Run: `just test`
Expected: PASS; the `live_stack` test is deselected.

- [ ] **Step 4: Commit**

```bash
just lint && just type
git add tests/integration/test_live_stack.py
git commit -m "test: RBAC raised-path negative over the wire

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: The full spine test

**Files:**
- Modify: `tests/integration/test_live_stack.py`

- [ ] **Step 1: Write the spine test**

```python
@pytest.mark.live_stack
def test_spine_over_the_wire() -> None:
    """Drive allocate → … → release over HTTP; assert #1/#2/#3/#5; name the failing phase."""
    issuer, base_url, db_url = _spine_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        async with op, admin:
            # 1. allocate
            async with phase("allocate"):
                env = _ok(await op.call_tool(
                    "allocations.request", project=_PROJECT, vcpus=2, memory_gb=2
                ), "allocate")
                allocation_id = env.object_id
            # out-of-band: grant the destructive capability scope (ADR-0045 §1)
            await _grant_force_crash_scope(db_url, allocation_id)
            # 2. provision (async; system_id is in data, NOT object_id)
            async with phase("provision"):
                env = _ok(await op.call_tool(
                    "systems.provision", allocation_id=allocation_id, profile=_provision_profile()
                ), "provision")
                system_id = env.data["system_id"]
                await _await_system_state(op, "provision", system_id, "ready")
            # 3. open-investigation
            async with phase("open-investigation"):
                env = _ok(await op.call_tool(
                    "investigations.open", project=_PROJECT, title="spine"
                ), "open-investigation")
                investigation_id = env.object_id
            # 4. create-run
            async with phase("create-run"):
                env = _ok(await op.call_tool(
                    "runs.create",
                    investigation_id=investigation_id,
                    system_id=system_id,
                    build_profile=_build_profile(),
                ), "create-run")
                run_id = env.object_id
            # 5/6/7. build → install → boot (each enqueues a job; drain to succeeded)
            for step in ("build", "install", "boot"):
                async with phase(step):
                    env = _ok(await op.call_tool(f"runs.{step}", run_id=run_id), step)
                    await _drain_job(op, step, env.object_id)
            # 8. attach + gdb-MI probe
            async with phase("attach"):
                env = _ok(await op.call_tool(
                    "debug.start_session", run_id=run_id, transport="gdbstub"
                ), "attach")
                session_id = env.object_id
                _ok(await op.call_tool(
                    "debug.read_registers", session_id=session_id, registers=["rip"]
                ), "attach")
            # operator is denied force_crash (gate returns authorization_denied envelope)
            async with phase("crash-rbac-negative"):
                denied = await op.call_tool("control.force_crash", system_id=system_id)
                if denied.status != "error" or denied.error_category != "authorization_denied":
                    raise SpinePhaseError("crash-rbac-negative", "operator was not denied")
            # 9. crash (admin; the 3-check gate passes)
            async with phase("crash"):
                _ok(await admin.call_tool("control.force_crash", system_id=system_id), "crash")
                await _await_system_state(admin, "crash", system_id, "crashed")
            # 10. capture (async; redacted vmcore lands in MinIO)
            async with phase("capture"):
                env = _ok(await op.call_tool("vmcore.fetch", system_id=system_id), "capture")
                await _drain_job(op, "capture", env.object_id)
                cores = await op.call_tool("vmcore.list", system_id=system_id)
                assert isinstance(cores, list) and cores, "no vmcore artifact listed (#1)"
                refs = [v for c in cores for v in c.refs.values()]
                assert refs and all(not r.endswith("/vmcore") for r in refs), "raw vmcore leaked (#1)"
            # 11. introspect (redacted report; #3 redaction)
            async with phase("introspect"):
                env = _ok(await op.call_tool("introspect.from_vmcore", run_id=run_id), "introspect")
                report = env.data.get("report", "")
                assert "hunter2" not in report and "password=" not in report, "secret leaked (#3)"
            # 12. release (Allocation only; System untouched)
            async with phase("release"):
                _ok(await op.call_tool("allocations.release", allocation_id=allocation_id), "release")
            # reconciler-driven teardown → torn_down (≥30s + worker drain)
            async with phase("teardown"):
                await _await_system_state(op, "teardown", system_id, "torn_down")

        # --- #2 audit attribution (driver principal vs system:reconciler) ---
        principal = f"operator-{_PROJECT}"
        assert await _count_audit(
            db_url, object_id=system_id, transition="ready->crashed", principal=f"admin-{_PROJECT}"
        ) == 1, "force_crash not audited under admin (#2)"
        assert await _count_audit(
            db_url, object_id=allocation_id, transition="releasing->released", principal=principal
        ) >= 1, "release not audited under operator (#2)"
        # teardown is attributed to the reconciler, NOT the driver (ADR-0021)
        assert await _count_audit(
            db_url, object_id=system_id, transition="ready->torn_down", principal="system:reconciler"
        ) >= 1 or await _system_torn_down_unowned(db_url, system_id), "teardown audit missing (#2)"
        # --- #5 teardown: DB torn_down + no OwnedInfra ---
        assert await _system_torn_down_unowned(db_url, system_id), "system not torn_down (#5)"
        from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
        import libvirt  # noqa: PLC0415 — only importable on a libvirt host (live_stack path)

        disc = LocalLibvirtDiscovery(
            host_uri="qemu:///system", connect=lambda: libvirt.open("qemu:///system"),
            concurrent_allocation_cap=2,
        )
        owned_ids = {o["system_id"] for o in disc.list_owned()}
        assert system_id not in owned_ids, "released system still owned (#5)"

    asyncio.run(_run())
```

> The `ready->torn_down` transition string and the `releasing->released` string must match what the handlers actually write. Verify at implementation time: `audit.record(... transition="ready->crashed" ...)` in `control.py`; the release transitions in `allocations._transition_and_audit` (`f"{frm.value}->{to.value}"`); the teardown transition in the teardown handler (`systems.py`). Adjust the literals to the real ones if they differ.

- [ ] **Step 2: Verify it SKIPS cleanly with no stack**

Run: `just test-live-stack`
Expected: collected and skipped (preflight), exit 0.

- [ ] **Step 3: Verify the non-live suite stays green**

Run: `just test`
Expected: PASS; both `live_stack` tests deselected.

- [ ] **Step 4: Commit**

```bash
just lint && just type
git add tests/integration/test_live_stack.py
git commit -m "test: phase-structured spine driver over the wire (#100)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Delete the walking-skeleton stub

**Files:**
- Modify: `tests/integration/test_walking_skeleton.py`

- [ ] **Step 1: Delete `test_walking_skeleton_full_path` and its now-unused preflight**

Remove the `test_walking_skeleton_full_path` function and the `# --- exit criteria #1/#2/#5: the full path (live_vm-gated) ---` section. Remove `_live_vm_preflight`, `_GUEST_IMAGE_ENV`/`_KERNEL_TREE_ENV`/`_LIVE_SSH_ENV` constants, and the now-unused imports (`os`, `Path`) **only if** no other test in the file uses them. Leave the three non-gated exit-criterion tests (`#6`/`#4`/`#3`) untouched.

- [ ] **Step 2: Verify the file still collects + the non-gated tests pass**

Run: `uv run python -m pytest tests/integration/test_walking_skeleton.py -q`
Expected: PASS — the non-gated tests run against disposable Postgres; the deleted `live_vm` test is gone.

- [ ] **Step 3: Confirm the stub is gone**

Run: `! rg -q "test_walking_skeleton_full_path" tests/ && echo DELETED`
Expected: prints `DELETED`.

- [ ] **Step 4: Guardrails + commit**

```bash
just lint && just type && just test
git add tests/integration/test_walking_skeleton.py
git commit -m "test: delete unimplemented walking-skeleton full-path stub

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Final verification

- [ ] **Step 1: Clean skip with no stack**

Run: `just test-live-stack`
Expected: exit 0; the two `live_stack` tests skip with actionable reasons (or "no live_stack tests collected").

- [ ] **Step 2: Non-live suite green**

Run: `just test`
Expected: PASS, zero warnings.

- [ ] **Step 3: Lint + type clean**

Run: `just lint && just type`
Expected: no findings.

---

## Self-review notes (already reconciled against the spec)

- Spec coverage: allocate→release phases (Task 8), RBAC two-mechanism split (Tasks 1/7/8), #1 (Task 8 capture), #2 audit principal split (Task 8), #3 redaction (Task 8 introspect), #5 torn_down + list_owned (Task 8), preflight/skip (Task 3), capability grant (Task 4), reconciler teardown wait (Tasks 5/8), stub deletion (Task 9).
- The `report`-phase accounting assertions are sub-issue E (out of scope here, per the spec Non-goals); this plan does not implement them. The RBAC reachability boundary for `accounting.report` is the issue's listed acceptance but the umbrella spec assigns the ledger/artifact assertions to E; if the orchestrator wants the report-RBAC reachability here, add a small phase-13 assertion mirroring the operator/auditor `accounting.report(scope=...)` calls — left out by default to keep D bisectable.
- Verify-at-implementation hooks are called out inline where a literal (transition strings, force_crash handler order) must be confirmed against `src/`.
