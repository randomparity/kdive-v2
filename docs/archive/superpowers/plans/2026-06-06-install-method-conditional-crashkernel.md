# Install-time method-conditional crashkernel gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `runs.install` require a `crashkernel=` cmdline token only for a `kdump`-provisioned System, resolve the capture method from the System's provisioning profile, and thread `method`/`initrd_ref` from `install_handler` into `installer.install(...)`.

**Architecture:** Two pure helpers in `mcp/tools/runs.py` — `_install_method_for(system)` (maps the System's loosely-read `provider["local-libvirt"]` section to a `CaptureMethod`) and a method-aware `_cmdline_for(run, method)` (splits the default cmdline into kdump/non-kdump). `install_run` (the tool gate) and `install_handler` (the worker) both fetch the System, resolve the method, and gate/thread on it. `initrd_ref` is recovered from the build ledger.

**Tech Stack:** Python 3.13, `uv`, `pytest` (DB tests use disposable Postgres via `migrated_url`), `ruff`, `ty`. Design: [ADR-0051](../../adr/0051-install-method-conditional-crashkernel.md), spec [§8](../specs/2026-06-05-crash-capture-tiers-design.md).

---

## Guardrail discipline (read before starting)

`_cmdline_for` has **two existing callers** today — `install_run` (`runs.py:668`) and `install_handler` (`runs.py:770`) — both calling it with one positional arg. Task 2 changes its signature to require `method`. **The signature change and both caller rewrites MUST land in the same commit** (Task 2), or the full suite goes red mid-plan. Every task's final verify step runs the **full** suite (`just test`), not a narrow `-k` subset, and you must not commit on red — this is the project's "guardrails green at every commit" rule. The `-k` runs shown are for fast iteration *within* a step; the pre-commit gate is always the full `just lint && just type && just test`.

## Background the engineer needs

- `runs.install` → `install_run(pool, ctx, run_id)` (`src/kdive/mcp/tools/runs.py:655`) is the **tool gate**: it validates the Run is `succeeded`, currently rejects any cmdline lacking `crashkernel=` (line 668), then enqueues the install job. It does **not** run the install — the worker does.
- `install_handler(conn, job, installer)` (`runs.py:752`) is the **worker**: it reads the Run, computes the cmdline, and calls `installer.install(...)`. The real installer is `LocalLibvirtInstall` (`src/kdive/providers/local_libvirt/install.py`); tests inject `_FakeInstaller`.
- `installer.install(system_id, run_id, kernel_ref, *, cmdline, method=CaptureMethod.HOST_DUMP, initrd_ref=None)` (`install.py:154`) already runs `_kdump_check` **only** for `method == CaptureMethod.KDUMP` and emits an `<initrd>` only when `initrd_ref` is not `None`. The tool layer just never passes `method`/`initrd_ref`. Leave this provider signature unchanged.
- `CaptureMethod` is `kdive.domain.capture.CaptureMethod` (`CONSOLE`, `HOST_DUMP`, `GDBSTUB`, `KDUMP`).
- A System's `provisioning_profile` (`System.provisioning_profile: dict[str, Any]`) is a `ProvisioningProfile.model_dump(by_alias=True)` in production (`mcp/tools/systems.py:335/476/743`). **The provider section lives under the alias key `"local-libvirt"`** (`ResourceKind.LOCAL_LIBVIRT.value`), **not** `"local_libvirt"`. The kdump prerequisite is `provider["local-libvirt"]["crashkernel"]` (a non-empty string or absent); the debug flags are `provider["local-libvirt"]["debug"]["preserve_on_crash"]` and `["gdbstub"]` (booleans).
- The build ledger row `(run_id, "build")` is read by `_existing_build_result(conn, run_id)` (`runs.py:336`). The external-build lane records `initrd_ref` there (`runs.py:513`, the uploaded initrd's object key or `""`); the server-build lane records no `initrd_ref` key (`runs.py:625-629`).
- `SYSTEMS` repository (`kdive.db.repositories.SYSTEMS`) is already imported in `runs.py:31`; `SYSTEMS.get(conn, uuid)` returns a `System | None`. **`runs.system_id` is `NOT NULL REFERENCES systems(id)`** (`db/schema/0001_init.sql:83`, default RESTRICT), so a persisted Run always resolves a live System — the `system is None` guards added below are defensive type-narrowing (`SYSTEMS.get` is typed `System | None`), not a reachable runtime path, and are therefore not given dedicated tests.
- `Mapping` and `Run` are already imported (`runs.py:16`, `:34`). You will add `CaptureMethod`, `ResourceKind`, and `System` imports.
- In the **test** file (`tests/mcp/test_runs_tools.py`): `System`, `Run`, `ResourceKind` (line 20-27), `SystemState`/`RunState`/`AllocationState` (line 28-33), and `_VALID_BUILD` (line 480) already exist. `Jsonb` is **not** imported there yet — add `from psycopg.types.json import Jsonb` near the psycopg imports (line 15-16) when Task 3 needs it.

Run a single test with: `uv run python -m pytest tests/mcp/test_runs_tools.py::<name> -q`.

---

## Task 1: Add the method resolver (purely additive — no signature changes)

This task adds **new** symbols only; it does not touch `_cmdline_for` or any call site, so the full suite stays green.

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py` (imports near 33-34; new helpers near 642, after the existing `_CRASHKERNEL_TOKEN`/`_cmdline_for` block)
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Write the failing unit tests for the resolver**

Append to `tests/mcp/test_runs_tools.py` (the `CaptureMethod` import already exists at line 869):

```python
def _system_with_profile(profile: dict[str, Any]) -> System:
    return System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        allocation_id=uuid4(),
        state=SystemState.READY,
        provisioning_profile=profile,
    )


def _profile_dump(**local_libvirt: Any) -> dict[str, Any]:
    """A real ProvisioningProfile.model_dump(by_alias=True) — pins the 'local-libvirt' alias."""
    from kdive.profiles.provisioning import ProvisioningProfile

    section: dict[str, Any] = {"rootfs": {"kind": "path", "path": "/img"}}
    section.update(local_libvirt)
    return ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org#v6.9",
            "provider": {"local-libvirt": section},
        }
    ).model_dump(by_alias=True)


