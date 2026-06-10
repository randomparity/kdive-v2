# Remote live-stack e2e + portability report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver M2 issue 8 (the capstone): an operator-run remote-libvirt spine e2e mirroring M1.2, an operator runbook for the remote stack, and the committed milestone-end portability report — all without touching the gate's core surface.

**Architecture:** Extract the provider-agnostic spine scaffolding from the local live-stack test into a shared `tests/integration/live_stack/spine.py`; both the local and a new remote spine import it. The remote spine selects the `remote-libvirt` resource by kind, uses the disk-image provision profile, and asserts the two-phase vmcore capture + worker-side postmortem. The portability report is generated from the existing gate via a new `--report` flag. Every change lives under `tests/`, `docs/`, `scripts/`, `justfile` — **zero** core-surface touches, so the M2 portability gate stays green.

**Tech Stack:** Python 3.13, pytest (`live_stack` marker, deselected in CI), `uv`/`ruff`/`ty`, `just` recipes, the existing `LiveStackClient` + mock-OIDC harness (ADR-0042/0044).

**Spec:** [docs/superpowers/specs/2026-06-09-remote-live-stack-e2e-207.md](../specs/2026-06-09-remote-live-stack-e2e-207.md)

**Guardrails (run before every commit):** `just lint` · `just type` · `just test` · `just m2-gate` · (docs tasks also) `just check-mermaid`. The prose guard: no `critical/crucial/essential/significant/comprehensive/robust/elegant/Sprint` in any doc/comment/commit. Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- `tests/integration/live_stack/spine.py` — **new**. The shared, provider-agnostic spine scaffolding (phase naming, drain/state polling, role-token minting, out-of-band DB seeding, audit/ledger/report helpers). Owns the contract both spines reuse.
- `tests/integration/live_stack/test_spine.py` — **new**. The non-gated unit tests for the `phase` naming contract (moved out of `test_live_stack.py`).
- `tests/integration/test_live_stack.py` — **modify**. Delete the moved helpers; import them from `spine.py`. Keep the local-libvirt profile factories, the local spine body, the RBAC-negative wire tests, and the local owned-infra teardown check.
- `tests/integration/test_remote_live_stack.py` — **new**. The remote spine: preflight, remote disk-image profile factory, the `live_stack`-marked spine body, and two non-gated unit tests (preflight skip, profile validates).
- `scripts/m2_portability_gate.py` — **modify**. Add a `--report` flag rendering the measurement as markdown (pure function over the `touched` dict). Default invocation unchanged.
- `tests/scripts/test_m2_portability_gate.py` — **modify**. Add a unit test for the markdown renderer.
- `docs/reports/m2-portability.md` — **new** (generated). The committed milestone-end measurement.
- `docs/runbooks/remote-live-stack.md` — **new**. Operator bring-up for the remote stack.
- `docs/runbooks/live-stack.md` — **modify**. Add a pointer to the remote runbook.
- `justfile` — **modify**. Add the `m2-report` recipe.

---

## Task 1: Extract shared spine scaffolding into `spine.py`

**Files:**
- Create: `tests/integration/live_stack/spine.py`
- Create: `tests/integration/live_stack/test_spine.py`
- Modify: `tests/integration/test_live_stack.py`

The current `test_live_stack.py` defines its spine helpers inline. Move the provider-agnostic ones into `spine.py` so a second spine reuses one copy. Two signatures change: `drain_job` and `await_system_state` gain an optional `deadline_s` so the remote capture phase can budget for a slow reboot (spec §capture); `mint_role_token`, `seed_metering`, `assert_report`, `write_report_artifact`, `assert_audit` gain explicit `project` / `artifact_name` parameters (they hardcoded the local `_PROJECT` / artifact name).

- [ ] **Step 1: Create `spine.py` with the shared helpers**

Move these symbols **verbatim** from `test_live_stack.py` into `spine.py`, applying the signature deltas below. Keep their docstrings and bodies. Source line ranges are in the current `test_live_stack.py`.

