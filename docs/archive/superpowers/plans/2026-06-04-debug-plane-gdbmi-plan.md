# Debug plane: gdb-MI tier (M0) — implementation plan

Derived from [`../specs/2026-06-04-debug-plane-gdbmi-design.md`](../specs/2026-06-04-debug-plane-gdbmi-design.md)
and [ADR-0034](../../adr/0034-debug-plane-gdbmi-tier.md). Issue #21.

**Working directory:** `/home/dave/src/kdive-v2-worktrees/gdbmi-tier-21`, branch
`feat/gdbmi-tier-21`. Spec + ADR already committed.

**Guardrails after every step** (must be green before committing): run
`uv run ruff format` (the **rewriting** form) and stage the result *before* committing — the
prek hook also runs `ruff format`, and per the project memory it can silently roll back a
commit when it rewrites a file with unstaged changes; staging the formatted file first avoids
the stash-rollback trap. Then `uv run ruff check` · `uv run ty check src` ·
`uv run python -m pytest -q`. `ty` checks tests too (via pre-commit); SQL params typed
`LiteralString`. After each commit verify `git log -1` shows the commit landed (external
worktree → ty hook clean, no `--no-verify`).

TDD throughout: write the failing test first, then the implementation, per phase.

---

## Phase 0 — Dependency

1. Add `pygdbmi==0.11.0.0` to `[project].dependencies` in `pyproject.toml`.
2. `uv lock` then `uv sync` to materialize it into `uv.lock` + the venv.
3. Verify import: `uv run python -c "import pygdbmi.gdbmiparser, pygdbmi.constants"`.

**Commit:** `build: add pygdbmi==0.11.0.0 for the gdb-MI tier (#21)`.
**Verification:** lockfile updated, import succeeds, guardrails green (no source change yet).
**Rollback:** revert the commit; `uv sync`.

> Prerequisite: this lands before any code importing `pygdbmi`, so the engine module's
> imports resolve under `ty` and at test collection.

---

## Phase 1 — The trimmed gdb-MI engine (`debug_gdbmi.py`), no handlers yet

Port only the §3 in-scope surface into
`src/kdive/providers/local_libvirt/debug_gdbmi.py`. Adapt the v1 idioms to v2:
- `GdbMiError` → `CategorizedError` (kdive.domain.errors); no new exception type.
- `Model` (v1) → the v2 domain base (`pydantic.BaseModel` with `extra="forbid"`); mirror
  the existing v2 domain-model config. Records are local module models, not durable rows.
- `Redactor` → `kdive.security.redaction.Redactor`.
- Drop `Endpoint`/`TcpEndpoint` (v1 seam); the engine takes a decoded
  `(host, port)` / `TransportHandleData`. Loopback validation already lives in #20's
  `connect.py`; the engine re-validates host is a loopback IP literal (defence in depth,
  ported `_validate_rsp_host`).
- Constants ported: `MAX_MEMORY_READ_BYTES = 4096`, `MAX_INTERACTIVE_WAIT_SEC = 60`, the MI
  command timeout, the symbol/register/break-id/location regexes, terminal-stop reasons.

**Engine surface to port (and nothing else):**
- records: `MiRecord` (+ `from_raw`, `first_result`), `Frame`, `StopRecord`, `BreakpointRef`;
  `parse_mi_records`.
- seam: `MiController` Protocol, `PygdbmiController` (real, `# pragma: no cover - live_vm`),
  `_timeout_error`.
- `GdbMiAttachment` dataclass (controller, host, port, transcript_path, records).
- `_ExecutionControl` (wait_for_stop / interrupt / resume / `_redact_stop`).
- engine ops: `set_breakpoint` (`-break-insert -h`), `clear_breakpoint`, `list_breakpoints`,
  `read_registers`, `read_memory`, `continue_`, `interrupt`; their private helpers
  (`_run`, `_records_from`, `_breakpoint_ref*`, `_stop_record_from`, `_frame_from`,
  `_append_transcript`).
- `read_memory` change vs v1: return **bytes** (hex-decode + concat the `memory[].contents`
  segments, capped at 4096), not the v1 dict. The 4096 + address-range validation stays.
- `GdbMiSessionRegistry` (register / get / require / reap), keyed on `session_id` (str).
- engine `__init__` keeps the injectable `controller_factory`, `gdb_path_finder`, `redactor`,
  `sleep` seams so tests drive it with a fake controller.

**Tests first** (`tests/providers/local_libvirt/test_debug_gdbmi.py`), with a
`_FakeMiController` mapping command→canned pygdbmi dicts:
- `parse_mi_records` skips blanks + `(gdb)`; `MiRecord.from_raw` whitelists keys.
- `set_breakpoint` happy path → `BreakpointRef`; bad location → `CONFIGURATION_ERROR`.
- `clear_breakpoint` numeric-only; non-numeric → `CONFIGURATION_ERROR`.
- `list_breakpoints` parses the `BreakpointTable.body` rows.
- `read_registers` name→ordinal mapping; empty/bad name → `CONFIGURATION_ERROR`;
  result redacted.
