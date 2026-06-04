# Debug plane (offline drgn introspection from vmcore) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run drgn over a captured vmcore on the host (offline — no live guest, no SSH), loading the Run's `debuginfo_ref` (`vmlinux`) for symbols/types, run three fixed helpers (tasks, modules, sysinfo), and return a redacted, size-bounded report. The real drgn path is `live_vm`-gated; the orchestration, provenance, dispatch, and redaction are unit-tested against a fake drgn program.

**Architecture:** A seam-injected `VmcoreIntrospector` provider (`providers/local_libvirt/introspect_drgn.py`) mirrors `LocalLibvirtRetrieve`'s `CrashPostmortem`: `fetch_object`/`read_vmcore_build_id`/`open_program`/`run_helper` are `live_vm`-gated seams; the orchestration (provenance comparison, temp staging, helper dispatch, byte-cap, redaction) is host-free and unit-tested with fakes. `introspect.from_vmcore(run_id)` is a synchronous, ungated read resolving the Run's `debuginfo_ref` + recorded `build_id` + the System's raw `vmcore` key (replicating the minimal resolution locally — **not** importing `vmcore.py`'s private postmortem helpers, see Task 5), calling the port, and JSON-serializing the already-redacted report into `data["report"]`. The port is the single redaction boundary. No schema migration; no job kind.

**Tech Stack:** Python 3.13 · `psycopg` 3 (async) · Pydantic v2 · FastMCP 3.x · `pytest` (testcontainers Postgres) · `ruff`/`ty`. drgn is `live_vm`-gated and never imported in CI.

**Design source:** [`../specs/2026-06-04-drgn-introspection-from-vmcore-design.md`](../specs/2026-06-04-drgn-introspection-from-vmcore-design.md) · [`../../adr/0033-drgn-introspection-from-vmcore.md`](../../adr/0033-drgn-introspection-from-vmcore.md). The spec's Components / Output bounds / Error contract / Redaction sections are the authoritative contracts; each task references its slice.

---

## File structure

- **Create** `src/kdive/providers/local_libvirt/introspect_drgn.py` — `IntrospectOutput`/`VmcoreIntrospector`, the `_Program` typed `Protocol`, the three helper functions (`_helper_tasks`/`_helper_modules`/`_helper_sysinfo`), the byte-cap + redaction assembly, `LocalLibvirtVmcoreIntrospect.{from_env,from_vmcore}`, the `live_vm` real seams (with the single `# ty: ignore[unresolved-import]` `import drgn` line). DB-free.
- **Create** `src/kdive/mcp/tools/introspect.py` — `introspect_from_vmcore` (the tool body + Run/System/build-id resolution), `register`. No `register_handlers` (synchronous, no job kind).
- **Modify** `src/kdive/mcp/app.py` — import `introspect`; append `introspect.register` to `_PLANE_REGISTRARS`. No `_HANDLER_REGISTRARS` change.
- **Create** `tests/providers/local_libvirt/test_introspect_drgn.py` — provider unit tests (fake `_Program` + fake seams): provenance, open failure, per-helper degrade, modules all-failed, byte-cap/`truncated`, redaction at the port boundary; a `@pytest.mark.n` real-drgn placeholder (deselected in CI, Task 4).
- **Create** `tests/mcp/test_introspect_tools.py` — `introspect.from_vmcore` handler tests (real Postgres, fake `VmcoreIntrospector`): resolution failures, provenance, typed-failure mapping, redaction in `data["report"]`, `truncated` surfaced, `register` adds the tool.
- **Modify** `tests/mcp/test_app.py` — only if it asserts the registered tool set; add `introspect.from_vmcore` if so.