Move as-is (rename leading `_` → public; update internal call sites):
- `SpinePhaseError` (class) and `phase` (asynccontextmanager).
- `_ok` → `ok`, `_scalar` → `scalar`.
- `_db_now` → `db_now`, `_ledger_sums` → `ledger_sums`, `_count_audit` → `count_audit`, `_count_audit_suffix` → `count_audit_suffix`, `_system_torn_down` → `system_torn_down`.
- `_grant_force_crash_scope` → `grant_force_crash_scope`.
- `_report_artifact_dir` → `report_artifact_dir`, `_find_project_row` → `find_project_row`.
- The constants `_DRAIN_DEADLINE_S` → `DRAIN_DEADLINE_S = 600.0`, `_POLL_INTERVAL_S` → `POLL_INTERVAL_S = 2.0`.

Change these signatures while moving:

```python
# was _drain_job(client, phase_name, job_id) with module-constant deadline
async def drain_job(
    client: LiveStackClient,
    phase_name: str,
    job_id: str,
    *,
    deadline_s: float = DRAIN_DEADLINE_S,
) -> ToolResponse:
    """Poll jobs.wait until the job succeeds; classify the three outcomes (ADR-0045 §2).

    ``deadline_s`` is overridable so a longer phase (the remote two-phase capture, which
    waits out a server-side readiness window) can extend the drain budget.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        env = await client.call_tool("jobs.wait", job_id=job_id, timeout_s=60.0)
        assert isinstance(env, ToolResponse)
        if env.status == "succeeded":
            return env
        if env.status in {"failed", "canceled"}:
            raise SpinePhaseError(phase_name, f"job {env.status}", error_category=env.error_category)
        if time.monotonic() >= deadline:
            raise SpinePhaseError(phase_name, "drain_timeout")
        await asyncio.sleep(POLL_INTERVAL_S)


async def await_system_state(
    client: LiveStackClient,
    phase_name: str,
    system_id: str,
    target: str,
    *,
    deadline_s: float = DRAIN_DEADLINE_S,
) -> None:
    """Poll systems.get until the System reaches ``target`` state (or the deadline)."""
    deadline = time.monotonic() + deadline_s
    while True:
        env = await client.call_tool("systems.get", system_id=system_id)
        assert isinstance(env, ToolResponse)
        if env.status == target:
            return
        if env.status in {"error", "failed"}:
            raise SpinePhaseError(phase_name, f"system {env.status}", error_category=env.error_category)
        if time.monotonic() >= deadline:
            raise SpinePhaseError(phase_name, f"system did not reach {target}")
        await asyncio.sleep(POLL_INTERVAL_S)


def mint_role_token(
    issuer: OidcIssuer,
    *,
    project: str,
    agent_session: str,
    role: str,
    platform_roles: list[str] | None = None,
) -> str:
    """Mint a per-project role token (the local test's `_token`, parameterized by project)."""
    return mint_token(
        issuer,
        subject=f"{role}-{project}",
        projects=[project],
        roles={project: role},
        platform_roles=platform_roles,
        agent_session=agent_session,
    )


async def seed_metering(
    db_url: str,
    project: str,
    *,
    limit_kcu: str = "1000000",
    max_allocations: int = 4,
    max_systems: int = 4,
) -> None:
    """Seed the budget (limit-only) + quota rows admission requires, out of band."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.execute(
            "INSERT INTO budgets (project, limit_kcu) VALUES (%s, %s) "
            "ON CONFLICT (project) DO UPDATE SET limit_kcu = EXCLUDED.limit_kcu",
            (project, limit_kcu),
        )
        await conn.execute(
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, %s, %s) ON CONFLICT (project) DO UPDATE SET "
            "max_concurrent_allocations = EXCLUDED.max_concurrent_allocations, "
            "max_concurrent_systems = EXCLUDED.max_concurrent_systems",
            (project, max_allocations, max_systems),
        )
        await conn.commit()


def write_report_artifact(payload: dict[str, object], *, name: str) -> Path:
    """Write the report payload as ``name`` under the artifact dir; return its path."""
    path = report_artifact_dir() / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


async def assert_audit(db_url: str, *, project: str, allocation_id: str, system_id: str) -> None:
    """#2: audit per transition + force_crash, split by attributing principal."""
    assert (
        await count_audit(
            db_url, object_id=system_id, transition="ready->crashed", principal=f"admin-{project}"
        )
        == 1
    ), "force_crash not audited under admin (#2)"
    assert (
        await count_audit(
            db_url,
            object_id=allocation_id,
            transition="releasing->released",
            principal=f"operator-{project}",
        )
        >= 1
    ), "release not audited under operator (#2)"
    assert (
        await count_audit_suffix(
            db_url, object_id=system_id, suffix="->torn_down", principal="system:reconciler"
        )
        >= 1
    ), "teardown not audited under system:reconciler (#2)"


async def assert_report(
    base_url: str,
    auditor_token: str,
    db_url: str,
    window_start: datetime,
    *,
    project: str,
    artifact_name: str,
) -> None:
    """Drive accounting.report_all_projects under platform_auditor; assert windowed spend."""
    # Move the body of the local test's `_assert_report` here verbatim, replacing every
    # `_PROJECT` with `project`, every `_scalar`/`_ok`/`_find_project_row`/`_ledger_sums` with the
    # public names, and the `_write_report_artifact({...})` call with
    # `write_report_artifact({...}, name=artifact_name)`. Keep all assertions identical.
```

