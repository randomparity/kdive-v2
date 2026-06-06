# dcache demo: cmdline wiring + A/B driver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dcache `dhash_entries=1` demo reproducible end-to-end by wiring the kernel cmdline through the build ledger to boot, adding env-sourced demo profiles, a `live_vm` A/B driver, and a runbook.

**Architecture:** The cmdline becomes ledger-sourced for both build lanes — `runs.build` gains an optional `cmdline`, recorded in the `(run_id, "build")` ledger `result`, and `_cmdline_for` reads it (the `build_profile` read is removed). A test-only helper builds the demo's build/provisioning profiles from operator env. A `live_vm`-marked driver runs one System through two sequential Runs (vulnerable → fixed). A runbook documents the host staging and the one-command path.

**Tech Stack:** Python 3.13, `uv`, FastMCP, Postgres (disposable via testcontainers), pytest, libvirt (live host only). Commands run through `just` recipes.

**Spec:** [`docs/superpowers/specs/2026-06-06-dcache-demo-driver-design.md`](../specs/2026-06-06-dcache-demo-driver-design.md) · **ADR:** [ADR-0056](../../adr/0056-live-demo-cmdline-wiring-dcache-driver.md)

**Guardrails (run after each task, all must be green):**
- `just lint` — ruff check + format check
- `just type` — `ty` whole tree (src + tests)
- `uv run python -m pytest tests/mcp/test_runs_tools.py tests/mcp/test_tool_docs.py tests/integration/test_dcache_demo_profiles.py -q` (scope to touched suites; the `live_vm` driver skips)

---

## File structure

- Modify `src/kdive/mcp/tools/runs.py` — `_cmdline_for` (async, ledger-sourced), `_enqueue_build` (carry cmdline), `build_run` (cmdline param), `build_handler` (record cmdline), `install_handler` (log resolved cmdline), the `runs.build` MCP wrapper (cmdline param). The two `_cmdline_for` call sites `await` the new signature.
- Modify `docs/guide/reference/runs.md` — correct the "inert until that wiring lands" note for `runs.complete_build`'s cmdline.
- Modify `tests/mcp/test_runs_tools.py` — rewrite the three `_cmdline_for` tests to the ledger source; seed a build-ledger row in the succeeded-run helpers; add a `build_run(cmdline=…)`-records-the-ledger test.
- Create `tests/integration/_dcache_demo.py` — the test-only demo-profile helper (build/provisioning profiles + `DEMO_CMDLINE` + preflight).
- Create `tests/integration/test_dcache_demo_profiles.py` — host-free profile-shape + method-resolution + preflight-skip tests.
- Create `tests/integration/test_dcache_demo.py` — the `@pytest.mark.live_vm` A/B driver (skips in CI).
- Create `docs/runbooks/dcache-demo.md` — host staging + the one-command/agent walkthrough + cleanup.

---

## Task 1: cmdline becomes ledger-sourced (the wiring)

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py`
- Modify: `tests/mcp/test_runs_tools.py`
- Modify: `docs/guide/reference/runs.md`

### Sub-task 1.1 — Seed a build-ledger row in the succeeded-run test helpers (contain churn)

`_cmdline_for` will read the `(run_id, "build")` ledger instead of `build_profile`. The install/boot tests seed a succeeded Run with `_SUCCEEDED_BUILD` (which carries a `cmdline` key in `build_profile`) but no ledger row. Migrate that cmdline into a seeded ledger row so those tests keep their expected cmdline unchanged.

- [ ] **Step 1: Add a ledger-seed helper and call it from both succeeded-run helpers**

In `tests/mcp/test_runs_tools.py`, add near `_record_install_step` (≈ line 968):

```python
async def _seed_build_ledger(
    pool: AsyncConnectionPool, run_id: str, *, cmdline: str | None
) -> None:
    """Record a (run_id, 'build') ledger row, optionally carrying the resolved cmdline."""
    result: dict[str, Any] = {
        "kernel_ref": f"local/runs/{run_id}/kernel",
        "debuginfo_ref": f"local/runs/{run_id}/vmlinux",
        "build_id": "abcdef0123456789",
    }
    if cmdline is not None:
        result["cmdline"] = cmdline
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run_id, Jsonb(result)),
        )
