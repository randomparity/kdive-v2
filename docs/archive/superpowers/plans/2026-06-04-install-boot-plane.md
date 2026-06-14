# Install + boot plane (local libvirt) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stage the built kernel for direct-kernel boot with a `crashkernel=` reservation (`runs.install`) and boot it with a run-readiness preflight (`runs.boot`), as two idempotent `(run_id, step)` jobs on a `succeeded` Run — without touching the Run state machine.

**Architecture:** A realized `Installer`/`Booter` port (`providers/local_libvirt/install.py`, one `LocalLibvirtInstall` class) redefines the System-tagged libvirt domain to a direct-kernel `<os>` (`<kernel>`/`<initrd>`/`<cmdline>`) referencing a per-Run host-local staging path, power-cycles the domain, and polls a ported run-readiness preflight within an injected boot window. `runs.install` synchronously parses the Run's `build_profile` cmdline and rejects a missing `crashkernel=` (`configuration_error`) before enqueuing a `JobKind.INSTALL` job; `runs.boot` requires a **succeeded** `install` step before enqueuing `JobKind.BOOT`. Both handlers wrap their body in the existing `run_step` helper, so a succeeded step replays from the ledger and a **failed** step records no row and dead-letters the job with its category (the Run stays `succeeded`). The real libvirt/object-store path is `live_vm`-gated; unit tests inject fake libvirt-conn, fetch, and readiness seams.

**Tech Stack:** Python 3.13 · `psycopg` 3 (async) · `libvirt-python` · FastMCP 3.x · `pytest` (testcontainers Postgres) · `ruff`/`ty`.

**Design source:** [`../specs/2026-06-04-install-boot-plane-design.md`](../specs/2026-06-04-install-boot-plane-design.md) · [`../../adr/0030-install-boot-plane.md`](../../adr/0030-install-boot-plane.md). The spec's §3 (lifecycle/failure contract), §5 (install), §6 (boot), §7 (idempotency), and §8 (failure table) are authoritative; each task references its slice.

---

## File structure

- **Create** `src/kdive/providers/local_libvirt/install.py` — `Installer`/`Booter` Protocols, `LocalLibvirtInstall.{from_env,install,boot}`, the direct-kernel `<os>` XML render, the per-Run staging path, the readiness-poll loop. Mirrors `providers/local_libvirt/build.py` (realized port + injected seams) and `…/control.py` (libvirt connect seam, `_close`).
- **Modify** `src/kdive/mcp/tools/runs.py` — add `install_run`/`boot_run` (the tool bodies), `install_handler`/`boot_handler`, the cmdline-build + `crashkernel=` gate, and the `install → boot` ordering check; register `runs.install`/`runs.boot` in `register`; bind `JobKind.INSTALL`/`BOOT` in the existing `register_handlers`. **Do not disturb** the existing `build_*` functions.
- **Create** `tests/providers/local_libvirt/test_install.py` — `LocalLibvirtInstall` unit tests (fake libvirt conn / fake fetch / fake readiness seams); a `live_vm`-gated real redefine+boot test.
- **Modify** `tests/mcp/test_runs_tools.py` — add `runs.install`/`runs.boot` tool tests + `install_handler`/`boot_handler` tests (real Postgres, fake `Installer`/`Booter`). Add an `install`/`boot` arm to the `register_handlers` binding test.

**No schema, no `app.py`, no `errors.py` edit** — `JobKind.INSTALL`/`BOOT`, the `install_failure`/`boot_timeout`/`readiness_failure` categories, the `boot` audit `tool` value, and the generic `run_steps.step` column all already exist (spec §8); `runs.register_handlers` already sits in `app.py`'s `_HANDLER_REGISTRARS` and only gains two `registry.register` calls internally.