`spine.py` imports: `asyncio`, `json`, `os`, `tempfile`, `time`, `from collections.abc import AsyncIterator`, `from contextlib import asynccontextmanager`, `from datetime import datetime`, `from decimal import Decimal`, `from pathlib import Path`, `psycopg`, `from kdive.domain.cost import quantize_kcu`, `from kdive.mcp.responses import ToolResponse`, `from tests.integration.live_stack.harness import LiveStackClient, OidcIssuer, mint_token`. Keep the `_ARTIFACT_DIR_ENV` constant (`report_artifact_dir` reads it).

- [ ] **Step 2: Create `test_spine.py` with the phase-contract unit tests**

Move `test_phase_names_the_failing_phase` and `test_phase_passes_through_spine_phase_error` out of `test_live_stack.py` into this new file, importing `phase` and `SpinePhaseError` from `spine.py`:

```python
"""Non-gated unit tests for the shared spine phase-naming contract (ADR-0042 §4)."""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.live_stack.spine import SpinePhaseError, phase


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
        assert excinfo.value.phase == "boot"

    asyncio.run(_run())
```

- [ ] **Step 3: Re-point `test_live_stack.py` at `spine.py`**

In `test_live_stack.py`: delete the moved symbols (the two phase unit tests, `SpinePhaseError`, `phase`, `_ok`, `_scalar`, `_drain_job`, `_await_system_state`, `_grant_force_crash_scope`, `_seed_metering`, `_db_now`, `_ledger_sums`, `_count_audit`, `_count_audit_suffix`, `_system_torn_down`, `_report_artifact_dir`, `_write_report_artifact`, `_find_project_row`, `_assert_audit`, `_assert_report`, the drain constants, the `_SEED_*` constants, and the `_ARTIFACT_*` constants). Add:

```python
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    assert_audit,
    assert_report,
    await_system_state,
    drain_job,
    grant_force_crash_scope,
    mint_role_token,
    ok,
    phase,
    scalar,
    seed_metering,
    system_torn_down,
)
```

Update the local call sites: `_token(...)` → `mint_role_token(issuer, project=_PROJECT, agent_session=_AGENT_SESSION, role=..., platform_roles=...)` (keep the local `_token` as a thin wrapper if it reduces churn); `_scalar`→`scalar`, `_ok`→`ok`, `_drain_job`→`drain_job`, `_await_system_state`→`await_system_state`, `_seed_metering(db_url, _PROJECT)`→`seed_metering(db_url, _PROJECT)`, `_grant_force_crash_scope`→`grant_force_crash_scope`, `_assert_audit(db_url, allocation_id=…, system_id=…)`→`assert_audit(db_url, project=_PROJECT, allocation_id=…, system_id=…)`, the report phase →`assert_report(base_url, auditor_token, db_url, window_start, project=_PROJECT, artifact_name="accounting-report.json")`. Keep `_assert_teardown` local (it does the local-libvirt `list_owned()` check) but have it call the imported `system_torn_down`.