> Each commit keeps all guardrails green: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`, `uv run python -m pytest -q`. Verify `git log -1 --oneline` after every commit (prek may roll back a `ruff format` rewrite). Type any SQL helper text passed to `cur.execute` as `LiteralString` where ty flags it.

> **Shared-file edits to call out in NOTES:** `src/kdive/mcp/app.py` (one `_PLANE_REGISTRARS` append + one import). `docs/adr/README.md` (one ADR-0033 row — already added). `tests/mcp/test_app.py` (one tool-name assertion, only if that file enumerates tools). No edits to `errors.py` (`ErrorCategory.DEBUG_ATTACH_FAILURE`/`CONFIGURATION_ERROR`/`MISSING_DEPENDENCY` already exist), `db/schema/*.sql`, or `domain/models.py`.

> **Concurrency with sibling #20:** #20 creates `mcp/tools/debug.py` and also edits `app.py`/`test_app.py`/`docs/adr/README.md`. Keep edits to those shared files to the single lines above so a merge is trivial. Do NOT touch `debug.py` (it does not exist on this base).

---

## Task 1: The typed `_Program` protocol + `IntrospectOutput`/`VmcoreIntrospector` shapes (DB-free, no drgn)

**Files:** Create `src/kdive/providers/local_libvirt/introspect_drgn.py` (first slice), `tests/providers/local_libvirt/test_introspect_drgn.py` (first slice)

Define the narrow `_Program` `Protocol` (the subset the helpers call: `for_each_task`-equivalent iteration, `stack_trace`, module iteration, symbol/global lookup `prog["name"]`), `IntrospectOutput` NamedTuple (`tasks`/`modules`/`sysinfo`/`truncated`), and the `VmcoreIntrospector` `Protocol`. No drgn import in this slice — the `_Program` protocol is what the helpers type against.

- [ ] **Step 1 (test first):** Add a test that imports `IntrospectOutput`, `VmcoreIntrospector`, and constructs an `IntrospectOutput` with the four fields, asserting field access. Define a module-level `_FakeProgram` (a hand-rolled object exposing the `_Program` protocol surface with canned tasks/modules/uts) that later tasks reuse.
- [ ] **Step 2:** Implement the `_Program` `Protocol`, `IntrospectOutput`, `VmcoreIntrospector` `Protocol`. `from __future__ import annotations`; absolute imports; Google-style docstrings.
- [ ] **Step 3 (guardrails):** `ruff check` · `ruff format` · `ty check src` · `pytest -q tests/providers/local_libvirt/test_introspect_drgn.py`. Commit: `feat(debug): introspect port protocols + IntrospectOutput (#22)`. Verify HEAD.

## Task 2: The three helper functions over `_Program` (DB-free, fake program)

**Files:** `src/kdive/providers/local_libvirt/introspect_drgn.py`, `tests/providers/local_libvirt/test_introspect_drgn.py`

Port the M0 subset of v1 `introspect/helpers/{tasks,modules,sysinfo}.py` as typed functions `_helper_tasks(prog) -> dict`, `_helper_modules(prog) -> dict`, `_helper_sysinfo(prog) -> dict`, operating on the `_Program` protocol. Spec §"The three helpers" + §"Output bounds" are the contract.

- [ ] **Step 1 (test first):** Tests over `_FakeProgram`: `tasks` returns blocked-only rows (state filter `{"D"}`), respects `limit=200` (a fake with >200 tasks sets the helper `truncated`), includes `kernel_stack`; `modules` returns name/size/refcount/used_by/state and a `decode_errors` count; a fake whose every module raises yields `{"modules": [], "decode_errors": N, "all_failed": true}` (per-helper degrade, NOT a raise); `sysinfo` returns the uts/cmdline/counter fields; a helper whose decode raises mid-row degrades to `{"error": "<type>"}` for that helper without failing the others.
- [ ] **Step 2:** Implement the three helpers + a `_HELPERS: dict[str, Callable[[_Program], dict]]` dispatch map. Each helper catches per-row decode exceptions and continues; `modules` all-failed sets `all_failed` rather than raising. No `# noqa: BLE001` without a justification comment (these are the offline decode boundary — justify).
- [ ] **Step 3 (guardrails + commit):** `feat(debug): port tasks/modules/sysinfo helpers over typed program (#22)`. Verify HEAD.

## Task 3: `LocalLibvirtVmcoreIntrospect` orchestration — provenance, staging, byte-cap, redaction (DB-free)

**Files:** `src/kdive/providers/local_libvirt/introspect_drgn.py`, `tests/providers/local_libvirt/test_introspect_drgn.py`

The `from_vmcore()` contract (spec §Components steps 1–5): fetch core, **provenance** (build-id equality → `CONFIGURATION_ERROR` on mismatch), stage to temp files, `open_program` (failure → `DEBUG_ATTACH_FAILURE`), run the three helpers, **byte-cap** the assembled report (trim `tasks` first, set `truncated`), **redact** via `Redactor.redact_value`, return `IntrospectOutput`. Seams injected; real seams `# pragma: no cover - live_vm`; unconfigured seams → `MISSING_DEPENDENCY`.

- [ ] **Step 1 (test first):** Tests with all seams injected as fakes (`fetch_object` echoing bytes, `read_vmcore_build_id` returning a planted id, `open_program` returning `_FakeProgram`, `run_helper` delegating to the real `_HELPERS`): happy path returns a populated `IntrospectOutput`; build-id mismatch → `CategorizedError(CONFIGURATION_ERROR)`; `open_program` raising → `CategorizedError(DEBUG_ATTACH_FAILURE)`; a `_FakeProgram` whose `tasks` `comm` is `token=hunter2` yields `[REDACTED]` in the returned `IntrospectOutput` (port-boundary redaction, real `Redactor`); a fake yielding a huge `tasks` list sets `truncated=True` and trims; `from_env()` real port (no seams overridden) raises `MISSING_DEPENDENCY` from `from_vmcore` (no drgn import).
- [ ] **Step 2:** Implement `LocalLibvirtVmcoreIntrospect.__init__`/`from_env`/`from_vmcore`, the `_assemble_report` (byte-cap + `truncated`), the `_redact` step, and the real seams (`_real_open_program` with the single `# ty: ignore[unresolved-import]` `import drgn` and `# pragma: no cover - live_vm`; `_real_run_helper`; `_real_read_vmcore_build_id`). **Define a local `_real_read_vmcore_build_id`** (`# pragma: no cover - live_vm`) that raises `CategorizedError(MISSING_DEPENDENCY)` — the same shape `retrieve._real_read_vmcore_build_id` uses; do **not** import the `_`-private from `retrieve.py` (two trivial gated stubs that both raise are not worth a cross-module private import). Keep `from_vmcore` ≤100 lines / complexity ≤8 (extract `_provenance`, `_assemble_report`).
- [ ] **Step 3 (guardrails + commit):** `feat(debug): LocalLibvirtVmcoreIntrospect with provenance, byte-cap, redaction (#22)`. Verify HEAD.

## Task 4: gated real-seam smoke test (repo `@pytest.mark.n` idiom)

**Files:** `tests/providers/local_libvirt/test_introspect_drgn.py`

The repo's gated-integration-test idiom is **not** a `live_vm`/`skipif` marker (`test_retrieve.py` has no gated test — its real path is covered by `# pragma: no cover - live_vm` on the seams). It is `@pytest.mark.n` on a `def ...():  # pragma: no cover - n` body that `raise NotImplementedError(...)`, as in `tests/providers/local_libvirt/test_build.py` / `test_install.py` (the `n` marker is already registered, so no unknown-marker warning fires). Mirror that exactly.

- [ ] **Step 1:** Add a `@pytest.mark.n`-marked test `test_n_real_drgn_from_vmcore` with a `# pragma: no cover - n` body that `raise NotImplementedError("n real drgn introspection harness wired by the n runner")`, copying the shape from `test_build.py::test_n_real_make_build_id_matches_readelf`. Confirm `pytest -q` deselects it (the `n` marker is filtered in the default run) with no unknown-marker warning.
- [ ] **Step 2 (guardrails + commit):** `test(debug): mark.n real drgn smoke placeholder (#22)`. Verify HEAD.

## Task 5: `introspect.from_vmcore` tool handler + Run/System/build-id resolution (real Postgres)

**Files:** Create `src/kdive/mcp/tools/introspect.py`, `tests/mcp/test_introspect_tools.py`

`introspect_from_vmcore(pool, ctx, run_id, introspector)` (spec §Components): resolve Run + `debuginfo_ref` + System + recorded `build_id`, load the System's raw `vmcore` key; call the port; map `CategorizedError` → typed `ToolResponse.failure` (never a 500); JSON-serialize the already-redacted report into `data["report"]`, and set `data["truncated"] = str(output.truncated).lower()` (`"true"`/`"false"` — `ToolResponse.data` is `dict[str, str]`, so the bool must be encoded as a string, not passed as a bool); `suggested_next_actions=["introspect.from_vmcore", "artifacts.list"]`. Resolution failures (null `debuginfo_ref`, no `build` step, no captured core, bad uuid) → `configuration_error`.

> **Resolution is replicated, not imported.** `vmcore.py`'s resolution helpers (`_resolve_postmortem`, `_RAW_KEY_SQL`, `_BUILD_STEP_SQL`, `_build_id_for_run`, `_as_uuid`) are `_`-private and `_resolve_postmortem` is bound to the postmortem contract (it takes `commands` and runs the crash-command allowlist). Do **not** import those privates across modules. Replicate the minimal introspection resolution in `introspect.py` with its own `LiteralString` SQL: uuid parse → `RUNS.get` + project scope + non-null `debuginfo_ref` → `build` step `result["build_id"]` → raw `vmcore` object key (`object_key LIKE '%/vmcore'`, `owner_kind='systems'`, `owner_id=run.system_id`). This keeps #22 off `vmcore.py` (which #20 may also touch) and avoids a cross-module private import.

- [ ] **Step 1 (test first):** Tests over real Postgres + `tests/mcp/_seed.py` (`seed_crashed_system`/`seed_run_on_system`) with a `_FakeIntrospector` (canned `IntrospectOutput`, or planted `CategorizedError`): happy path returns `status != error` with `data["report"]` JSON-parseable and `data["truncated"] == "false"` (and `== "true"` when the port reports a truncated report); a `_FakeIntrospector` returning a `comm` of `token=hunter2` yields `[REDACTED]` in `data["report"]` (defense-in-depth even though the port already redacts); null `debuginfo_ref` → `configuration_error`; no `build` step result → `configuration_error`; System with no captured core → `configuration_error`; malformed `run_id` → `configuration_error`; a planted `CategorizedError(DEBUG_ATTACH_FAILURE)` → `error` with that category; cross-project `run_id` → `configuration_error` (not-found-shaped).
- [ ] **Step 2:** Implement `introspect_from_vmcore` + `register`. Keep the handler ≤100 lines; extract a local `_resolve` helper (the resolution replicated per the note above, not importing `_resolve_postmortem`). Build the introspector lazily in `register` (no drgn import at registration).
- [ ] **Step 3 (guardrails + commit):** `feat(debug): introspect.from_vmcore tool + resolution (#22)`. Verify HEAD.

## Task 6: Register the plane + app-wiring test (shared files — minimal)

**Files:** Modify `src/kdive/mcp/app.py`, `src/kdive/mcp/tools/introspect.py` (register already done in Task 5), `tests/mcp/test_app.py`

- [ ] **Step 1 (test first):** In `tests/mcp/test_app.py`, if it enumerates registered tool names, add `introspect.from_vmcore`; assert `build_app` registers it (via `await app.list_tools()` per the FastMCP auth-claims memo). If `test_app.py` does not enumerate tools, add a focused test in `test_introspect_tools.py` that `introspect.register` adds the tool to a bare `FastMCP`.
- [ ] **Step 2:** Append `introspect` to the `app.py` import block and `introspect.register` to `_PLANE_REGISTRARS` (single-line append, last entry, to minimize the merge surface with #20).
- [ ] **Step 3 (guardrails + commit):** `feat(mcp): register introspect.from_vmcore plane (#22)`. Verify HEAD.

## Task 7: Full-suite guardrails + adversarial-review loop

**Files:** all of the above

- [ ] **Step 1:** Full `uv run ruff check` · `uv run ruff format --check` · `uv run ty check src` · `uv run python -m pytest -q` (whole suite, not just new files) — confirm zero warnings and that the `@pytest.mark.n` real-drgn test is **deselected** (filtered out of the default run, never collected/errored), no unknown-marker warning.
- [ ] **Step 2:** Run the enforced `/challenge main..HEAD` loop (spec §6 of work-issue); address every defensible finding via `superpowers:receiving-code-review`; commit after each pass until `approve` or 5 iterations.
- [ ] **Step 3:** Push `git push -u origin HEAD`; open the PR (`Closes #22`); watch `gh pr checks --watch` AND poll `gh pr view --json mergeable,mergeStateStatus` until green AND CLEAN/MERGEABLE.

---

## Verification matrix (per the spec's Testing section)

| Scenario | Where | Expected |
|----------|-------|----------|
| happy path: tasks/modules/sysinfo populated | port + tool | `IntrospectOutput`/`data["report"]` populated, `status != error` |
| build-id provenance mismatch | port (fake reader) + tool | `configuration_error` |
| drgn open/load failure | port (fake `open_program` raises) + tool | `debug_attach_failure` |
| `modules` all-failed decode | port (fake program) | call succeeds, `all_failed: true` (NOT `debug_attach_failure`) |
| single helper raises mid-decode | port | `{"error": "<type>"}` for that helper, others ok |
| report exceeds `_REPORT_BYTE_CAP` | port | `truncated: true`, `tasks` trimmed |
| planted secret-shaped guest string | port + tool | `[REDACTED]` in `IntrospectOutput` and `data["report"]` |
| null `debuginfo_ref` / no build step / no core / bad uuid | tool | `configuration_error` |
| cross-project `run_id` | tool | `configuration_error` (not-found-shaped) |
| `register` adds the tool | tool/app | `introspect.from_vmcore` in `list_tools()` |
| real drgn path | `@pytest.mark.n` test | deselected in CI (not collected) |

## Rollback / cleanup

Every task is its own commit on the feature branch; a bad task is reverted with `git revert <sha>` (no migration, no external state — the plane writes no rows and moves no lifecycle, so there is nothing to undo beyond the code). No object-store or DB cleanup is needed because `introspect.from_vmcore` is a pure read. If `/challenge` surfaces a design flaw, the spec/ADR are amended first (defects are cheapest there), then the code.
