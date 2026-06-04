# Build plane (local make) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a kernel from source as an idempotent `runs.build` job that records two artifacts on the Run — the bootable `kernel_ref` and a build-id-keyed `debuginfo_ref` — and drives the Run `created → running → succeeded|failed`.

**Architecture:** A DB-free `BuildProfile` schema (`profiles/build.py`) parses the opaque `build_profile` jsonb the Run already carries. A realized `Builder` port (`providers/local_libvirt/build.py`) runs `make` in a warm workspace, preflights the kdump/debuginfo config, extracts the `vmlinux` GNU build-id, and stores two `sensitive` artifacts under deterministic Run-keyed object keys. `runs.build` synchronously drives `created → running` and enqueues a `build` job under a per-Run advisory lock; `build_handler` builds **with no DB transaction held across `make`**, then records the step ledger and finalizes the Run (setting both refs) in one short fenced transaction. The real `make`/object-store path is `live_vm`-gated; unit tests inject a fake `Builder` + fake store.

**Tech Stack:** Python 3.13 · `psycopg` 3 (async) · Pydantic v2 · FastMCP 3.x · `boto3` (object store) · `pytest` (testcontainers Postgres) · `ruff`/`ty`.

**Design source:** [`../specs/2026-06-04-build-plane-design.md`](../specs/2026-06-04-build-plane-design.md) · [`../../adr/0027-build-plane-local-make.md`](../../adr/0027-build-plane-local-make.md). The spec's §6/§7 are the authoritative handler/error contracts; each task references its slice.

---

## File structure

- **Modify** `src/kdive/db/locks.py:31-34` — add `LockScope.RUN = "run"` (reserved in the global order already).
- **Create** `src/kdive/profiles/build.py` — the `BuildProfile` schema + `parse()` (mirrors `profiles/provisioning.py`). DB-free.
- **Create** `src/kdive/providers/local_libvirt/build.py` — `BuildOutput`, the `Builder` Protocol, `LocalLibvirtBuild.{from_env,build}`, build-id extraction, deterministic artifact keys.
- **Modify** `src/kdive/mcp/tools/runs.py` — add `build_run` (the `runs.build` tool body), `build_handler`, `register_handlers`; register `runs.build` in `register`.
- **Modify** `src/kdive/mcp/app.py:36` — append `runs.register_handlers` to `_HANDLER_REGISTRARS`.
- **Create** `tests/profiles/test_build.py` — `BuildProfile.parse` unit tests.
- **Create** `tests/providers/local_libvirt/test_build.py` — builder unit tests (fake store / fake subprocess seam); `live_vm`-gated real-`make` test.
- **Modify** `tests/mcp/test_runs_tools.py` — add `runs.build` tool tests + `build_handler` tests (real Postgres, fake `Builder`/store).