- [ ] **Step 4: Run the non-gated tests + type check**

Run: `uv run python -m pytest tests/integration/live_stack/test_spine.py -q && uv run python -m pytest tests/integration/test_live_stack.py -q`
Expected: the two phase unit tests PASS; the `live_stack`-marked tests in `test_live_stack.py` SKIP cleanly (no stack). No collection or import errors.

Run: `just type`
Expected: PASS (whole tree). Run `just lint`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/live_stack/spine.py tests/integration/live_stack/test_spine.py tests/integration/test_live_stack.py
git commit -m "test: extract shared live-stack spine scaffolding into spine.py

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: The remote spine e2e (`test_remote_live_stack.py`)

**Files:**
- Create: `tests/integration/test_remote_live_stack.py`
- Test (CI-runnable parts): the two non-gated unit tests below.

TDD: the unit tests (preflight skip, profile validates) run in normal CI and are written first; the `live_stack`-marked spine body is operator-run.

- [ ] **Step 1: Write the failing unit tests**

```python
"""Operator-run remote-libvirt spine e2e (#207, M2 issue 8; mirrors ADR-0042's local spine).

Drives allocate(remote-libvirt) -> provision(disk-image) -> build -> install -> boot ->
attach(gdb-MI direct TCP) -> force-crash -> two-phase vmcore capture -> introspect(from_vmcore)
-> release -> (reconciler) teardown -> accounting report, over the live MCP HTTP transport under
per-project role tokens. ``live_stack``-marked and preflighted to a clean skip unless the remote
provider config + a reachable stack + issuer + DB are all present (CI deselects ``live_stack``).
Two non-gated unit tests pin the preflight skip + the remote profile shape so a regression is
caught in normal CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdive.profiles.provisioning import ProvisioningProfile

_REMOTE_URI_ENV = "KDIVE_REMOTE_LIBVIRT_URI"
_BASE_IMAGE_ENV = "KDIVE_REMOTE_BASE_IMAGE_VOLUME"  # test/runbook input -> profile.base_image_volume
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_PROJECT = "remote-spine-proj"
_AGENT_SESSION = "remote-spine-sess"


def _remote_provision_profile() -> dict[str, object]:
    """The disk-image remote profile (ADR-0080); force_crash opted in (gate's profile factor)."""
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "boot_method": "disk-image",
        "kernel_source_ref": os.environ.get(_KERNEL_TREE_ENV, "git+https://git.kernel.org/x#v6.9"),
        "provider": {
            "remote-libvirt": {
                "base_image_volume": os.environ.get(_BASE_IMAGE_ENV, "kdive-base.qcow2"),
                "crashkernel": "256M",
                "destructive_ops": ["force_crash"],
            }
        },
    }


def test_remote_provision_profile_validates() -> None:
    """The remote profile factory parses through the real validator (disk-image<->remote pairing)."""
    profile = ProvisioningProfile.parse(_remote_provision_profile())
    assert profile.boot_method.value == "disk-image"
    assert profile.provider.remote_libvirt_section.base_image_volume


def test_remote_preflight_skips_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the remote provider URI unset, the preflight skips with the actionable reason."""
    monkeypatch.delenv(_REMOTE_URI_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        _remote_spine_preflight()
    assert _REMOTE_URI_ENV in str(excinfo.value)
```

- [ ] **Step 2: Run the unit tests to verify they fail**

Run: `uv run python -m pytest tests/integration/test_remote_live_stack.py -q`
Expected: `test_remote_provision_profile_validates` may PASS already; `test_remote_preflight_skips_without_config` FAILS with `NameError: _remote_spine_preflight` (not yet defined).