> **Precondition for the DB-backed tests:** the `runs.*` tool/handler tests (Tasks 2–4) use the testcontainers Postgres `migrated_url` fixture, so **Docker must be running**; without it those tests error at fixture setup (not red/green). The Installer/Booter unit tests (Task 1) are pure and need no DB.
>
> Each commit keeps all guardrails green: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`, `uv run python -m pytest -q`. Verify `git log -1 --oneline` after every commit (prek may roll back a `ruff format` rewrite).

---

## Task 1: The `Installer`/`Booter` ports and `LocalLibvirtInstall`

**Files:** Create `src/kdive/providers/local_libvirt/install.py`, `tests/providers/local_libvirt/test_install.py` (spec §4, §5, §6)

- [ ] **Step 0 (read first — confirm seams against the siblings):** Read `providers/local_libvirt/control.py` (the `connect`/`_LibvirtConn`/`_LibvirtDomain` Protocols, `_close`, the `VIR_ERR_*` idempotency handling, `from_env` over `KDIVE_LIBVIRT_URI`) and `providers/local_libvirt/provisioning.py` (`domain_name_for(system_id)`, `defineXML`, the `xml.etree.ElementTree` render with no string interpolation, the kdive metadata tag). The install render **adds an `<os>` direct-kernel section** to the same domain shape; reuse `domain_name_for` and the `ET`-construction discipline (no f-string XML — a cmdline value must not be able to inject XML). Confirm the object-store read API the `fetch_kernel` seam wraps (`store.get_artifact` or equivalent in `store/objectstore.py`).
- [ ] **Step 1 (tests first):** Write `tests/providers/local_libvirt/test_install.py`. Inject fakes for every slow/host op (`connect` → a fake `_LibvirtConn` recording `defineXML`/`lookupByName`/`create`/`destroy`; `fetch_kernel`/`fetch_initrd` → write canned bytes to the given path; `kdump_check` → a canned present/absent result; `readiness` → a canned structured result; an injected `boot_window`/`clock` so no real wait):
  - **install render:** `install(system_id, run_id, kernel_ref, cmdline="… crashkernel=256M …")` (the committed Protocol signature, spec §4) calls `defineXML` once with XML that parses (via `ET.fromstring`) to a `<domain>` whose `<os>` has `<kernel>`, `<initrd>`, and `<cmdline>` text equal to the cmdline, and whose `<kernel>`/`<initrd>` point at the **per-Run staging path** `…/{system_id}/{run_id}/…`;
  - **kdump prerequisite (inside `install`):** with the `kdump_check` seam reporting the capture path **present**, `install` redefines; with it reporting **absent**, `install` raises `CategorizedError(CONFIGURATION_ERROR)` **before** `defineXML` (assert no redefine);
  - **fetch atomicity:** the fetch writes via a temp file + rename (assert the seam is called with a temp target then the final path exists; a fetch raising mid-way leaves no file at the final path);
  - a libvirt error on `defineXML` → `CategorizedError(INSTALL_FAILURE)`;
  - **boot happy path:** `boot(system_id)` `destroy`s (if running) then `create`s, then the readiness seam returns *pass* → no raise; assert `create` called;
  - **boot timeout:** the readiness seam reports *never-answered* for the whole boot window → `CategorizedError(BOOT_TIMEOUT)` (assert the poll stopped at the injected window, no real sleep);
  - **readiness failure:** the readiness seam reports *answered-but-failed* → `CategorizedError(READINESS_FAILURE)`;
  - a libvirt error on `destroy`/`create` → `CategorizedError(INSTALL_FAILURE)`;
  - `from_env` does not connect/spawn (lazy), mirroring `control.from_env`/`provisioning.from_env`.
  Run; expect FAIL (module absent).
- [ ] **Step 2:** Implement `src/kdive/providers/local_libvirt/install.py`:
  - `Installer(Protocol)` (`install(self, system_id, run_id, kernel_ref, *, cmdline) -> None`, per spec §4) and `Booter(Protocol)` (`boot(self, system_id) -> None`). The `run_id` keys the per-Run staging path (spec §5.2). Keep one `LocalLibvirtInstall` satisfying both.
  - `LocalLibvirtInstall(__init__(*, connect, fetch_kernel, fetch_initrd, kdump_check, readiness, staging_root, boot_window))` — slow ops injected; `from_env()` defaults `connect` to `lambda: libvirt.open(KDIVE_LIBVIRT_URI)`, `fetch_*` to object-store reads, `kdump_check` (install-time capture-path-present check) and `readiness` (boot-time preflight) to the ported preflight machinery, `staging_root` to `KDIVE_INSTALL_STAGING` (default under `/var/lib/kdive`), `boot_window` to a small default. (`kdump_check` and `readiness` may be the same ported module exposing two entry points; they are injected separately so tests drive each independently.)
  - `install(system_id, run_id, kernel_ref, *, cmdline)`: compute the per-Run staging dir `staging_root/{system_id}/{run_id}`; `fetch_kernel`/`fetch_initrd` into it via temp-file-then-rename; run `kdump_check` (capture service/initramfs present) — **absent → `CategorizedError(CONFIGURATION_ERROR)` before any redefine**; render the direct-kernel `<os>` domain XML with `ET` (reuse `domain_name_for`, the kdive metadata tag, the rootfs disk; **add** `<os>` `<kernel>`/`<initrd>`/`<cmdline>`); `defineXML` (libvirt error → `INSTALL_FAILURE`). `install()` therefore owns staging, the kdump prerequisite, and the redefine; the handler (Task 3) only invokes it under `run_step`.
  - `boot(system_id)`: open conn; `lookupByName(domain_name_for(system_id))`; `destroy` if running (swallow not-running per `control.py`'s `VIR_ERR_OPERATION_INVALID`), then `create` (libvirt error → `INSTALL_FAILURE`); poll `readiness` until pass or `boot_window` elapses — *never-answered* at window → `BOOT_TIMEOUT`, *answered-but-failed* → `READINESS_FAILURE`. Use the injected clock; no real `sleep` in tests.
  Type the libvirt seam per the repo memory: ignore `invalid-argument-type` at the connect seam only if needed, **not** `unresolved-import` (libvirt-python is ty-resolvable). Re-run unit tests; expect PASS.
- [ ] **Step 3 (`live_vm` real test):** Add a real redefine+boot test decorated **exactly** `@pytest.mark.live_vm` (copy the marker name from `test_build.py`; `--strict-markers` is not set, so a typo silently no-ops the gate). All libvirt/host access lives **inside the test body**, never at import/collection (a stock runner must still *collect* the file). The test redefines a real domain for direct-kernel boot and boots it; mark `# pragma: no cover`. Verify the default suite reports this test **`skipped`** on a runner without `KDIVE_LIBVIRT_URI`/KVM: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -rs -q` shows a skip line.
- [ ] **Step 4:** Guardrails green; commit `feat(install): add Installer/Booter port + LocalLibvirtInstall (#19)`.

