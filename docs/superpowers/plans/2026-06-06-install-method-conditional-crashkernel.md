# Install-time method-conditional crashkernel gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `runs.install` require a `crashkernel=` cmdline token only for a `kdump`-provisioned System, resolve the capture method from the System's provisioning profile, and thread `method`/`initrd_ref` from `install_handler` into `installer.install(...)`.

**Architecture:** Two pure helpers in `mcp/tools/runs.py` — `_install_method_for(system)` (maps the System's loosely-read `provider["local-libvirt"]` section to a `CaptureMethod`) and a method-aware `_cmdline_for(run, method)` (splits the default cmdline into kdump/non-kdump). `install_run` (the tool gate) and `install_handler` (the worker) both fetch the System, resolve the method, and gate/thread on it. `initrd_ref` is recovered from the build ledger.

**Tech Stack:** Python 3.13, `uv`, `pytest` (DB tests use disposable Postgres via `migrated_url`), `ruff`, `ty`. Design: [ADR-0051](../../adr/0051-install-method-conditional-crashkernel.md), spec [§8](../specs/2026-06-05-crash-capture-tiers-design.md).

---

## Background the engineer needs

- `runs.install` → `install_run(pool, ctx, run_id)` (`src/kdive/mcp/tools/runs.py:655`) is the **tool gate**: it validates the Run is `succeeded`, currently rejects any cmdline lacking `crashkernel=` (line 668), then enqueues the install job. It does **not** run the install — the worker does.
- `install_handler(conn, job, installer)` (`runs.py:752`) is the **worker**: it reads the Run, computes the cmdline, and calls `installer.install(...)`. The real installer is `LocalLibvirtInstall` (`src/kdive/providers/local_libvirt/install.py`); tests inject `_FakeInstaller`.
- `installer.install(system_id, run_id, kernel_ref, *, cmdline, method=CaptureMethod.HOST_DUMP, initrd_ref=None)` (`install.py:154`) already runs `_kdump_check` **only** for `method == CaptureMethod.KDUMP` and emits an `<initrd>` only when `initrd_ref` is not `None`. The tool layer just never passes `method`/`initrd_ref`.
- `CaptureMethod` is `kdive.domain.capture.CaptureMethod` (`CONSOLE`, `HOST_DUMP`, `GDBSTUB`, `KDUMP`).
- A System's `provisioning_profile` (`System.provisioning_profile: dict[str, Any]`) is a `ProvisioningProfile.model_dump(by_alias=True)` in production (`mcp/tools/systems.py:335/476/743`). **The provider section lives under the alias key `"local-libvirt"`** (`ResourceKind.LOCAL_LIBVIRT.value`), **not** `"local_libvirt"`. The kdump prerequisite is `provider["local-libvirt"]["crashkernel"]` (a non-empty string or absent); the debug flags are `provider["local-libvirt"]["debug"]["preserve_on_crash"]` and `["gdbstub"]` (booleans).
- The build ledger row `(run_id, "build")` is read by `_existing_build_result(conn, run_id)` (`runs.py:336`). The external-build lane records `initrd_ref` there (`runs.py:513`, the uploaded initrd's object key or `""`); the server-build lane records no `initrd_ref` key (`runs.py:625-629`).
- `SYSTEMS` repository (`kdive.db.repositories.SYSTEMS`) is already imported in `runs.py:31`; `SYSTEMS.get(conn, uuid)` returns a `System | None`.
- `Mapping` and `Run` are already imported (`runs.py:16`, `:34`). You will add `CaptureMethod` and `ResourceKind` imports.

Run a single test with: `uv run python -m pytest tests/mcp/test_runs_tools.py::<name> -q`.
The whole-tree type check is `just type`; lint is `just lint`; the suite is `just test`.

---

## Task 1: Method resolver + method-aware cmdline default (pure helpers)

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py` (imports near line 33-34; constants/helpers near 636-652)
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Write the failing unit tests for the resolver and cmdline split**

Add near the other install tests in `tests/mcp/test_runs_tools.py` (the `CaptureMethod` import already exists at line 869; add a `System`/`Run` builder using the existing `_DT`). Append:

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


def test_cmdline_default_is_kdump_reserving_for_kdump() -> None:
    run = _run_with_build_profile({"schema_version": 1})
    assert "crashkernel=" in runs_tools._cmdline_for(run, CaptureMethod.KDUMP)


def test_cmdline_default_omits_crashkernel_for_non_kdump() -> None:
    run = _run_with_build_profile({"schema_version": 1})
    assert "crashkernel=" not in runs_tools._cmdline_for(run, CaptureMethod.CONSOLE)


def test_cmdline_explicit_overrides_default_for_any_method() -> None:
    run = _run_with_build_profile({"cmdline": "console=ttyS0 dhash_entries=1"})
    assert runs_tools._cmdline_for(run, CaptureMethod.KDUMP) == "console=ttyS0 dhash_entries=1"


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_method or cmdline_default or cmdline_explicit"`
Expected: FAIL — `AttributeError: module ... has no attribute '_install_method_for'` and `_cmdline_for` taking one positional arg (TypeError on the 2-arg calls).

- [ ] **Step 3: Add the imports**

In `src/kdive/mcp/tools/runs.py`, extend the domain imports. Change line 34 region to add `ResourceKind` and add the `CaptureMethod` import after it:

```python
from kdive.domain.capture import CaptureMethod
from kdive.domain.models import Investigation, Job, JobKind, ResourceKind, Run, Sensitivity
```

(Keep the import block alphabetically grouped as ruff's `I` rule expects; `from kdive.domain.capture import CaptureMethod` sorts before `from kdive.domain.errors import ...` at line 33.)

- [ ] **Step 4: Replace the cmdline constants and helper, add the resolver**

Replace lines 636-652 (`_DEFAULT_CMDLINE` through the end of `_cmdline_for`) with:

```python
# Default kernel command lines for direct-kernel boot, split by capture method. The kdump
# default carries a `crashkernel=` reservation (the kdump prerequisite); the non-kdump default
# does not — the non-kdump tiers (console/host_dump/gdbstub) boot without it (ADR-0049 §5,
# ADR-0051 §3). An operator override (the Run's `cmdline`) replaces the default entirely.
_KDUMP_DEFAULT_CMDLINE = "console=ttyS0 crashkernel=256M"
_NONKDUMP_DEFAULT_CMDLINE = "console=ttyS0"
_CRASHKERNEL_TOKEN = "crashkernel="


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

You will also need `System` in scope. It is imported from `kdive.domain.models`? Check the import line — if `System` is not already imported there, add it. (As of writing, `runs.py:34` imports `Investigation, Job, JobKind, Run, Sensitivity` — **add `System`** to that list as well, keeping alphabetical order: `Investigation, Job, JobKind, ResourceKind, Run, Sensitivity, System`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_method or cmdline_default or cmdline_explicit"`
Expected: PASS (9 tests).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): resolve capture method from the System profile

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Make the `runs.install` gate method-conditional

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py:655-670` (`install_run`)
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Allow seeding a System's provisioning profile, then write the gate tests**

First extend the seed helpers so a test can choose the System's profile. In `tests/mcp/test_runs_tools.py`:

Change `_seed_system` (line 64) to accept a profile and use it instead of the hard-coded `{"schema_version": 1}`:

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

and inside, replace the `provisioning_profile={"schema_version": 1}` argument to `System(...)` with:

```python
                provisioning_profile=provisioning_profile
                if provisioning_profile is not None
                else {"schema_version": 1},
```

Thread it through `_seed_run` (line 136) — add `provisioning_profile: dict[str, Any] | None = None` to its signature and pass it to `_seed_system(pool, project=project, provisioning_profile=provisioning_profile)`. Thread it through `_seed_succeeded_run` (line 916) the same way — add the kwarg and forward it to `_seed_run`.

Now rewrite the obsolete test and add the kdump/non-kdump pair. Delete `test_install_cmdline_without_crashkernel_is_config_error_no_job` (lines 1016-1027 — it asserts the OLD unconditional behavior #116 reverses) and add:

```python
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
```

(`_profile_dump` was added in Task 1; `_VALID_BUILD` already exists in the file.)

- [ ] **Step 2: Run to verify the new gate tests fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_kdump_system or install_nonkdump_system"`
Expected: FAIL — the kdump-without-crashkernel case still enqueues (today's gate is unconditional and the seed cmdline `console=ttyS0` *would* be rejected for *every* System, so `test_install_nonkdump_system_admits...` fails: status is `error`, not `queued`).

- [ ] **Step 3: Rewrite `install_run` to gate on the resolved method**

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
            if system is None:
                return _config_error(run_id, data={"reason": "system_gone"})
            method = _install_method_for(system)
            if method is CaptureMethod.KDUMP and _CRASHKERNEL_TOKEN not in _cmdline_for(run, method):
                return _config_error(run_id, data={"reason": "cmdline_missing_crashkernel"})
            return await _enqueue_step(conn, ctx, run, JobKind.INSTALL, "install", "runs.install")
```

- [ ] **Step 4: Run the gate tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_kdump_system or install_nonkdump_system or install_succeeded or install_on_unbuilt or install_on_terminal or install_cross_project or install_malformed or install_without_operator"`
Expected: PASS (all existing install-gate tests plus the three new ones; the previously-removed test is gone).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): gate crashkernel cmdline token on kdump method

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Thread `method`/`initrd_ref` from `install_handler` to the provider

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py:752-794` (`install_handler`) + add `_installed_initrd_ref`
- Test: `tests/mcp/test_runs_tools.py` (extend `_FakeInstaller`)

- [ ] **Step 1: Extend `_FakeInstaller` to record method/initrd_ref, write forwarding tests**

In `tests/mcp/test_runs_tools.py`, change `_FakeInstaller` (line 881) to capture the new params:

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

Add a helper to record a build ledger row with an `initrd_ref`, and the forwarding tests:

```python
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


def test_install_handler_missing_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET system_id=%s WHERE id=%s", (str(uuid4()), run_id)
                )
            job = await _enqueue_job(pool, JobKind.INSTALL, run_id, "install")
            installer = _FakeInstaller()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_tools.install_handler(conn, job, installer)
            assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert installer.calls == []

    asyncio.run(_run())
```

`Jsonb` is already imported (`runs.py` uses it; in the test it comes from `psycopg.types.json`). If `Jsonb` is not yet imported in the **test** file, add `from psycopg.types.json import Jsonb` near the other psycopg imports.

- [ ] **Step 2: Run to verify the forwarding tests fail**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_handler_forwards or install_handler_no_initrd or install_handler_missing_system"`
Expected: FAIL — `install_handler` does not fetch the System, always passes the default `HOST_DUMP` (so the `console`/`host_dump` asserts and the missing-system raise fail), and never reads the ledger for `initrd_ref`.

- [ ] **Step 3: Add `_installed_initrd_ref` and rewrite `install_handler`'s resolution**

In `src/kdive/mcp/tools/runs.py`, add a ledger reader next to `_existing_build_result` (after line 346):

```python
async def _installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The build ledger's recorded `initrd_ref`, or `None` (server builds record none).

    The external-build lane records the uploaded initrd's object key in the `(run_id, "build")`
    ledger result (`_finalize_external_build`); a blank/absent value means no external initrd
    is staged (a bzImage with an embedded initramfs), so the install emits no `<initrd>`.
    """
    result = await _existing_build_result(conn, run_id)
    if result is None:
        return None
    ref = result.get("initrd_ref")
    return ref if isinstance(ref, str) and ref else None
```

Then in `install_handler` (lines 761-780), replace the block from `run = await RUNS.get(conn, run_id)` through the `_do` body's `installer.install(...)` call with:

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
    if system is None:
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

Delete the obsolete `# `method`/`initrd_ref` are left at their defaults ...` comment block (the old `runs.py:774-777`) — it is now wrong and the behavior it described is replaced.

- [ ] **Step 4: Run the forwarding tests and the full install-handler set to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_runs_tools.py -q -k "install_handler"`
Expected: PASS — the existing handler tests (records-step, replay, concurrent, failure, missing-kernel) plus the new forwarding/missing-system tests.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/runs.py tests/mcp/test_runs_tools.py
git commit -m "feat(runs): thread capture method and initrd_ref into install

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Tier-0 demo cmdlines pass the boundary (acceptance)

**Files:**
- Test: `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Write the acceptance test for both demo cmdlines**

```python
@pytest.mark.parametrize(
    "cmdline",
    ["console=ttyS0 dhash_entries=1 panic_on_oops=1", "console=ttyS0"],
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

## Task 5: Full guardrails green + dead-comment sweep

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py` (only if the guardrails flag something)

- [ ] **Step 1: Confirm no stale references to the removed `_DEFAULT_CMDLINE`**

Run: `rg -n "_DEFAULT_CMDLINE\b" src tests`
Expected: no matches (the constant was renamed to `_KDUMP_DEFAULT_CMDLINE`/`_NONKDUMP_DEFAULT_CMDLINE` in Task 1). If a stray `complete_build` comment at `runs.py:508` mentions `_cmdline_for`, leave it (still accurate) — but verify it does not reference the old single-default name.

- [ ] **Step 2: Run the full local gate**

Run: `just lint && just type && just test`
Expected: all PASS, zero warnings. `just type` is whole-tree (src + tests). If `ty` flags the new `_FakeInstaller.calls` tuple or the `System` import, fix it before continuing.

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
- **Acceptance 2 — `install_handler` forwards `method`/`initrd_ref`:** Task 3 (`test_install_handler_forwards_console_method...`, `..._host_dump...`, `..._initrd_ref_from_build_ledger`, `..._no_initrd_when_ledger_initrd_blank`). ✅
- **Acceptance 3 — Tier-0 demo cmdlines pass:** Task 4. ✅
- **ADR-0051 Decision 1 (resolve from profile, alias-keyed, loose read):** Task 1 (`test_install_method_*`, incl. the alias-vs-attribute and partial-profile tests). ✅
- **ADR-0051 Decision 3 (`_DEFAULT_CMDLINE` split):** Task 1 (`test_cmdline_default_*`). ✅
- **ADR-0051 Decision 4 (initrd_ref from the ledger; `_FakeInstaller` asserts forwarding):** Task 3. ✅
- **ADR-0051 fail-fast on a gone System:** Task 2 (`system_gone` branch) + Task 3 (`test_install_handler_missing_system_is_config_error`). ✅
- **Out of scope (deferred, not in this plan):** flag-derived panic-escalation/`nokaslr` cmdline tokens (spec §8, Tier-1/2 plans); install-gate unsupported-method reject (ADR-0051 Decision 5).