- [ ] **Step 3: Add the preflight + remote helpers**

```python
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import LiveStackClient, OidcIssuer
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    assert_report,
    await_system_state,
    drain_job,
    grant_force_crash_scope,
    mint_role_token,
    ok,
    phase,
    scalar,
    seed_metering,
)

_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
# The remote capture job waits out a 300s server-side readiness window (retrieve.py) while the
# guest reboots out of the kdump kernel, then uploads; budget the drain above that + the reboot.
_CAPTURE_DEADLINE_S = 900.0
_CERT_REF_ENVS = (
    "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF",
    "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
)


def _remote_spine_preflight() -> tuple[OidcIssuer, str, str]:
    """Resolve issuer + stack URL + DB URL for the remote spine, or skip with the exact fix."""
    if not os.environ.get(_REMOTE_URI_ENV):
        pytest.skip(
            f"{_REMOTE_URI_ENV} unset; configure the remote-libvirt host "
            "(see docs/runbooks/remote-live-stack.md)"
        )
    for ref_env in _CERT_REF_ENVS:
        if not os.environ.get(ref_env):
            pytest.skip(f"{ref_env} unset; stage the TLS cert refs (remote-live-stack runbook)")
    if not os.environ.get(_BASE_IMAGE_ENV):
        pytest.skip(f"{_BASE_IMAGE_ENV} unset; stage the base-OS volume (remote-live-stack runbook)")
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (remote-live-stack runbook)")
    issuer = require_issuer()
    base_url = require_stack()
    return issuer, base_url, db_url


def _token(issuer: OidcIssuer, *, role: str, platform_roles: list[str] | None = None) -> str:
    return mint_role_token(
        issuer, project=_PROJECT, agent_session=_AGENT_SESSION, role=role, platform_roles=platform_roles
    )


def _build_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kernel_source_ref": os.environ.get(_KERNEL_TREE_ENV, "git+https://git.kernel.org/x#v6.9"),
        "config": {"kind": "local", "path": "/configs/kdump.config"},
    }
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run python -m pytest tests/integration/test_remote_live_stack.py -q`
Expected: both unit tests PASS.

- [ ] **Step 5: Add the `live_stack`-marked remote spine body**