## Task 2: `runs.install` tool (synchronous admission + `crashkernel=` gate)

**Files:** Modify `src/kdive/mcp/tools/runs.py`, `tests/mcp/test_runs_tools.py` (spec §5, §8)

- [ ] **Step 0 (read first):** Re-read `runs.py`'s existing `build_run`/`_build_locked`/`_authorizing`/`_run_job_envelope`/`_config_error`/`_stale_handle` — the install tool **reuses** `_authorizing`, `_run_job_envelope`, `_config_error`, `_as_uuid`, the `RUN` lock, and `queue.enqueue` (already imported). **Cmdline source (decided):** read the cmdline from the raw `run.build_profile` dict's optional `cmdline` key — **not** via `BuildProfile.parse`, whose `extra="forbid"` would reject the unknown `cmdline` key (a real bug). Absent → a module-level default const that includes `crashkernel=`. Pin this read as the test contract.
- [ ] **Step 1 (tests first):** Add `runs.install` tool tests to `tests/mcp/test_runs_tools.py` (seed Runs via `_seed_run`; the install job is admitted, not dispatched):
  - a `succeeded` Run with a default/absent cmdline → queued **job** handle (status `queued`, `data.run_id` set), exactly one `install` job with `dedup_key = f"{run_id}:install"`, the Run **still `succeeded`** (no state flip), an audit `install` row present;
  - idempotent re-issue → **same** `object_id`, still one job;
  - a Run whose `build_profile.cmdline` is set **without** `crashkernel=` → synchronous `configuration_error`, **no job** (`count(jobs)=0`);
  - install on a `created`/`running` Run → `configuration_error` with `data.current_status`;
  - install on a `failed`/`canceled` Run → `configuration_error` with `data.current_status`;
  - cross-project / missing / bad-uuid Run → not-found `configuration_error`;
  - a `viewer` ctx → raises `AuthorizationError`.
  Run; expect FAIL (`install_run`/`runs.install` absent).