- `read_memory` returns concatenated bytes; `byte_count==4096` ok; `==4097` →
  `CONFIGURATION_ERROR` with **no** MI command written; bytes returned **verbatim** (a
  secret-looking byte string is unchanged).
- `read_memory` transcript line is redacted even though the returned bytes are not.
- `continue_` happy stop; timeout→`-exec-interrupt` fallback marks `timed_out`; silent link →
  `INFRASTRUCTURE_FAILURE` `transport_stall`.
- `interrupt` returns the stop or None.
- `_run` maps an MI `^error` → `CategorizedError(DEBUG_ATTACH_FAILURE)`; MI write timeout →
  `INFRASTRUCTURE_FAILURE`.
- `_append_transcript` writes one redacted JSON line per command to the transcript path.
- `GdbMiSessionRegistry` register/get/require(miss→`no_live_session`)/reap.

**Commit:** `feat(debug): port trimmed gdb-MI engine + records + registry (#21)`.
**Verification:** new test file green; `ty check src` clean (no `pygdbmi` stub gap — it ships
types or is suppressed at the one import site with a justified ignore if needed); full suite
green.
**Rollback:** revert; the module is new and unreferenced, so nothing else breaks.

> Prerequisite: Phase 0 (pygdbmi import). No handler/registry wiring yet — the engine is
> exercised directly, so this phase is independent of `debug.py`.

---

## Phase 2 — The attach seam + debuginfo resolver (`live_vm`-gated)

In `debug_gdbmi.py`, add the attach seam used lazily by the handlers:
- `attach(rsp_endpoint: TransportHandleData, debuginfo_ref: str) -> GdbMiAttachment` — the
  real path spawns `gdb --interpreter=mi3`, materializes the debuginfo object to a temp file,
  `-file-exec-and-symbols`, sets `remotetimeout`/mi-async, `-target-select remote host:port`.
  `# pragma: no cover - live_vm`.
- `_resolve_debuginfo_ref(run_id) -> str` default raises `MISSING_DEPENDENCY`
  (`# pragma: no cover - live_vm`), mirroring `connect.py`'s `_real_resolve_endpoint`.
- An `AttachSeam` Protocol (or a `Callable`) so handlers inject a fake in tests; the real
  default is `from_env()`-style, wired in `register`.

**Where the `MISSING_DEPENDENCY → DEBUG_ATTACH_FAILURE` re-tag lives (not inside the gated
body):** the handler's lazy-attach catch maps a `MISSING_DEPENDENCY` `CategorizedError` raised
by the seam onto `DEBUG_ATTACH_FAILURE` (same shape #20's `_mapped` uses for the connect
path). The real `attach()` body is `live_vm`-pragma'd, but the *mapping* is in the handler, so
Phase 3 unit-tests it with a fake seam that **raises** `MISSING_DEPENDENCY` — covered off the
gate. Phase 2 only asserts the resolver default raises; the mapping is asserted in Phase 3.