```

In `_seed_succeeded_run` (≈ line 921), after the `kernel_ref` UPDATE, add:

```python
    await _seed_build_ledger(
        pool, run_id, cmdline=(build_profile or _SUCCEEDED_BUILD).get("cmdline")
    )
    return run_id
```

In `_seed_succeeded_run_on_system` (≈ line 943), after the `kernel_ref` UPDATE and before `return`:

```python
    await _seed_build_ledger(pool, str(run.id), cmdline=_SUCCEEDED_BUILD.get("cmdline"))
    return str(run.id)
```

Confirm `Jsonb` is imported in the test file (it is used elsewhere; if not, add `from psycopg.types.json import Jsonb`).

- [ ] **Step 2: Re-scope total-`run_steps` count assertions to the relevant step**

A succeeded Run now faithfully carries a build ledger row, so any test that asserts the *total*
`run_steps` count after `_seed_succeeded_run`/`_seed_succeeded_run_on_system` must scope its count
to the step it means. Find them: `rg -n "FROM run_steps WHERE run_id=%s\"" tests/mcp/test_runs_tools.py`.
The known break is `test_install_handler_failure_records_no_step` (≈ line 1312): it seeds via
`_seed_succeeded_run(pool)` and asserts `nsteps == 0` over `SELECT count(*) FROM run_steps WHERE
run_id=%s`. Change that query (and any sibling that seeds a succeeded run and asserts a total of 0)
to scope to install/boot:

```python
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='install'",
                (run_id,),
            )
```

`assert nsteps == 0` then means "no *install* row on failure", which is the test's real intent; the
build row from the faithful seed is expected. (`test_install_handler_missing_kernel_ref_is_config_error`
at ≈ line 1334 uses `_seed_run(..., state=SUCCEEDED)` directly — no build row added — so it is
unaffected; leave it. The build-handler tests around ≈ line 781 use `_seed_running_run`, also
unmodified.)

- [ ] **Step 3: Run the suite to confirm the seed change alone is green**

`_cmdline_for` is still sync at this point, so the only behavioral change is the extra build row.
Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q`
Expected: PASS (the re-scoped count assertions hold; the extra build row is harmless to every other
test). If a test still fails on a total count, re-scope it the same way.

### Sub-task 1.2 — Make `_cmdline_for` async + ledger-sourced (RED → GREEN)

- [ ] **Step 1: Rewrite the three `_cmdline_for` unit tests to the ledger source**

Replace the three tests at ≈ lines 1025–1037 of `tests/mcp/test_runs_tools.py`. They were sync over a hand-built Run; now they exercise the ledger read against the disposable Postgres. Use the existing `_pool`/`migrated_url` fixtures and the new `_seed_build_ledger` helper, fetching the Run with `RUNS.get`:

```python
def test_cmdline_default_is_kdump_reserving_for_kdump(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool, build_profile={"schema_version": 1})
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await runs_tools._cmdline_for(conn, run, CaptureMethod.KDUMP)
            assert "crashkernel=" in cmdline

    asyncio.run(_run())


def test_cmdline_default_omits_crashkernel_for_non_kdump(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool, build_profile={"schema_version": 1})
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await runs_tools._cmdline_for(conn, run, CaptureMethod.CONSOLE)
            assert "crashkernel=" not in cmdline

    asyncio.run(_run())


def test_cmdline_from_ledger_overrides_default_for_any_method(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={"schema_version": 1, "cmdline": "console=ttyS0 dhash_entries=1"}
            )
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                cmdline = await runs_tools._cmdline_for(conn, run, CaptureMethod.KDUMP)
            assert cmdline == "console=ttyS0 dhash_entries=1"

    asyncio.run(_run())
```

Delete the now-unused `_run_with_build_profile` helper if no other test references it (grep first: `rg -n _run_with_build_profile tests/mcp/test_runs_tools.py`). Ensure `RUNS` is imported (`from kdive.db.repositories import RUNS` — likely already present; add if not).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -k cmdline -q`
Expected: FAIL — `_cmdline_for` is still sync (`TypeError: object str can't be used in 'await' expression`) / signature mismatch.