def test_install_method_kdump_when_crashkernel_set() -> None:
    system = _system_with_profile(_profile_dump(crashkernel="256M"))
    assert runs_tools._install_method_for(system) is CaptureMethod.KDUMP


def test_install_method_gdbstub_when_flag_set() -> None:
    system = _system_with_profile(_profile_dump(debug={"gdbstub": True}))
    assert runs_tools._install_method_for(system) is CaptureMethod.GDBSTUB


def test_install_method_host_dump_when_preserve_on_crash() -> None:
    system = _system_with_profile(_profile_dump(debug={"preserve_on_crash": True}))
    assert runs_tools._install_method_for(system) is CaptureMethod.HOST_DUMP


def test_install_method_console_for_bare_system() -> None:
    system = _system_with_profile(_profile_dump())
    assert runs_tools._install_method_for(system) is CaptureMethod.CONSOLE


def test_install_method_console_for_partial_profile_does_not_raise() -> None:
    # The minimal seed profile (no provider section) must resolve, not raise.
    system = _system_with_profile({"schema_version": 1})
    assert runs_tools._install_method_for(system) is CaptureMethod.CONSOLE


def test_install_method_reads_alias_not_attribute_spelling() -> None:
    # A crashkernel under the WRONG key 'local_libvirt' must NOT resolve kdump:
    # the resolver reads the persisted alias 'local-libvirt' (ADR-0051 Decision 1).
    system = _system_with_profile({"provider": {"local_libvirt": {"crashkernel": "256M"}}})
    assert runs_tools._install_method_for(system) is CaptureMethod.CONSOLE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_method"`
Expected: FAIL — `AttributeError: module 'kdive.mcp.tools.runs' has no attribute '_install_method_for'`.

- [ ] **Step 3: Add the imports**

In `src/kdive/mcp/tools/runs.py`, add the `CaptureMethod` import (sorts before `kdive.domain.errors` at line 33) and add `ResourceKind` + `System` to the `kdive.domain.models` import (line 34), keeping alphabetical order:

```python
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Investigation, Job, JobKind, ResourceKind, Run, Sensitivity, System
```

- [ ] **Step 4: Add the resolver helpers**

Insert directly **after** the existing `_cmdline_for` function (after `runs.py:652`), leaving `_DEFAULT_CMDLINE`/`_CRASHKERNEL_TOKEN`/`_cmdline_for` untouched for now:

```python
def _local_libvirt_section(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    """The `provider['local-libvirt']` section of a stored profile, or `{}` (loose read).

    Navigates the persisted **alias** key (`ResourceKind.LOCAL_LIBVIRT.value`, `"local-libvirt"`),
    which is what `ProvisioningProfile.model_dump(by_alias=True)` writes — not the Python
    attribute spelling `local_libvirt`. A missing/odd-shaped profile yields `{}` rather than
    raising, mirroring `_cmdline_for`'s loose read (ADR-0051 Decision 1).
    """
    provider = profile.get("provider")
    if not isinstance(provider, Mapping):
        return {}
    section = provider.get(ResourceKind.LOCAL_LIBVIRT.value)
    return section if isinstance(section, Mapping) else {}


def _install_method_for(system: System) -> CaptureMethod:
    """Resolve the capture method the System is provisioned for (ADR-0051 Decision 1).

    A non-empty `crashkernel` reservation means the System is provisioned for kdump
    (`crashkernel ⇔ kdump`, ADR-0049 §5); otherwise the `debug` flags select the non-kdump
    method, defaulting to the always-on `console` baseline (ADR-0049 §4).
    """
    section = _local_libvirt_section(system.provisioning_profile)
    crashkernel = section.get("crashkernel")
    if isinstance(crashkernel, str) and crashkernel.strip():
        return CaptureMethod.KDUMP
    debug = section.get("debug")
    debug = debug if isinstance(debug, Mapping) else {}
    if debug.get("gdbstub") is True:
        return CaptureMethod.GDBSTUB
    if debug.get("preserve_on_crash") is True:
        return CaptureMethod.HOST_DUMP
    return CaptureMethod.CONSOLE
```

- [ ] **Step 5: Run the resolver tests, then the full suite**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_method"`
Expected: PASS (6 tests).
Then run the full gate before committing: `just lint && just type && just test`
Expected: all PASS (this task is additive — no existing behavior changed).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): resolve capture method from the System profile

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Method-aware cmdline + rewire BOTH callers (atomic — one commit)

This is the rewire commit: it changes `_cmdline_for`'s signature **and** updates both call sites (`install_run`, `install_handler`) in the same change, so the suite never goes red. It also adds `_installed_initrd_ref` and threads `method`/`initrd_ref` through the handler.

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py` — constants/`_cmdline_for` (636-652), `install_run` (655-670), `install_handler` (752-794), new `_installed_initrd_ref` (after 346)
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Extend seed helpers + `_FakeInstaller`, then write the gate + handler tests**

In `tests/mcp/test_runs_tools.py`:

(a) Add `from psycopg.types.json import Jsonb` near line 15-16.

(b) Let `_seed_system` (line 64) accept a profile — add the kwarg and use it:

```python
async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    system_state: SystemState = SystemState.READY,
    alloc_state: AllocationState = AllocationState.ACTIVE,
    project: str = "proj",
    provisioning_profile: dict[str, Any] | None = None,
) -> str:
```

and replace the `provisioning_profile={"schema_version": 1}` argument inside the `System(...)` insert with:

```python
                provisioning_profile=provisioning_profile
                if provisioning_profile is not None
                else {"schema_version": 1},
```

(c) Thread `provisioning_profile: dict[str, Any] | None = None` through `_seed_run` (line 136) → pass to `_seed_system(pool, project=project, provisioning_profile=provisioning_profile)`; and through `_seed_succeeded_run` (line 916) → forward to `_seed_run`.

(d) Extend `_FakeInstaller` (line 881) to record `method`/`initrd_ref`:

```python
class _FakeInstaller:
    """Records install() calls (incl. method/initrd_ref); returns or raises a canned category."""

    def __init__(self, *, error: ErrorCategory | None = None) -> None:
        self.calls: list[tuple[UUID, UUID, str, str, CaptureMethod, str | None]] = []
        self._error = error

    def install(
        self,
        system_id: UUID,
        run_id: UUID,
        kernel_ref: str,
        *,
        cmdline: str,
        method: CaptureMethod = CaptureMethod.HOST_DUMP,
        initrd_ref: str | None = None,
    ) -> None:
        self.calls.append((system_id, run_id, kernel_ref, cmdline, method, initrd_ref))
        if self._error is not None:
            raise CategorizedError("boom", category=self._error)