**Tests first** (extend `test_debug_gdbmi.py`):
- the default resolver raises `MISSING_DEPENDENCY` (asserts the gate default; the real attach
  body is `live_vm`-pragma'd, not unit-tested).

**Commit:** `feat(debug): add live_vm-gated gdb-MI attach seam + debuginfo resolver (#21)`.
**Verification:** resolver-default test green; `ty` clean; suite green.
**Rollback:** revert; seam is unreferenced until Phase 3.

> Prerequisite: Phase 1 (the engine + `GdbMiAttachment` the seam returns).

---

## Phase 3 — The seven handlers + lazy attach + per-session lock + reap wiring

Extend `src/kdive/mcp/tools/debug.py` (do **not** create a new module):
1. A process-scoped holder built in `register`: `GdbMiSessionRegistry`, a
   `dict[str, asyncio.Lock]` lock table guarded by a `threading.Lock` for get-or-create, the
   injected `AttachSeam`, and the `pool`.
2. A shared `_with_live_engine(session_id, ctx, op)` helper: UUID-parse → load row →
   project/role gate → `state == live` gate (each failure carries its §5a `data["code"]`) →
   take the per-session `asyncio.Lock` → registry get-or-`attach` (counting once) → dispatch
   `await asyncio.to_thread(op, attachment)` → return.
3. Seven `async def` handlers calling `_with_live_engine` with the right engine op + input
   validation (bad inputs → `CONFIGURATION_ERROR` + `data["code"]`, **before** the engine op),
   building the §5/§6 envelopes (incl. `read_memory` → `data["memory_hex"/"byte_count"/
   "address"]`, bytes **not** redacted).
4. `@app.tool(name="debug.<op>")` registrations for all seven, added to the existing
   `register`.
5. Extend the existing `end_session`: under the per-session lock, `reap(session_id)` +
   best-effort `controller.exit()` + drop the lock-table entry, then the existing detach.

**Tests first** (extend `tests/mcp/test_debug_tools.py`), handler-level with injected pool +
fake `AttachSeam` returning a `GdbMiAttachment` over a `_FakeMiController`:
- happy path per tool: status + `suggested_next_actions` + (read_memory) verbatim `memory_hex`.
- 4096 boundary at the handler: `4096` ok, `4097` → `CONFIGURATION_ERROR`
  (`data["code"]="bad_read_range"`), no MI command, no attach.
- transcript/record redaction in the envelope; memory bytes unredacted.
- input-validation rejections each carry the right `data["code"]`.
- state/authz: bad UUID (`bad_session_id`), missing/cross-project (`unknown_session`),
  non-operator (`AUTHORIZATION_DENIED`), non-`live` (`not_live`, `data["current_status"]`).
- `no_live_session`: a fake seam whose engine is "gone" → `CONFIGURATION_ERROR`
  (`no_live_session`).
- attach-once: two ops on one session launched via `asyncio.gather` exercise the
  per-session lock; a counting fake seam is invoked exactly once and the second op reuses the
  registered attachment (the lock serializes them, so the second sees it registered).
- `MISSING_DEPENDENCY` resolver default surfaces as `DEBUG_ATTACH_FAILURE`.
- `end_session` reap: after an op registers a fake engine, `end_session` calls the fake's
  `exit()` and drops the registry entry; a later op on the now-`detached` session is rejected
  at the state gate; `end_session` on a never-debugged session still succeeds.

**Commit:** `feat(debug): add gdb-MI debug.* tools + lazy attach + engine reap (#21)`.
**Verification:** new handler tests green; the existing #20 start/end tests still green
(end_session reap is additive — a session with no engine reaps to a no-op); `ty check src` +
pre-commit `ty` (tests) clean; full suite green.
**Rollback:** revert; Phases 1–2 modules become unreferenced again but still compile.

> Prerequisite: Phases 1 (engine) + 2 (attach seam). This is the only phase that touches the
> shared `debug.py`; it adds to #20's `register`, so `app.py` is untouched.

---

## Phase 4 — App-registration assertion (shared file, minimal edit)

Add to `tests/mcp/test_app.py` an assertion that the seven `debug.*` tool names register
(alongside #20's two). Keep the edit additive and minimal — #22 also edits this file.

**Commit:** `test(app): assert debug.* gdb-MI tools register (#21)`.
**Verification:** `test_app.py` green; full suite green.
**Rollback:** revert.

> Prerequisite: Phase 3 (the tools exist to be asserted). If #22 already added rows here,
> rebase/merge and keep both additive.

---

## Phase 5 — Guardrail sweep + adversarial-review loop (spec step 6)

1. Full guardrail pass: `uv run ruff check` · `uv run ruff format --check` ·
   `uv run ty check src` · `uv run python -m pytest -q`. Zero warnings.
2. Run `/challenge main..HEAD`; address every defensible finding (one logical change per
   commit), re-running guardrails each time; repeat until `approve` or 5 iterations.

> No new code expected here beyond review fixes; this phase is the diff-level review gate.

---

## Phase 6 — Ship

1. `git push -u origin HEAD`.
2. `gh pr create` against `main`; body = plain factual description of the diff, ending
   `Closes #21`.
3. `gh pr checks <PR> --watch` until green (the `live_vm` jobs skip as expected).
4. Poll `gh pr view <PR> --json mergeable,mergeStateStatus` until CLEAN/MERGEABLE. If
   DIRTY/BEHIND/CONFLICTING: merge `origin/main`, resolve (most likely the shared
   `docs/adr/README.md` / `tests/mcp/test_app.py` with #22), re-run guardrails, re-push.
5. Stop at CI-green + MERGEABLE. **Do not self-merge.**

---

## Cross-cutting risks & mitigations

- **`ty` + `pygdbmi` (no stubs):** if `pygdbmi` ships no type info, the one import site gets a
  scoped `# ty: ignore[unresolved-import]`-style suppression with a justification, never a
  project-wide rule relaxation (the pyproject comment already mandates this).
- **`asyncio.Lock` held across `to_thread`:** correct — `async with lock:` holds the lock
  across the inner `await asyncio.to_thread(...)`; the lock is released only at the
  `async with` exit, so a second op on the same session waits. Verified by the attach-once
  test.
- **Shared-file contention with #22:** `docs/adr/README.md` and `tests/mcp/test_app.py` are
  touched by both. Edits are additive; a merge conflict is resolved by keeping both rows /
  both assertions. Listed in PR NOTES.
- **Engine leak on crash-path detach (acknowledged, M0):** a `force_crash` detach in the
  worker process cannot reap the MCP-server-process engine; the next op is safely rejected at
  the state gate (row is `detached`). No incorrect behavior; the subprocess is reclaimed on
  server restart. Out of #21 scope to fix (would need a cross-process reaper).