```python
@pytest.mark.live_stack
def test_remote_spine_over_the_wire() -> None:
    """Drive allocate(remote) -> ... -> report over HTTP; assert capture/introspect; name the phase."""
    import asyncio  # noqa: PLC0415

    issuer, base_url, db_url = _remote_spine_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
    auditor_token = _token(issuer, role="viewer", platform_roles=["platform_auditor"])

    async def _run() -> None:
        from tests.integration.live_stack.spine import db_now  # noqa: PLC0415

        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        system_id = allocation_id = run_id = ""
        async with op, admin:
            await seed_metering(db_url, _PROJECT)
            window_start = await db_now(db_url)
            async with phase("allocate"):
                env = ok(
                    await scalar(
                        op,
                        "allocations.request",
                        project=_PROJECT,
                        request={
                            "vcpus": 2,
                            "memory_gb": 2,
                            "disk_gb": 10,
                            "resource": {"mode": "kind", "kind": "remote-libvirt"},
                        },
                    ),
                    "allocate",
                )
                allocation_id = env.object_id
            await grant_force_crash_scope(db_url, allocation_id)
            async with phase("provision"):
                env = ok(
                    await scalar(
                        op, "systems.provision", allocation_id=allocation_id, profile=_remote_provision_profile()
                    ),
                    "provision",
                )
                system_id = env.data["system_id"]
                await await_system_state(op, "provision", system_id, "ready")
            async with phase("open-investigation"):
                env = ok(await scalar(op, "investigations.open", project=_PROJECT, title="remote-spine"), "open-investigation")
                investigation_id = env.object_id
            async with phase("create-run"):
                env = ok(
                    await scalar(
                        op, "runs.create", investigation_id=investigation_id, system_id=system_id, build_profile=_build_profile()
                    ),
                    "create-run",
                )
                run_id = env.object_id
            for step in ("build", "install", "boot"):
                async with phase(step):
                    env = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
                    await drain_job(op, step, env.object_id)
            async with phase("attach"):
                env = ok(await scalar(op, "debug.start_session", run_id=run_id, transport="gdbstub"), "attach")
                session_id = env.object_id
                ok(await scalar(op, "debug.read_registers", session_id=session_id, registers=["rip"]), "attach")
            async with phase("crash-rbac-negative"):
                denied = await scalar(op, "control.force_crash", system_id=system_id)
                if denied.status != "error" or denied.error_category != "authorization_denied":
                    raise SpinePhaseError("crash-rbac-negative", "operator was not denied")
            async with phase("crash"):
                ok(await scalar(admin, "control.force_crash", system_id=system_id), "crash")
                await await_system_state(admin, "crash", system_id, "crashed")
            async with phase("capture"):
                env = ok(await scalar(op, "vmcore.fetch", system_id=system_id), "capture")
                await drain_job(op, "capture", env.object_id, deadline_s=_CAPTURE_DEADLINE_S)
                cores = await op.call_tool("vmcore.list", system_id=system_id)
                assert isinstance(cores, list) and cores, "no vmcore artifact listed"
                refs = [v for c in cores for v in c.refs.values()]
                assert refs, "no vmcore refs"
                assert all(not ("/vmcore-" in r and not r.endswith("-redacted")) for r in refs), "raw vmcore leaked"
            async with phase("introspect"):
                env = ok(await scalar(op, "introspect.from_vmcore", run_id=run_id), "introspect")
                report = env.data.get("report", "")
                assert report, "empty postmortem report"
                assert "hunter2" not in report and "password=" not in report, "secret leaked (#3)"
            async with phase("release"):
                ok(await scalar(op, "allocations.release", allocation_id=allocation_id), "release")
            async with phase("teardown"):
                await await_system_state(op, "teardown", system_id, "torn_down")
            async with phase("report"):
                await assert_report(
                    base_url, auditor_token, db_url, window_start, project=_PROJECT, artifact_name="remote-accounting-report.json"
                )

    asyncio.run(_run())
```

- [ ] **Step 6: Verify collection + clean skip + guardrails**

Run: `uv run python -m pytest tests/integration/test_remote_live_stack.py -q`
Expected: the two unit tests PASS; `test_remote_spine_over_the_wire` SKIPS (remote config unset) — no error.

Run: `just test-live-stack`
Expected: collects the remote spine and SKIPS cleanly (or "no live_stack tests collected" if the marked suite is filtered) — exit 0.

Run: `just lint && just type && just test && just m2-gate`
Expected: all PASS; `m2-gate` still reports only the three allowlisted files (no new core touch).

- [ ] **Step 7: Commit**

```bash
git add tests/integration/test_remote_live_stack.py
git commit -m "test: add operator-run remote-libvirt spine e2e (#207)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `--report` flag on the portability gate + the committed report

**Files:**
- Modify: `scripts/m2_portability_gate.py`
- Modify: `tests/scripts/test_m2_portability_gate.py`
- Modify: `justfile`
- Create: `docs/reports/m2-portability.md` (generated)

- [ ] **Step 1: Write the failing renderer test**

Add to `tests/scripts/test_m2_portability_gate.py`:

```python
from scripts.m2_portability_gate import render_report  # add to the existing import block


def test_render_report_lists_allowlisted_and_flags_violations() -> None:
    md = render_report(
        {
            "src/kdive/domain/models.py": 4,
            "src/kdive/services/resources/discovery.py": 7,
        }
    )
    assert "# M2 portability report" in md
    assert "src/kdive/domain/models.py" in md
    assert "allowlisted" in md
    # a non-allowlisted core file renders as a VIOLATION line and a fail verdict
    assert "VIOLATION" in md and "src/kdive/services/resources/discovery.py" in md
    assert "gate FAILED" in md