```

(e) **Delete** `test_install_cmdline_without_crashkernel_is_config_error_no_job` (lines 1016-1027 — it asserts the OLD unconditional behavior #116 reverses).

(f) Add the cmdline-default unit tests, the gate tests, the ledger helper, and the handler-forwarding tests:

```python
def test_cmdline_default_is_kdump_reserving_for_kdump() -> None:
    run = _run_with_build_profile({"schema_version": 1})
    assert "crashkernel=" in runs_tools._cmdline_for(run, CaptureMethod.KDUMP)


def test_cmdline_default_omits_crashkernel_for_non_kdump() -> None:
    run = _run_with_build_profile({"schema_version": 1})
    assert "crashkernel=" not in runs_tools._cmdline_for(run, CaptureMethod.CONSOLE)


def test_cmdline_explicit_overrides_default_for_any_method() -> None:
    run = _run_with_build_profile({"cmdline": "dhash_entries=1"})
    assert runs_tools._cmdline_for(run, CaptureMethod.KDUMP) == "dhash_entries=1"


def _run_with_build_profile(build_profile: dict[str, Any]) -> Run:
    return Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        investigation_id=uuid4(),
        system_id=uuid4(),
        state=RunState.SUCCEEDED,
        build_profile=build_profile,
    )


def test_install_nonkdump_system_admits_cmdline_without_crashkernel(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": "console=ttyS0"}
            )  # bare System (default seed profile) => method console
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())


def test_install_kdump_system_without_crashkernel_is_config_error_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "console=ttyS0"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["reason"] == "cmdline_missing_crashkernel"
        assert njobs == 0

    asyncio.run(_run())


def test_install_kdump_system_with_crashkernel_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool,
                build_profile={**_VALID_BUILD, "cmdline": "console=ttyS0 crashkernel=256M"},
                provisioning_profile=_profile_dump(crashkernel="256M"),
            )
            resp = await _install(pool, _ctx(), run_id)
        assert resp.status == "queued"

    asyncio.run(_run())


async def _record_build_ledger(
    pool: AsyncConnectionPool, run_id: str, result: dict[str, Any]
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run_id, Jsonb(result)),
        )


def test_install_handler_forwards_console_method_for_bare_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)  # bare System => console
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][4] is CaptureMethod.CONSOLE
        assert installer.calls[0][5] is None  # no initrd

    asyncio.run(_run())


def test_install_handler_forwards_host_dump_for_preserve_on_crash(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, provisioning_profile=_profile_dump(debug={"preserve_on_crash": True})
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][4] is CaptureMethod.HOST_DUMP

    asyncio.run(_run())


def test_install_handler_forwards_initrd_ref_from_build_ledger(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_build_ledger(
                pool, run_id, {"kernel_ref": "k", "initrd_ref": "local/runs/x/initrd"}
            )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][5] == "local/runs/x/initrd"

    asyncio.run(_run())