- [ ] **Step 2:** Implement `install_run(pool, ctx, run_id)` in `runs.py` per spec §5: uuid-parse guard; load Run; not-found/cross-project → `_config_error`; `require_role(OPERATOR)`; if Run state ∉ `{succeeded}` → `_config_error(data={"current_status": state})`; build the cmdline from `run.build_profile.get("cmdline")` (default const with `crashkernel=`); if `"crashkernel=" not in cmdline` → `_config_error` (synchronous, no job); else under `conn.transaction()` + `advisory_xact_lock(RUN, run_id)` enqueue `queue.enqueue(JobKind.INSTALL, {"run_id": str(run_id)}, _authorizing(...), f"{run_id}:install")` + audit `install`; return `_run_job_envelope(job, run_id)`. Register `runs.install` in `register`. Re-run; expect PASS.
- [ ] **Step 3:** Guardrails green; commit `feat(runs): add runs.install admission tool (#19)`.

## Task 3: `install_handler` (the worker — stage + kdump prerequisite)

**Files:** Modify `src/kdive/mcp/tools/runs.py`, `tests/mcp/test_runs_tools.py` (spec §3, §5.2, §5.3, §7)

- [ ] **Step 1 (tests first):** Add `install_handler` tests (call the handler directly with a fake `Installer` and a real claimed `Job`):
  - happy path: handler runs, `installer.install` called once with the Run's `system_id`/`run_id`/`kernel_ref`/cmdline, exactly one `(run_id,"install")` `run_steps` row, the Run **still `succeeded`** (assert state unchanged), an audit `install` row;
  - **replay/no-restage:** invoke the handler a second time for the same job → `installer.install` **not** called again (fake call-count stays 1), still one ledger row;
  - **install failure:** the fake raises `CategorizedError(INSTALL_FAILURE)` → handler re-raises, **no** `run_steps` row (`count=0`), Run still `succeeded`;
  - **kdump prerequisite absent:** the fake `Installer.install` raises `CategorizedError(CONFIGURATION_ERROR)` (the capture-path check lives in `install()`, Task 1) → handler re-raises, no ledger row;
  - **kernel_ref absent:** a `succeeded` Run with `kernel_ref IS NULL` (defensive) → `CategorizedError(CONFIGURATION_ERROR)` or `INFRASTRUCTURE_FAILURE` (pick the more specific; the build handler always sets `kernel_ref` on `succeeded`, so this is a guard, not an expected path — assert no ledger row).
  Run; expect FAIL (`install_handler` absent).
- [ ] **Step 2:** Implement `install_handler(conn, job, installer)` per spec: resolve `run_id` from payload; read Run; guard `kernel_ref` present (else the specific categorized error); wrap the body in `run_step(conn, run_id, "install", fn)` where `fn` reads the cmdline from `run.build_profile`, calls `await asyncio.to_thread(installer.install, system_id, run_id, run.kernel_ref, cmdline=cmdline)` (the kdump-capture-present check runs inside `install()` — Task 1 — raising `CONFIGURATION_ERROR`), audits the `install` step (use `_ctx_from_job`), and returns a small result dict (`{"domain": …, "cmdline_has_crashkernel": True}`). On a `CategorizedError` the body raises **before** `run_step` writes (spec §3) — re-raise so the worker dead-letters; the Run is untouched. Return `str(run_id)`. Bind `JobKind.INSTALL` in `register_handlers`. Re-run; expect PASS.
- [ ] **Step 3:** Guardrails green; commit `feat(runs): add install handler + register on the worker seam (#19)`.

## Task 4: `runs.boot` tool + `boot_handler` (the worker — boot + readiness)

**Files:** Modify `src/kdive/mcp/tools/runs.py`, `tests/mcp/test_runs_tools.py` (spec §6, §3, §7, §8)