def test_render_report_passes_when_only_allowlisted() -> None:
    md = render_report({"src/kdive/domain/models.py": 4})
    assert "gate passed" in md
    assert "VIOLATION" not in md
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_m2_portability_gate.py::test_render_report_passes_when_only_allowlisted -q`
Expected: FAIL — `ImportError: cannot import name 'render_report'`.

- [ ] **Step 3: Add `render_report` + the `--report` flag**

In `scripts/m2_portability_gate.py`, add a pure renderer (no new git calls) and wire `--report` in `main`:

```python
def render_report(touched: dict[str, int]) -> str:
    """Render the measurement as a markdown report (pure function over the touched map)."""
    allowed = {p: c for p, c in touched.items() if p in ALLOWED_FILES}
    bad = violations(touched)
    lines = [
        "# M2 portability report",
        "",
        f"Cumulative touched lines of the M2 commit set since the `{BASELINE_TAG}` tag, over the",
        "provider-agnostic core surface (ADR-0076). Generated by `just m2-report`.",
        "",
        "## Allowlisted touch-points",
        "",
        "| cumulative lines | file |",
        "|---:|---|",
    ]
    for path, count in sorted(allowed.items()):
        lines.append(f"| {count} | `{path}` |")
    lines.append("")
    if bad:
        lines.append("## Violations")
        lines.append("")
        lines.append("| cumulative lines | file |")
        lines.append("|---:|---|")
        for path, count in sorted(bad.items()):
            lines.append(f"| {count} | `{path}` |")
        lines.append("")
        lines.append("**Verdict: gate FAILED** — provider-specific changes reached the core surface.")
    else:
        lines.append("**Verdict: gate passed** — no core surface touched outside the ADR-0076 allowlist.")
    lines.append("")
    return "\n".join(lines)
```

In `main`, after computing `touched` (the union of the per-commit and net measurements), branch on the flag before the existing print/exit logic:

```python
def main() -> int:
    report_mode = "--report" in sys.argv[1:]
    # ... existing tag_check / log / net measurement, producing `touched` ...
    if report_mode:
        print(render_report(touched))
        return 0 if not violations(touched) else 1
    # ... existing print + verdict logic unchanged ...
```

- [ ] **Step 4: Run the renderer tests + the existing gate tests**

Run: `uv run python -m pytest tests/scripts/test_m2_portability_gate.py -q`
Expected: all PASS (the two new + the existing).

- [ ] **Step 5: Add the `m2-report` recipe and generate the report**

In `justfile`, after the `m2-gate` recipe:

```just
# Regenerate the committed milestone-end portability report (ADR-0076).
m2-report:
    python3 scripts/m2_portability_gate.py --report > docs/reports/m2-portability.md
```

Run: `mkdir -p docs/reports && just m2-report`
Then verify: `cat docs/reports/m2-portability.md` shows the three allowlisted files and `gate passed`.

Run: `just check-mermaid` (the new doc is scanned) → Expected: PASS. Confirm no banned prose words: `rg -ni "\b(critical|crucial|essential|significant|comprehensive|robust|elegant|sprint)\b" docs/reports/m2-portability.md` → no matches.

- [ ] **Step 6: Commit**

```bash
git add scripts/m2_portability_gate.py tests/scripts/test_m2_portability_gate.py justfile docs/reports/m2-portability.md
git commit -m "feat: emit the milestone-end M2 portability report (#207)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: The remote live-stack runbook

**Files:**
- Create: `docs/runbooks/remote-live-stack.md`
- Modify: `docs/runbooks/live-stack.md`

- [ ] **Step 1: Write `docs/runbooks/remote-live-stack.md`**