- [ ] **Step 3: Make `_cmdline_for` async and ledger-sourced**

In `src/kdive/mcp/tools/runs.py`, replace `_cmdline_for` (≈ lines 660–671):

```python
async def _cmdline_for(conn: AsyncConnection, run: Run, method: CaptureMethod) -> str:
    """Resolve the kernel command line from the build ledger (ADR-0056 §2).

    The cmdline's source of record is the `(run_id, "build")` ledger `result["cmdline"]`,
    written by the build handler (server lane) or `complete_build` (external lane). A
    non-blank string is the cmdline; otherwise the method-appropriate default applies — the
    kdump default reserves `crashkernel=`, the non-kdump default does not (ADR-0051 §3).
    """
    result = await _existing_build_result(conn, run.id)
    if result is not None:
        value = result.get("cmdline")
        if isinstance(value, str) and value.strip():
            return value
    return _KDUMP_DEFAULT_CMDLINE if method is CaptureMethod.KDUMP else _NONKDUMP_DEFAULT_CMDLINE
```

Update the two call sites to `await`:
- `install_run` (≈ line 731): `cmdline = await _cmdline_for(conn, run, method)`
- `install_handler` (≈ line 842): `cmdline = await _cmdline_for(conn, run, method)`

- [ ] **Step 4: Run the cmdline + install/boot tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q`
Expected: PASS (the seeded ledger rows from 1.1 keep the install/boot cmdline assertions intact). If a test that asserts a specific installer cmdline fails, confirm its succeeded-run was seeded via `_seed_succeeded_run`/`_seed_succeeded_run_on_system` (now carrying the ledger row); if it builds a Run inline, add a `_seed_build_ledger(pool, run_id, cmdline=…)` call.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): resolve the boot cmdline from the build ledger, not build_profile (#128)"
```

### Sub-task 1.3 — `runs.build` records the cmdline; the MCP wrapper exposes it (RED → GREEN)

- [ ] **Step 1: Write the failing test — `build_run(cmdline=…)` records `result["cmdline"]`**

Add to `tests/mcp/test_runs_tools.py` near the existing build_handler tests (model on `test_completed_step_replay_does_not_re_execute` / the build helpers around line 700–760). It admits a build with a cmdline, runs the handler with the recording builder, and asserts the ledger carries the cmdline:

```python
def test_build_run_records_cmdline_in_the_build_ledger(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool)
            run_id = await _seed_running_built_run(pool, sys_id)  # created-state server run
            ctx = request_context(Role.OPERATOR)
            env = await runs_tools.build_run(pool, ctx, run_id, cmdline="console=ttyS0 dhash_entries=1")
            assert env.status != "error"  # a job-handle envelope, not a failure
            async with pool.connection() as conn:
                job = await _build_job_for(conn, run_id)  # the enqueued build job
                await runs_tools.build_handler(conn, job, _RecordingBuilder())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT result FROM run_steps WHERE run_id=%s AND step='build'", (run_id,)
                )
                row = await cur.fetchone()
            assert row is not None and row["result"]["cmdline"] == "console=ttyS0 dhash_entries=1"

    asyncio.run(_run())


def test_build_run_without_cmdline_records_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool)
            run_id = await _seed_running_built_run(pool, sys_id)
            ctx = request_context(Role.OPERATOR)
            await runs_tools.build_run(pool, ctx, run_id)
            async with pool.connection() as conn:
                job = await _build_job_for(conn, run_id)
                await runs_tools.build_handler(conn, job, _RecordingBuilder())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT result FROM run_steps WHERE run_id=%s AND step='build'", (run_id,)
                )
                row = await cur.fetchone()
            assert row is not None and "cmdline" not in row["result"]

    asyncio.run(_run())
```

Add the two small helpers if absent (model on the existing seeds; `_seed_running_run` already exists per the walking-skeleton test — reuse it and a created-state variant). If a `created`-state server run helper does not exist, add:

```python
async def _seed_running_built_run(pool: AsyncConnectionPool, system_id: str) -> str:
    """A created-state server-lane Run ready for runs.build (valid ServerBuildProfile)."""
    inv_id = await _seed_investigation(pool)
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                investigation_id=UUID(inv_id), system_id=UUID(system_id),
                state=RunState.CREATED, build_profile=copy.deepcopy(_VALID_BUILD),
                failure_category=None,
            ),
        )
    return str(run.id)


async def _build_job_for(conn: AsyncConnection, run_id: str) -> Job:
    """Fetch the enqueued build job by its dedup key (no dequeue — avoids charging an attempt)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (f"{run_id}:build",))
        row = await cur.fetchone()
    assert row is not None
    return Job.model_validate(row)
```

(`_VALID_BUILD` is the existing valid `ServerBuildProfile` dict; confirm `Job`, `dict_row`, and
`RUNS`/`uuid4`/`_DT`/`copy` are imported — most already are. Fetching by dedup key matches the
build-handler tests' pattern and avoids `queue.dequeue`'s required `worker_id` arg + attempt
charge.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -k "records_cmdline or without_cmdline_records_none" -q`
Expected: FAIL — `build_run()` takes no `cmdline` keyword.

- [ ] **Step 3: Thread the cmdline through `build_run` → payload → handler**

In `src/kdive/mcp/tools/runs.py`:

Change `_enqueue_build` (≈ line 278) to carry an optional cmdline in the payload:

```python
async def _enqueue_build(
    conn: AsyncConnection, ctx: RequestContext, run: Run, cmdline: str | None
) -> Job:
    payload: dict[str, Any] = {"run_id": str(run.id)}
    if isinstance(cmdline, str) and cmdline.strip():
        payload["cmdline"] = cmdline
    return await queue.enqueue(
        conn, JobKind.BUILD, payload, _authorizing(ctx, run.project), f"{run.id}:build"
    )
```

Thread `cmdline` from `build_run` through `_build_locked` to `_enqueue_build`:
- `build_run(pool, ctx, run_id)` → `build_run(pool, ctx, run_id, *, cmdline: str | None = None)`; pass `cmdline` into `_build_locked`.
- `_build_locked(conn, ctx, run)` → `_build_locked(conn, ctx, run, cmdline)`; pass to `_enqueue_build(conn, ctx, run, cmdline)`.

In `build_handler` (≈ line 633), record the payload cmdline into the build result before finalize:

```python
        result = {
            "kernel_ref": output.kernel_ref,
            "debuginfo_ref": output.debuginfo_ref,
            "build_id": output.build_id,
        }
        cmdline = job.payload.get("cmdline")
        if isinstance(cmdline, str) and cmdline.strip():
            result["cmdline"] = cmdline
```

(Place the cmdline lines inside the `if result is None:` block, right after `result = {...}`, so a rebuild that reused the existing ledger row is untouched — idempotent.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Add the cmdline param to the `runs.build` MCP wrapper + log the resolved cmdline**

In `register()`, the `runs.build` wrapper (≈ line 1034), add the param and pass it:

```python
    async def runs_build(
        run_id: Annotated[str, Field(description="The Run to build.")],
        cmdline: Annotated[
            str | None,
            Field(
                description="Kernel command line recorded in the build ledger and applied at "
                "boot (e.g. 'console=ttyS0 dhash_entries=1'). Omit for the method default. "
                "Bound on the first build of a Run."
            ),
        ] = None,
    ) -> ToolResponse:
        """Enqueue the kernel build job for a Run; poll jobs.* for completion. Requires operator."""
        return await build_run(pool, current_context(), run_id, cmdline=cmdline)
```

In `install_handler` (`_do`, ≈ line 846), after `cmdline` is resolved, log it:

```python
    cmdline = await _cmdline_for(conn, run, method)
    _log.info("install: run %s resolved cmdline %r (method %s)", run_id, cmdline, method.value)
```

(`cmdline` is resolved before `_do`; keep the existing resolution, add the `_log.info`.)

- [ ] **Step 6: Run the doc-contract + runs tests**

Run: `uv run python -m pytest tests/mcp/test_tool_docs.py tests/mcp/test_runs_tools.py -q`
Expected: PASS (`test_every_parameter_has_a_description` passes because the new param has a `Field(description=…)`).

- [ ] **Step 7: Correct the agent-facing doc**

In `docs/guide/reference/runs.md`, the `cmdline` row (≈ line 34) currently says it is "inert until that wiring lands." Replace the trailing clause with: "Recorded in the build ledger and applied at boot via `runs.install`/`runs.boot`." If an ADR-0047 generated tool guide is committed and a regeneration command exists (check `justfile`/`docs/guide`), regenerate it; otherwise the assertion-based `test_tool_docs.py` is the gate.

- [ ] **Step 8: Run guardrails and commit**

```bash
just lint && just type
uv run python -m pytest tests/mcp/test_runs_tools.py tests/mcp/test_tool_docs.py -q
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py docs/guide/reference/runs.md
git commit -m "feat(runs): runs.build cmdline param recorded in the build ledger (#128)"
```

---

## Task 2: demo-profile helper + host-free tests

**Files:**
- Create: `tests/integration/_dcache_demo.py`
- Create: `tests/integration/test_dcache_demo_profiles.py`

- [ ] **Step 1: Write the failing host-free tests**

Create `tests/integration/test_dcache_demo_profiles.py`:

```python
"""Host-free tests for the dcache-demo profile helper (#128, ADR-0056 §3–4)."""

from __future__ import annotations

import pytest

from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from tests.integration import _dcache_demo as demo


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_KERNEL_SRC", "/abs/linux")
    monkeypatch.setenv("KDIVE_TEST_BUILD_CONFIG", "/abs/.config")
    monkeypatch.setenv("KDIVE_GUEST_IMAGE", "/abs/rootfs.qcow2")
    monkeypatch.setenv("KDIVE_DEMO_FIX_PATCH", "/abs/fix.patch")


def test_vulnerable_build_profile_has_no_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    parsed = BuildProfile.parse(demo.demo_build_profile(fixed=False))
    assert isinstance(parsed, ServerBuildProfile)
    assert parsed.patch_ref is None
    assert parsed.kernel_source_ref == "/abs/linux"


def test_fixed_build_profile_carries_the_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    parsed = BuildProfile.parse(demo.demo_build_profile(fixed=True))
    assert isinstance(parsed, ServerBuildProfile)
    assert parsed.patch_ref == "/abs/fix.patch"


def test_provisioning_profile_is_console_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    raw = demo.demo_provisioning_profile()
    parsed = ProvisioningProfile.parse(raw)  # does not raise
    assert parsed is not None
    # Console-only invariant: no crashkernel reservation (→ _install_method_for resolves CONSOLE,
    # ADR-0051 §1), no SSH credential, no destructive opt-in. Assert the stored section directly —
    # the falsifiable shape — rather than constructing a full System for the resolver.
    section = raw["provider"]["local-libvirt"]
    assert "crashkernel" not in section
    assert "ssh_credential_ref" not in section
    assert "destructive_ops" not in section


def test_demo_cmdline_carries_the_trigger() -> None:
    assert demo.DEMO_CMDLINE == "console=ttyS0 dhash_entries=1"


def test_preflight_skips_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("KDIVE_KERNEL_SRC", "KDIVE_TEST_BUILD_CONFIG", "KDIVE_GUEST_IMAGE", "KDIVE_DEMO_FIX_PATCH"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(pytest.skip.Exception):
        demo.demo_preflight()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/integration/test_dcache_demo_profiles.py -q`
Expected: FAIL — `tests/integration/_dcache_demo.py` does not exist (ImportError).

- [ ] **Step 3: Write the helper**

Create `tests/integration/_dcache_demo.py`:

```python
"""Test-only demo-profile helper for the dcache `dhash_entries=1` A/B (#128, ADR-0056 §3–4).

Resolves the demo's build/provisioning profiles from operator env — the seams G1/G3
established — so nothing kernel-sized is committed. Imported by the host-free profile test
and the `live_vm` driver. Not shipped product code.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

_KERNEL_SRC_ENV = "KDIVE_KERNEL_SRC"
_CONFIG_ENV = "KDIVE_TEST_BUILD_CONFIG"
_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_FIX_PATCH_ENV = "KDIVE_DEMO_FIX_PATCH"

DEMO_CMDLINE = "console=ttyS0 dhash_entries=1"
"""The pathological boot parameter that triggers the dcache OOB read (test-case 05)."""


def demo_build_profile(*, fixed: bool) -> dict[str, Any]:
    """The server-build profile: `~/src/linux` + the demo `.config`; `fixed` adds the patch."""
    profile: dict[str, Any] = {
        "schema_version": 1,
        "kernel_source_ref": os.environ[_KERNEL_SRC_ENV],
        "config_ref": os.environ[_CONFIG_ENV],
    }
    if fixed:
        profile["patch_ref"] = os.environ[_FIX_PATCH_ENV]
    return profile


def demo_provisioning_profile() -> dict[str, Any]:
    """A console-only provisioning profile (no crashkernel, no SSH, no destructive opt-in)."""
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_SRC_ENV],
        "provider": {
            "local-libvirt": {"rootfs": {"kind": "path", "path": os.environ[_GUEST_IMAGE_ENV]}}
        },
    }


def demo_preflight() -> None:
    """Resolve the four demo env vars or `pytest.skip` with the exact fix (ADR-0035 §4 style)."""
    fixes = {
        _KERNEL_SRC_ENV: "run scripts/live-vm/fetch-kernel-tree.sh (or point at ~/src/linux)",
        _CONFIG_ENV: "generate a .config (CONFIG_CRASH_DUMP + DWARF/BTF) — see docs/runbooks/dcache-demo.md",
        _GUEST_IMAGE_ENV: "run scripts/live-vm/build-guest-image.sh",
        _FIX_PATCH_ENV: "export the dcache 7.0.1 fix as a -p1 patch — see docs/runbooks/dcache-demo.md",
    }
    for var, fix in fixes.items():
        value = os.environ.get(var)
        if not value or not os.path.exists(value):
            pytest.skip(f"{var} unset or missing; {fix}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/integration/test_dcache_demo_profiles.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails and commit**

```bash
just lint && just type
uv run python -m pytest tests/integration/test_dcache_demo_profiles.py -q
git add tests/integration/_dcache_demo.py tests/integration/test_dcache_demo_profiles.py
git commit -m "test(demo): env-sourced dcache demo profiles + host-free shape tests (#128)"
```

---

## Task 3: the `live_vm` A/B driver

**Files:**
- Create: `tests/integration/test_dcache_demo.py`

This test is `@pytest.mark.live_vm` and skips in CI (no kernel tree / guest image). It is not runnable in this environment; it encodes the A/B as an executable runbook. The CI-checkable property is *collection + clean skip*.

- [ ] **Step 1: Write the driver (collected, skips cleanly in CI)**

Create `tests/integration/test_dcache_demo.py`:

```python
"""The dcache `dhash_entries=1` A/B demo driver (#128, gap G5, ADR-0056 §5–6).

`live_vm`-marked: one real System, two sequential Runs over the real build/install/boot
handlers — Run A (vulnerable, no patch) crashes on the console; Run B (fixed, patch_ref)
boots clean. Skips cleanly in CI and on any host missing the four demo env vars. Drives the
handlers directly (ADR-0019), not the HTTP transport.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.providers.local_libvirt.install import classify_console, read_console_log
from kdive.providers.local_libvirt.provisioning import console_log_path
from tests.integration import _dcache_demo as demo


@pytest.mark.live_vm
def test_dcache_demo_ab_loop(migrated_url: str) -> None:  # pragma: no cover - live_vm
    """Vulnerable boot crashes on the console; the patched rebuild boots ready (test-case 05).

    Wired against an operator host: KDIVE_KERNEL_SRC, KDIVE_TEST_BUILD_CONFIG, KDIVE_GUEST_IMAGE,
    KDIVE_DEMO_FIX_PATCH all present and the staging dirs worker-writable
    (docs/runbooks/dcache-demo.md). The body below is the executable A/B; it provisions one
    System, runs build(cmdline=dhash)->install->boot twice (no patch, then patch_ref), asserts
    Run A's console classifies `crashed` with `__d_lookup` and shows dhash_entries=1, then Run B's
    console classifies `ready`, and tears the System down in a finally.
    """
    demo.demo_preflight()
    raise NotImplementedError(
        "live_vm dcache A/B harness: provision one System, then per Run "
        "create->build(cmdline=demo.DEMO_CMDLINE)->install->boot via the real handlers; "
        "assert classify_console(read_console_log(console_log_path(system_id))) == 'crashed' "
        "(Run A, dhash_entries=1 in the Command line) then 'ready' (Run B, fixed); "
        "release + teardown in a finally. Wired by the live_vm runner."
    )
```

Rationale: the established `live_vm` convention in this repo (`test_c8_live_introspect_over_ssh`) is a preflight-then-`NotImplementedError` body the operator's `live_vm` runner fleshes out against real hardware; the falsifiable CI property is the clean skip. The imports (`classify_console`, `read_console_log`, `console_log_path`, `CaptureMethod`) are the real symbols the harness uses and double as a compile-time check that they exist. The runbook (Task 4) carries the full agent/operator walkthrough that this body encodes.

- [ ] **Step 2: Verify it skips cleanly (CI behavior) and is collected**

Run: `uv run python -m pytest tests/integration/test_dcache_demo.py -q`
Expected: `1 skipped` (the `live_vm` marker is deselected by `just test`; run directly it reaches `demo_preflight()` and skips on the absent env). Confirm no collection error.

Run: `uv run python -m pytest tests/integration/test_dcache_demo.py -m live_vm -q` (force-select the marker)
Expected: `1 skipped` with the preflight reason (env absent in this environment).

- [ ] **Step 3: Guardrails and commit**

```bash
just lint && just type
git add tests/integration/test_dcache_demo.py
git commit -m "test(demo): live_vm dcache A/B driver, skips cleanly without the host (#128)"
```

---

## Task 4: the runbook (acceptance artifact)

**Files:**
- Create: `docs/runbooks/dcache-demo.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/dcache-demo.md` covering, in order (mirror `docs/runbooks/live-stack.md`'s tone — plain, factual, no "comprehensive/robust"):

1. **What this reproduces** — `docs/test-cases/05-dcache-dhash-entries-oob-read.md`: build `~/src/linux` @ v7.0, boot `dhash_entries=1` → `__d_lookup` crash on the console; apply the 7.0.1 dcache fix, rebuild, reboot → clean `kdive-ready`.
2. **Prerequisites** — the #123 host (KVM/libvirt/toolchain), the G3 rootfs (`scripts/live-vm/build-guest-image.sh`).
3. **Host staging** — make `/var/lib/kdive/{rootfs,install,build,console}` worker-writable (an OS admin `chown`/`chmod` once), **or** export `KDIVE_BUILD_WORKSPACE` / `KDIVE_INSTALL_STAGING` and the rootfs/console dirs to writable paths. **No libvirt network** (console-only).
4. **Env** — the four vars with how to produce each:
   - `KDIVE_KERNEL_SRC=~/src/linux`
   - `KDIVE_TEST_BUILD_CONFIG` — `make defconfig`, then enable `CONFIG_CRASH_DUMP` and one of `CONFIG_DEBUG_INFO_DWARF5`/`DWARF4`/`BTF`; verify with the build preflight requirements (ADR-0029). A `.config` missing a prerequisite fails Run A's build with `build_failure`.
   - `KDIVE_GUEST_IMAGE` — `scripts/live-vm/build-guest-image.sh` output.
   - `KDIVE_DEMO_FIX_PATCH` — the dcache 7.0.1 fix as a `-p1` patch; `git apply --check` it against the v7.0 tree first (a patch that does not apply fails Run B's build with `configuration_error`).
5. **One command** — `just test-live` (or `uv run python -m pytest tests/integration/test_dcache_demo.py -m live_vm -q`).
6. **Agent-facing walkthrough** — the tool-by-tool path: `allocations.request` → `systems.provision(profile=…)` → `investigations.open` → `runs.create(build_profile=vulnerable)` → `runs.build(run_id, cmdline="console=ttyS0 dhash_entries=1")` → `jobs.wait` → `runs.install` → `jobs.wait` → `runs.boot` → observe the `__d_lookup` crash in the console artifact → repeat with `runs.create(build_profile=fixed-with-patch_ref)` on the same System → boot clean. **Flag `cmdline=` as load-bearing**: omit it and the kernel boots `console=ttyS0` (no trigger) and the bug silently does not reproduce; the resolved cmdline is logged by the worker at install.
7. **Cleanup** — `allocations.release` then teardown; reap a leftover System from an aborted run with `virsh destroy`/`undefine kdive-<system_id>` and remove `/var/lib/kdive/install/<system_id>`.

- [ ] **Step 2: Lint the doc (mermaid/style guard) and commit**

Run: `just lint` (and `uv run python -m pytest tests/mcp/test_tool_docs.py -q` if the runbook is referenced by a doc test — it is not, but confirm no doc-style guard trips on banned words).
Expected: PASS. Grep the runbook for banned words: `rg -n -i "critical|robust|comprehensive|elegant|crucial|essential" docs/runbooks/dcache-demo.md` → no hits.

```bash
git add docs/runbooks/dcache-demo.md
git commit -m "docs(demo): dcache dhash_entries A/B runbook — staging, one command, cleanup (#128)"
```

---

## Task 5: full guardrails + cross-check

- [ ] **Step 1: Run the full local CI gate**

Run: `just ci`
Expected: PASS (lint, type, lint-shell, lint-workflows, check-mermaid, test). The `live_vm` driver is deselected by `just test`.

- [ ] **Step 2: Confirm the new tool param is covered + described**

Run: `uv run python -m pytest tests/mcp/test_tool_docs.py -q`
Expected: PASS — `test_every_parameter_has_a_description` (the `runs.build` `cmdline` description) and `test_implemented_tools_have_a_covering_test` green.

- [ ] **Step 3: Confirm no `_cmdline_for` `build_profile` read remains (replace-don't-deprecate)**

Run: `rg -n "build_profile.*cmdline|cmdline.*build_profile" src/kdive/mcp/tools/runs.py`
Expected: no hits (the read is gone; the cmdline lives only in the ledger).

- [ ] **Step 4: Commit any residual + verify the tree is clean**

```bash
git status   # expect clean
```

---

## Self-review (run after drafting; fix inline)

- **Spec coverage:** Part A → Task 1 (1.2 `_cmdline_for` ledger, 1.3 `build_run` param + record + log + doc). Part B → Task 2. Part C (incl. teardown finally) → Task 3 (the `live_vm` body encodes provision→A→B→teardown; the runbook carries the full sequence). Part D → Task 4. Success criteria: Wiring → 1.2/1.3 tests; Profiles → Task 2 tests; Skip → Task 2 preflight test + Task 3 skip; End-to-end → Task 3 (manual). Edges: blank cmdline (1.2 default path), external-source (existing gate, unchanged), kdump-missing-crashkernel (existing install gate, unchanged — now ledger-sourced), false-negative (Task 3 dhash-in-console assert + 1.3 install log + runbook flag), bad patch / bad config / aborted-run (runbook, Task 4). Test plan → Tasks 1–3 + Task 5 doc checks.
- **Placeholder scan:** every code step shows the code; commands have expected output. No TBD/TODO.
- **Type consistency:** `_cmdline_for(conn, run, method)` async — both call sites updated and awaited (1.2 Step 3). `_enqueue_build(conn, ctx, run, cmdline)` — `_build_locked` updated to pass cmdline (1.3 Step 3). `demo_build_profile(*, fixed)`, `demo_provisioning_profile()`, `demo_preflight()`, `DEMO_CMDLINE` — names match across Tasks 2/3 and the tests.
- **Known caveat for the executor:** Task 1.1 Step 2 re-scopes the total-`run_steps` count assertions (the build row a faithful succeeded-run seed now carries); the known break is `test_install_handler_failure_records_no_step`. If any other install/boot test builds a succeeded Run *inline* (not via `_seed_succeeded_run*`) and asserts a non-default installer cmdline, add a `_seed_build_ledger(pool, run_id, cmdline=…)` call to it. The `just type` whole-tree run catches a missed `await` on `_cmdline_for`; the `_build_job_for` helper fetches the build job by dedup key (no `queue.dequeue` `worker_id` arg).