- [ ] **Step 1 (tests first):** Add `runs.boot` tool tests and `boot_handler` tests:
  - **tool — install-ordering gate:** boot a `succeeded` Run with **no** `(run_id,"install")` ledger row → `configuration_error` "install first", no `boot` job;
  - **tool — admitted:** seed a succeeded `install` ledger row, then boot → queued `boot` job (`dedup_key = f"{run_id}:boot"`), Run still `succeeded`, idempotent re-issue returns the same job;
  - **tool — Run not succeeded / terminal** → `configuration_error` with `current_status`; cross-project/missing/bad-uuid → `configuration_error`; `viewer` → `AuthorizationError`;
  - **handler happy path:** fake `Booter.boot` returns; one `(run_id,"boot")` ledger row, Run still `succeeded`, audit `boot` row;
  - **handler replay:** second dispatch → `booter.boot` not called again, one ledger row;
  - **handler boot_timeout:** fake raises `BOOT_TIMEOUT` → re-raise, no ledger row, Run unchanged;
  - **handler readiness_failure:** fake raises `READINESS_FAILURE` → re-raise, no ledger row, Run unchanged.
  Run; expect FAIL (`boot_run`/`boot_handler` absent).
- [ ] **Step 2:** Implement `boot_run(pool, ctx, run_id)` per spec §6: uuid/not-found/cross-project guards; `require_role(OPERATOR)`; Run state ∉ `{succeeded}` → `_config_error(current_status)`; check a **succeeded** `(run_id,"install")` `run_steps` row exists (a short read) → absent → `_config_error` "install first"; else under `RUN` lock enqueue `queue.enqueue(JobKind.BOOT, …, f"{run_id}:boot")` + audit `boot`; return `_run_job_envelope`. Implement `boot_handler(conn, job, booter)`: wrap `run_step(conn, run_id, "boot", fn)` where `fn` calls `await asyncio.to_thread(booter.boot, system_id)`, audits the `boot` step, returns a result dict; a `CategorizedError` (`BOOT_TIMEOUT`/`READINESS_FAILURE`/`INSTALL_FAILURE`) raises before `run_step` writes — re-raise. Register `runs.boot` in `register`; bind `JobKind.BOOT` in `register_handlers`. Re-run; expect PASS.
- [ ] **Step 3:** Extend the `register_handlers` binding test: assert `build_handler_registry().get(JobKind.INSTALL)` **and** `.get(JobKind.BOOT)` are bound (alongside the existing `BUILD` assertion). Re-run; expect PASS.
- [ ] **Step 4:** Guardrails green; commit `feat(runs): add runs.boot tool + boot handler (#19)`.

## Task 5: Full-suite + acceptance sweep

- [ ] **Step 1:** Run the whole suite `uv run python -m pytest -q` (Docker up) and `-m "not live_vm"` to confirm the two new gated tests skip. Map each spec §9 acceptance row to a passing test; fill any gap. Confirm the existing `build_*` tests still pass unchanged (the additions did not disturb them).
- [ ] **Step 2:** `uv run ruff check`, `uv run ruff format --check`, `uv run ty check src` — zero warnings. Pre-commit also `ty`-checks tests; let it run.
- [ ] **Step 3:** If anything changed, commit; verify HEAD landed.

---

## Verification & rollback

- **Per-task verification:** each task ends with the four guardrails green and a HEAD check.
- **Acceptance proof (issue #19 acceptance):** a kernel command line without `crashkernel=` is rejected at install with `configuration_error` (Task 2 tool test); install stages the built kernel for direct-kernel boot with the reservation (Task 1 render test + Task 3 handler test); boot brings the System up and the readiness preflight passes (Task 1 boot happy-path + Task 4 handler test). The "install+boot brings the System up **on the built kernel** against a real host" half is `live_vm`-deferred (Task 1 Step 3 gated test), recorded in the spec.
- **Rollback:** every task is an isolated commit on `feat/install-boot-19`; revert a task's commit to undo it. **No migrations, no schema/`app.py`/`errors.py` edits**, no destructive external ops — the only DB writes are `run_steps`/`jobs`/`audit_log` rows guarded by existing transitions (the Run `state` is never changed by this plane), so a revert needs no data cleanup. The libvirt redefine/boot and object-store reads are touched only on the `live_vm` path (skipped in CI).
- **Shared-file risk:** `runs.py` is touched by sibling #24; the additions are localized new functions + two `register`/`register_handlers` lines and are called out in NOTES. `app.py`, `errors.py`, and the schema are **not** edited by this plane.