> Each commit keeps all guardrails green: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`, `uv run python -m pytest -q`. Verify `git log -1 --oneline` after every commit (prek may roll back a `ruff format` rewrite).

---

## Task 1: Add the `LockScope.RUN` member

**Files:** Modify `src/kdive/db/locks.py:22-35`

- [ ] **Step 1 (test first):** In `tests/db/test_locks.py`, add a test asserting `LockScope.RUN` exists and `_lock_key(LockScope.RUN, uid)` differs from `_lock_key(LockScope.SYSTEM, uid)` for the same uid (the scope-prefix keeps disjoint keys). Run it; expect FAIL (`AttributeError: RUN`).
- [ ] **Step 2:** Add `RUN = "run"` to `LockScope` and drop the "no M0 tool needs a per-Run lock yet" caveat from the docstring (it is now used). Re-run; expect PASS.
- [ ] **Step 3:** Guardrails green; commit `feat(locks): add LockScope.RUN for the build plane (#18)`.

## Task 2: `BuildProfile` schema and parse boundary

**Files:** Create `src/kdive/profiles/build.py`, `tests/profiles/test_build.py` (spec §4)

- [ ] **Step 1 (tests first):** Write `tests/profiles/test_build.py` mirroring `tests/profiles/test_provisioning.py`:
  - a fully-valid profile parses (`schema_version:1`, `kernel_source_ref`, `config_ref`, optional `patch_ref`);
  - `patch_ref` omitted → `None`;
  - a missing required field → `CategorizedError(CONFIGURATION_ERROR)`;
  - an unknown field → `configuration_error` (`extra="forbid"`);
  - an empty/whitespace `kernel_source_ref`/`config_ref` → `configuration_error` (`NonEmptyStr`);
  - `schema_version` as `True` / `1.0` / `"1"` → `configuration_error` (the coercion trap);
  - the error details carry field locations but **not** the submitted values (redaction — assert a sentinel secret value is absent from `str(exc.details)`);
  - the model is `frozen` (assigning a field raises).
  Run; expect FAIL (module absent).
- [ ] **Step 2:** Implement `src/kdive/profiles/build.py` mirroring `provisioning.py`: `NonEmptyStr`, a `_ProfileBase` (`frozen`, `extra="forbid"`), `BuildProfile(schema_version: Literal[1]`, `kernel_source_ref`, `config_ref`, `patch_ref: NonEmptyStr | None = None)`, the `_reject_coerced_version` before-validator, and `parse()` mapping `ValidationError → CONFIGURATION_ERROR` with `include_input=False` (value scrub). Re-run; expect PASS.
- [ ] **Step 3:** Guardrails green; commit `feat(profiles): add BuildProfile schema + parse (#18)`.

## Task 3: The `Builder` port and `LocalLibvirtBuild`

**Files:** Create `src/kdive/providers/local_libvirt/build.py`, `tests/providers/local_libvirt/test_build.py` (spec §5)

- [ ] **Step 1 (tests first):** Write `tests/providers/local_libvirt/test_build.py`. The builder's slow seams (`make` subprocess, `.config` read, build-id read, object-store `put`) are injected so the unit tests need no toolchain:
  - `build()` returns `BuildOutput(kernel_ref, debuginfo_ref, build_id)` with the two refs equal to the deterministic keys `{tenant}/runs/{run_id}/{kernel,vmlinux}` and `build_id` the extracted value;
  - both artifacts are put as `Sensitivity.SENSITIVE`;
  - a config missing `CONFIG_CRASH_DUMP`/`crashkernel` → `CategorizedError(CONFIGURATION_ERROR)`, raised **before** `make` runs (assert the `make` seam was not invoked);
  - a config missing `CONFIG_DEBUG_INFO`/DWARF/BTF → `configuration_error`, before `make`;
  - a non-zero `make` exit → `CategorizedError(BUILD_FAILURE)`;
  - an object-store put failure → `CategorizedError(INFRASTRUCTURE_FAILURE)` (the store already raises this; assert it propagates);
  - build-id extraction parses a known GNU build-note byte sequence to the expected hex (table-test the parser against a crafted note);
  - `from_env` does not connect/spawn (lazy), mirroring `provisioning.from_env`.
  Run; expect FAIL (module absent).
- [ ] **Step 2:** Implement `src/kdive/providers/local_libvirt/build.py`:
  - `BuildOutput(NamedTuple)`, `Builder(Protocol)`;
  - `LocalLibvirtBuild(__init__(*, workspace_root, store, run_make=..., read_config=..., read_build_id=...))` — the slow ops are injected callables defaulting to the real `subprocess`/file/`readelf`-note implementations, so tests pass fakes;
  - `build(run_id, profile)`: warm-tree checkout (base ref + optional patch) and stage `.config`; **config preflight** (assert the required `CONFIG_*` options, raising `CONFIGURATION_ERROR` before `make`); run `make` (non-zero → `BUILD_FAILURE`); extract the build-id from the produced `vmlinux`; `put_artifact` the kernel image and `vmlinux` under the deterministic keys (sync store offloaded by the **handler**, not here — `build()` is sync and the handler wraps the whole call in `asyncio.to_thread`); return `BuildOutput`;
  - `from_env()` builds workspace/store lazily.
  Type the build-id parser carefully for `ty` (`bytes` slicing, `int.from_bytes`). Re-run unit tests; expect PASS.
- [ ] **Step 3 (`live_vm` real-make test):** Add a real-`make` test decorated **exactly** `@pytest.mark.live_vm` (the marker registered in `pyproject.toml [tool.pytest.ini_options].markers`; `--strict-markers` is **not** set, so a typo would silently no-op the gate — copy the marker name, do not retype). The test runs the real `make` against `$KDIVE_KERNEL_SRC` and asserts the extracted build-id equals `readelf -n vmlinux`'s. **All toolchain access (`make`, `readelf`, the kernel tree) must be inside the test body, never at module import or collection** — a stock runner has no toolchain and must still *collect* the file. Verify: (a) the default suite reports this test **`skipped`** (not `passed`/`errored`) on a runner without the env — run `uv run python -m pytest tests/providers/local_libvirt/test_build.py -rs -q` and confirm a skip line; (b) CI's main job runs `uv run python -m pytest -m "not live_vm" -q` (`.github/workflows/ci.yml`), and a separate manual-dispatch self-hosted-KVM job runs `-m live_vm`; the `@pytest.mark.live_vm` decorator is what routes this test to the gated job, so the marker name must match exactly.
- [ ] **Step 4:** Guardrails green; commit `feat(build): add Builder port + LocalLibvirtBuild (#18)`.

## Task 4: `runs.build` tool (synchronous admission)

**Files:** Modify `src/kdive/mcp/tools/runs.py`, `tests/mcp/test_runs_tools.py` (spec §6a)

- [ ] **Step 0 (helpers to lift — read these first so steps run in order):** The job-plane helpers `runs.build`/`build_handler` need live in `systems.py`, not `runs.py`. Lift (copy, not import — keep planes decoupled) into `runs.py`:
  - `_authorizing(ctx, project) -> dict` — `{"principal", "agent_session", "project"}` (the job's `authorizing` tuple);
  - `_ctx_from_job(job, project) -> RequestContext` — reconstructs attribution from `job.authorizing` (handler-side audit);
  - `_run_job_envelope(job, run_id) -> ToolResponse` — `ToolResponse.from_job(job)` with `run_id` merged into `data` (mirrors `_system_job_envelope`).
  `audit.record` (already imported in `runs.py`) is called as `await audit.record(conn, ctx, tool="runs.build", object_kind="runs", object_id=run_id, transition="created->running", args={"run_id": str(run_id)}, project=project)`. `queue.enqueue(conn, JobKind.BUILD, payload, authorizing, dedup_key)` returns the existing job on a dedup conflict (upsert-then-fetch). Confirm each signature against `systems.py`/`security/audit.py`/`jobs/queue.py` before writing Step 2.
- [ ] **Step 1 (tests first):** Add `runs.build` tool tests to `tests/mcp/test_runs_tools.py` (seed Runs via the existing `_seed_run` helper; the build job is not dispatched here — only admitted):
  - `created` Run → response is a queued **job** handle (status `queued`, `data.run_id` set), the Run is now `running`, and exactly one `build` job exists with `dedup_key = f"{run_id}:build"`;
  - two `runs.build` on the same `created→running` Run → **same** `object_id` (job id), still one job, Run still `running` (dedup, no second flip);
  - `runs.build` on a `succeeded` Run (seed `succeeded` + a pre-existing `build` job) → returns the same (succeeded) job, no transition;
  - a malformed `build_profile` (seed a Run with `build_profile = {"schema_version": 2}`) → synchronous `configuration_error`, **no job enqueued** (assert `count(jobs)=0`);
  - `failed` / `canceled` Run → `configuration_error` with `data.current_status`;
  - cross-project / missing Run → not-found `configuration_error`;
  - a `viewer` ctx → raises `AuthorizationError`;
  - concurrent `runs.build` (two `asyncio.gather`ed calls on one `created` Run) → both return the same job id, exactly one job row, Run `running` (the per-Run lock serializes the flip).
  Run; expect FAIL (`build_run`/`runs.build` absent).
- [ ] **Step 2:** Implement `build_run(pool, ctx, run_id)` in `runs.py` per spec §6a: uuid-parse guard; load Run; not-found/cross-project → `_config_error`; `require_role(OPERATOR)`; `BuildProfile.parse(run.build_profile)` → `configuration_error` on `CategorizedError`; then under `conn.transaction()` + `advisory_xact_lock(RUN, run_id)`: re-read Run state `FOR UPDATE`; for `created` drive `created → running` (via a fenced `UPDATE … WHERE state='created'`) + audit `created->running`; for `created`/`running`/`succeeded` call `queue.enqueue(JobKind.BUILD, {"run_id": str(run_id)}, _authorizing(...), f"{run_id}:build")` (the one uniform exit); for `failed`/`canceled` return `_config_error(data=current_status)`. Return a job-handle envelope carrying `run_id` in `data` (reuse a `_run_job_envelope` mirroring `_system_job_envelope`). Register `runs.build` in `register`. Re-run; expect PASS.
- [ ] **Step 3:** Guardrails green; commit `feat(runs): add runs.build admission tool (#18)`.

## Task 5: `build_handler` (the worker)

**Files:** Modify `src/kdive/mcp/tools/runs.py`, `tests/mcp/test_runs_tools.py`, `src/kdive/mcp/app.py` (spec §6b/§6c/§7)

- [ ] **Step 1 (tests first):** Add `build_handler` tests (call the handler directly with a fake `Builder` + fake store and a real claimed `Job`, mirroring the provision-handler tests):
  - happy path: handler runs, `builder.build` called once, Run → `succeeded` with `kernel_ref`/`debuginfo_ref` set to the builder's refs, a `(run_id,"build")` `run_steps` row exists, an audit `running->succeeded` row exists;
  - **replay/no-rebuild:** invoke the handler a **second** time for the same job after the first succeeded → `builder.build` is **not** called again (assert the fake's call-count stays 1), Run stays `succeeded`, refs unchanged;
  - **build failure:** the fake `Builder` raises `CategorizedError(BUILD_FAILURE)` → handler re-raises, Run → `failed` with `failure_category = build_failure`, **no** `run_steps` row recorded, and (driving through `Worker`/`queue.fail`) the job dead-letters with `build_failure` after `max_attempts`;
  - **config-preflight failure:** fake raises `CONFIGURATION_ERROR` → Run `failed` with `failure_category = configuration_error`;
  - **concurrent cancel:** drive the Run to `canceled` before finalize → handler tolerates `IllegalTransition`, leaves the Run `canceled`, does not crash;
  - **store-then-record window (observable end-to-end re-dispatch, not an internal skip):** dispatch the job with a fake `Builder` that stores its two artifacts and then **raises** (simulating a finalize crash after the puts) → assert no `run_steps` row, Run still `running`. Re-dispatch the **same** job with a fake `Builder` that succeeds → assert (a) the second build wrote the **same** deterministic keys (the fake records its put keys; both dispatches match `{tenant}/runs/{run_id}/{kernel,vmlinux}` — overwrite, no orphan), (b) the Run ends `succeeded` with both refs set, (c) exactly **one** `(run_id,"build")` ledger row. This tests the recovery outcome, not the handler's internal choreography.
  Run; expect FAIL (`build_handler` absent).
- [ ] **Step 2:** Implement `build_handler(conn, job, builder, store)` per spec §6b: resolve `run_id` from payload; read Run; `BuildProfile.parse`; `_ctx_from_job`; **ledger read** in a short txn (existing `(run_id,"build")` row → use its stored result, skip the build); else **connectionless** `result = await asyncio.to_thread(builder.build, run_id, profile)` (no `conn` use during `make`); on `CategorizedError` take the **failure path** (short txn + `RUN` lock: `running → failed` with the category as `failure_category`, audit, tolerate `IllegalTransition`, re-raise); on success the **record+finalize** short txn under the `RUN` lock: `INSERT … run_steps … ON CONFLICT DO NOTHING`, re-read Run `FOR UPDATE`, if `running` then `UPDATE runs SET kernel_ref, debuginfo_ref, state='succeeded' WHERE id=… AND state='running'` + audit `running->succeeded`; return `str(run_id)`. Add `register_handlers(registry, *, builder=None, store=None)` (lazy `LocalLibvirtBuild.from_env`) registering `JobKind.BUILD`. Re-run; expect PASS.
- [ ] **Step 3:** Append `runs.register_handlers` to `_HANDLER_REGISTRARS` in `app.py`. Add/extend the app wiring test (mirroring the provisioning handler-registration test) asserting `build_handler_registry().get(JobKind.BUILD)` is bound. Re-run; expect PASS.
- [ ] **Step 4:** Guardrails green; commit `feat(runs): add build handler + register on the worker seam (#18)`.

## Task 6: Full-suite + acceptance sweep

- [ ] **Step 1:** Run the whole suite `uv run python -m pytest -q` (Docker up) and `-m "not live_vm"` to confirm the gated test skips. Map each spec §9 acceptance row to a passing test; fill any gap.
- [ ] **Step 2:** `uv run ruff check`, `uv run ruff format --check`, `uv run ty check src` — zero warnings. (Pre-commit also `ty`-checks tests; let it run.)
- [ ] **Step 3:** If anything changed, commit; verify HEAD landed.

---

## Verification & rollback

- **Per-task verification:** each task ends with the four guardrails green and a HEAD check.
- **Acceptance proof:** the §9 table — both refs set + Run `succeeded` (handler test); dedup returns the same job + no rebuild (tool test + handler replay test); a failing build → Run `failed`/`build_failure` + dead-letter (handler test). The build-id-matches-**booted**-kernel half is `live_vm`-deferred (#19/#20), recorded in the spec.
- **Rollback:** every task is an isolated commit on `feat/build-plane-18`; revert a task's commit to undo it. No migrations, no destructive external ops — the only DB writes are `runs`/`run_steps`/`jobs`/`audit_log` rows guarded by existing transitions, so a revert needs no data cleanup. The object store is touched only on the `live_vm` path (skipped in CI).
- **Shared-file risk:** `runs.py` and `app.py` are touched by siblings; the additions are localized (new functions + one tuple append) and called out in NOTES.