Mirror the local runbook's structure and cover what the remote host adds. Sections (full prose, no placeholders): (1) Prerequisites — a reachable `qemu+tls://` libvirtd, an operator-staged base-OS qcow2 volume with qemu-guest-agent + drgn + matching vmlinux/debuginfo; (2) Worker→host TLS reachability — set `KDIVE_REMOTE_LIBVIRT_URI` + the three cert refs (`KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF` / `_CLIENT_KEY_REF` / `_CA_CERT_REF`), server-cert verification on (`no_verify` forbidden, fail-closed), reachability check `virsh -c "$KDIVE_REMOTE_LIBVIRT_URI" list`; (3) The gdbstub-port ACL — `KDIVE_REMOTE_LIBVIRT_GDB_ADDR` (no default; the ACL'd listen address) + `_GDB_PORT_MIN/MAX`, reachable only from the worker pool's source, the ACL is the auth (ADR-0079); (4) Object-store reachability for the presigned PUT — the guest must reach the S3 endpoint to upload the vmcore on the post-crash reboot; the worker mints time-boxed single-object presigned URLs, no standing credential in any guest; (5) The base image volume — `KDIVE_REMOTE_BASE_IMAGE_VOLUME` (a **test/runbook input** to the provision profile's `base_image_volume`, not provider config); (6) Running it — `just test-live-stack` collects the remote spine; it skips clean unless the env above is present; the capture phase budgets ~300s server-side readiness (raise the drain if the operator reboot is slow); the run emits `remote-accounting-report.json` as completion evidence. End with a non-goals note: in-guest drgn-live MCP routing is deferred (#215); the introspect phase uses the worker-side vmcore postmortem.

Constraints: use **Milestone** not Sprint; plain factual prose (no banned words); link `ADR-0042`, `ADR-0076`, `ADR-0079`, `ADR-0084`, and the spec.

- [ ] **Step 2: Link it from the local runbook**

In `docs/runbooks/live-stack.md`, add near the top (after the intro paragraph): a sentence pointing to `remote-live-stack.md` for the remote `qemu+tls://` variant.

- [ ] **Step 3: Verify docs guardrails**

Run: `just check-mermaid` → Expected: PASS (`ok: N mermaid block(s) ...`).
Run: `rg -ni "\b(critical|crucial|essential|significant|comprehensive|robust|elegant|sprint)\b" docs/runbooks/remote-live-stack.md docs/runbooks/live-stack.md` → Expected: no matches.
Run: `just docs-check` → Expected: PASS (no tool-reference drift).

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/remote-live-stack.md docs/runbooks/live-stack.md
git commit -m "docs: add the remote live-stack operator runbook (#207)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Full-gate verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full local gate**

Run: `just lint && just type && just lock-check && just lint-shell && just lint-workflows && just check-mermaid && just docs-check && just m2-gate && just test`
Expected: every recipe PASSES. `m2-gate` reports only the three allowlisted core files (this PR added none).

- [ ] **Step 2: Confirm the remote suite skips cleanly**

Run: `just test-live-stack`
Expected: exit 0 — the remote + local `live_stack` tests skip cleanly with actionable reasons (no stack configured).

- [ ] **Step 3: Confirm the report regenerates deterministically**

Run: `just m2-report && git diff --exit-code docs/reports/m2-portability.md`
Expected: no diff (the committed report matches a fresh generation at this HEAD).

---

## Self-Review notes

- **Spec coverage:** Deliverable 1 (remote spine) → Tasks 1–2; Deliverable 2 (portability report) → Task 3; Deliverable 3 (runbook) → Task 4. The spec's drain-budget point → Task 1 Step 1 (`deadline_s` param) + Task 2 (`_CAPTURE_DEADLINE_S=900`). The evidence artifact → Task 2 report phase (`remote-accounting-report.json`). The introspect-routing claim → Task 2 introspect phase asserts a non-empty redacted report.
- **Type consistency:** the public helper names (`ok`, `scalar`, `drain_job`, `await_system_state`, `mint_role_token`, `seed_metering`, `grant_force_crash_scope`, `assert_audit`, `assert_report`, `db_now`, `system_torn_down`) are defined in Task 1 and imported unchanged in Tasks 1 (local repoint) and 2 (remote).
- **No core touches:** every file is under `tests/`, `docs/`, `scripts/`, `justfile` — Task 5 Step 1 re-confirms `m2-gate` green.