def test_install_handler_no_initrd_when_ledger_initrd_blank(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_build_ledger(pool, run_id, {"kernel_ref": "k", "initrd_ref": ""})
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                await runs_tools.install_handler(conn, job, installer)
        assert installer.calls[0][5] is None

    asyncio.run(_run())
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "cmdline_default or cmdline_explicit or install_kdump_system or install_nonkdump_system or install_handler_forwards or install_handler_no_initrd"`
Expected: FAIL — `_cmdline_for` takes 1 positional arg (TypeError on the 2-arg unit-test calls); the kdump gate is still unconditional; the handler still passes the default `HOST_DUMP` and reads no ledger `initrd_ref`.

- [ ] **Step 3: Split the cmdline default and make `_cmdline_for` method-aware**

Replace lines 636-652 (`_DEFAULT_CMDLINE` through the end of `_cmdline_for`) with:

```python
# Default kernel command lines for direct-kernel boot, split by capture method. The kdump
# default carries a `crashkernel=` reservation (the kdump prerequisite); the non-kdump default
# does not — the non-kdump tiers (console/host_dump/gdbstub) boot without it (ADR-0049 §5,
# ADR-0051 §3). An operator override (the Run's `cmdline`) replaces the default entirely.
_KDUMP_DEFAULT_CMDLINE = "console=ttyS0 crashkernel=256M"
_NONKDUMP_DEFAULT_CMDLINE = "console=ttyS0"
_CRASHKERNEL_TOKEN = "crashkernel="


def _cmdline_for(run: Run, method: CaptureMethod) -> str:
    """Resolve the kernel command line from the Run's opaque `build_profile`.

    The cmdline is read from the raw `build_profile` dict (not via `BuildProfile.parse`, whose
    `extra="forbid"` would reject the `cmdline` key); an absent/blank value falls back to the
    method-appropriate default — the kdump default reserves `crashkernel=`, the non-kdump
    default does not (ADR-0051 §3).
    """
    value = run.build_profile.get("cmdline")
    if isinstance(value, str) and value.strip():
        return value
    return _KDUMP_DEFAULT_CMDLINE if method is CaptureMethod.KDUMP else _NONKDUMP_DEFAULT_CMDLINE
```

(The `_install_method_for`/`_local_libvirt_section` helpers from Task 1 sit just below this block — leave them.)

- [ ] **Step 4: Add `_installed_initrd_ref` (after `_existing_build_result`, ~line 346)**

```python
async def _installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The build ledger's recorded `initrd_ref`, or `None` (server builds record none).

    The external-build lane records the uploaded initrd's object key in the `(run_id, "build")`
    ledger result (`_finalize_external_build`); a blank/absent value means no external initrd is
    staged (a bzImage with an embedded initramfs), so the install emits no `<initrd>`.
    """
    result = await _existing_build_result(conn, run_id)
    if result is None:
        return None
    ref = result.get("initrd_ref")
    return ref if isinstance(ref, str) and ref else None
```

- [ ] **Step 5: Rewire `install_run` (caller #1) to gate on the resolved method**

Replace `install_run` (lines 655-670) with:

```python
async def install_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Admit an idempotent install for a built Run; require `crashkernel=` only for kdump.

    The capture method is resolved from the System's provisioning profile (ADR-0051 §1): a
    kdump-provisioned System (a `crashkernel` reservation) must carry a `crashkernel=` cmdline
    token; the non-kdump tiers are admitted without it.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            if run.state is not RunState.SUCCEEDED:
                return _config_error(run_id, data={"current_status": run.state.value})
            system = await SYSTEMS.get(conn, run.system_id)
            if system is None:  # defensive: runs.system_id is NOT NULL REFERENCES systems(id)
                return _config_error(run_id, data={"reason": "system_gone"})
            method = _install_method_for(system)
            if method is CaptureMethod.KDUMP and _CRASHKERNEL_TOKEN not in _cmdline_for(run, method):
                return _config_error(run_id, data={"reason": "cmdline_missing_crashkernel"})
            return await _enqueue_step(conn, ctx, run, JobKind.INSTALL, "install", "runs.install")
```

- [ ] **Step 6: Rewire `install_handler` (caller #2) to resolve + thread method/initrd_ref**

In `install_handler` (lines 761-780), replace the block from `run_id = UUID(...)` through the `installer.install(...)` call with:

```python
    run_id = UUID(job.payload["run_id"])
    run = await RUNS.get(conn, run_id)
    if run is None or run.kernel_ref is None:
        raise CategorizedError(
            "install target run is gone or unbuilt (no kernel_ref)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    system = await SYSTEMS.get(conn, run.system_id)
    if system is None:  # defensive: runs.system_id is NOT NULL REFERENCES systems(id)
        raise CategorizedError(
            "install target system is gone",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id), "system_id": str(run.system_id)},
        )
    method = _install_method_for(system)
    kernel_ref = run.kernel_ref
    cmdline = _cmdline_for(run, method)
    initrd_ref = await _installed_initrd_ref(conn, run_id)
    job_ctx = _ctx_from_job(job, run.project)

    async def _do() -> dict[str, Any]:
        await asyncio.to_thread(
            installer.install,
            run.system_id,
            run_id,
            kernel_ref,
            cmdline=cmdline,
            method=method,
            initrd_ref=initrd_ref,
        )
```

Delete the now-wrong comment block at the old `runs.py:774-777` (the `# `method`/`initrd_ref` are left at their defaults …` paragraph) — the behavior it described is replaced.

- [ ] **Step 7: Run the new tests, then the FULL suite**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "cmdline or install"`
Expected: PASS — the new gate/handler/cmdline tests plus every pre-existing `install`/`boot` test (which now exercise the method-aware path through the bare seed profile = console).
Then the pre-commit gate: `just lint && just type && just test`
Expected: all PASS, zero warnings. (`just type` is whole-tree.) **Do not commit on red.**

- [ ] **Step 8: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): method-conditional crashkernel gate; thread method/initrd_ref

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Tier-0 demo cmdlines pass the boundary (acceptance — additive)

**Files:**
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Write the acceptance test for both demo cmdlines**

```python
@pytest.mark.parametrize(
    "cmdline",
    ["dhash_entries=1 panic_on_oops=1", ""],
)
def test_install_tier0_demo_cmdlines_pass_boundary(migrated_url: str, cmdline: str) -> None:
    # Acceptance (#116): the Tier-0 demo cmdlines carry no crashkernel=; a bare (console)
    # System admits them through runs.install.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(
                pool, build_profile={**_VALID_BUILD, "cmdline": cmdline}
            )
            resp = await _install(pool, _ctx(), run_id)
            njobs = await _count(pool, "SELECT count(*) AS n FROM jobs", ())
        assert resp.status == "queued"
        assert njobs == 1

    asyncio.run(_run())
```

- [ ] **Step 2: Run the acceptance test**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py::test_install_tier0_demo_cmdlines_pass_boundary -q`
Expected: PASS (2 parametrizations) — no code change needed; this pins the acceptance criterion against the Task 2 gate.

- [ ] **Step 3: Commit**

```bash
git add tests/mcp/test_runs_tools.py
git commit -m "test(runs): Tier-0 demo cmdlines pass the install boundary

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Final guardrails + dead-reference sweep

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py` (only if a guardrail flags something)

- [ ] **Step 1: Confirm no stale references to the removed `_DEFAULT_CMDLINE`**

Run: `rg -n "_DEFAULT_CMDLINE\b" src tests`
Expected: no matches (renamed to `_KDUMP_DEFAULT_CMDLINE`/`_NONKDUMP_DEFAULT_CMDLINE`). The `complete_build` comment at `runs.py:508` mentions `_cmdline_for` generically — verify it does not name the old single-default constant; leave it otherwise.

- [ ] **Step 2: Run the full local gate**

Run: `just lint && just type && just test`
Expected: all PASS, zero warnings.

- [ ] **Step 3: Commit any guardrail fixes**

```bash
git add -A
git commit -m "chore(runs): satisfy lint/type after method threading

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(Skip this commit if Steps 1-2 produced no changes.)

---

## Self-review against the spec / ADR-0051

- **Acceptance 1 — admit non-kdump w/o crashkernel; reject kdump w/o it:** Task 2 (`test_install_nonkdump_system_admits...`, `test_install_kdump_system_without_crashkernel...`, `test_install_kdump_system_with_crashkernel_enqueues`). ✅
- **Acceptance 2 — `install_handler` forwards `method`/`initrd_ref`:** Task 2 (`test_install_handler_forwards_console_method...`, `..._host_dump...`, `..._initrd_ref_from_build_ledger`, `..._no_initrd_when_ledger_initrd_blank`). ✅
- **Acceptance 3 — Tier-0 demo cmdlines pass:** Task 3. ✅
- **ADR-0051 Decision 1 (resolve from profile, alias-keyed, loose read):** Task 1 (`test_install_method_*`, incl. the alias-vs-attribute and partial-profile tests). ✅
- **ADR-0051 Decision 3 (`_DEFAULT_CMDLINE` split):** Task 2 (`test_cmdline_default_*`). ✅
- **ADR-0051 Decision 4 (initrd_ref from the ledger; `_FakeInstaller` asserts forwarding):** Task 2. ✅
- **`system is None` guards:** defensive type-narrowing only — `runs.system_id` is `NOT NULL REFERENCES systems(id)` (RESTRICT), so the branch is unreachable for a persisted Run and is intentionally not given a DB test (would error in setup on the FK).
- **Out of scope (deferred, not in this plan):** flag-derived panic-escalation/`nokaslr` cmdline tokens (spec §8, Tier-1/2 plans); install-gate unsupported-method reject (ADR-0051 Decision 5).
