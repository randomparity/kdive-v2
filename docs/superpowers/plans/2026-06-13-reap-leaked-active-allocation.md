# Reap leaked active allocations — Implementation Plan (#371)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:test-driven-development per task. Steps use `- [ ]`.

**Goal:** The reconciler auto-reclaims an `active` allocation whose System is terminal/absent, freeing the host-cap slot without `force_release`.

**Architecture:** New repair `reap_orphaned_active_allocations` in `reconciler/allocations.py`, mirroring `sweep_expired_allocations`: candidate read (no lock) → per-candidate PROJECT→ALLOCATION lock → re-check predicate → `release_with_backstops` with a system audit writer. Wired into `reconcile_once` after the expiry sweep, before promotion. New `ReconcileReport.reaped_active_allocations` field + `ops.reconcile_now` data key.

**Tech Stack:** Python 3.13, psycopg async, pytest + testcontainers (db-tier, Docker-gated).

---

### Task 1: The reap repair + predicate

**Files:**
- Modify: `src/kdive/reconciler/allocations.py`
- Test: `tests/reconciler/test_orphaned_active_sweep.py` (create)

- [ ] **Step 1: Write failing tests** covering: leaked-active+torn_down → reclaimed (released, one `reconciled`, `active_ended_at` set); failed-System → reclaimed; no-System → reclaimed; ready-System → preserved; provisioning → preserved; crashed → preserved; within-grace → preserved then aged → reclaimed; idempotent re-run; one-bad-does-not-starve-good. Use `tests/reconciler/conftest.py` seeders; age `updated_at` via SQL (`UPDATE allocations SET updated_at = now() - interval ...`). Seed budget+reserve as `test_expiry_sweep.py` does so `reconcile` writes a credit.
- [ ] **Step 2: Run, expect fail** (`reap_orphaned_active_allocations` undefined).
- [ ] **Step 3: Implement** `reap_orphaned_active_allocations(conn) -> int` + `_reclaim_one` + `DEFAULT_ORPHANED_ACTIVE_GRACE = timedelta(minutes=2)` + `_TERMINAL_SYSTEM_STATE_VALUES = (torn_down, failed)` (the complement of admission's `_NON_TERMINAL_SYSTEM`). Candidate SQL: `state='active' AND updated_at < now() - grace AND NOT EXISTS (live system)`. Per candidate, under PROJECT→ALLOCATION lock: re-read; skip if not `active`; re-check no-live-system; call `release_with_backstops(pool=...)` — **but** the repair runs on one `conn`, while `release_with_backstops` takes a `pool`. Resolve by extracting the locked release body (`_release_locked`) — call `release.reclaim_active_under_lock(conn, audit_writer, alloc_id, project=...)` (new thin helper in release.py exposing the already-locked path) OR inline the transition using the same primitives. Use the connection-based path (no nested pool). System audit writer = `audit.record_system(conn, principal="system:reconciler", event=...)`. Per-candidate try/except (BLE001) like `sweep_expired_allocations`.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: lint+type; commit.**

### Task 2: Connection-based reclaim helper in release.py

**Files:**
- Modify: `src/kdive/services/allocation/release.py`

- [ ] Expose the existing `_release_locked` logic for a caller that already holds a connection and wants the system-writer path, without re-acquiring a pool. Add `async def reclaim_under_lock(conn, audit_writer, uid, *, project) -> ReleaseOutcome` that takes the PROJECT→ALLOCATION locks and runs the release transition (refactor `_release_locked` to be the shared body; `release_with_backstops` keeps its pool wrapper + exception mapping). The reaper catches `IllegalTransition`/`CategorizedError` itself (per-candidate isolation) and counts only a `released` outcome.
- [ ] Tests: existing release tests must still pass; add a direct test of `reclaim_under_lock` (active→released, one credit). lint+type; commit.

### Task 3: Wire into reconcile_once + report field

**Files:**
- Modify: `src/kdive/reconciler/loop.py`
- Test: `tests/reconciler/test_loop.py`, `tests/reconciler/test_orphaned_active_sweep.py`

- [ ] Add `reaped_active_allocations: int = 0` to `ReconcileReport`; add the `_reap_orphaned_active_allocations` alias + `__all__`; add `_RepairSpec("reaped_active_allocations", _reap_orphaned_active_allocations)` in `_repair_plan` **after** `expired_allocations`, **before** `promoted_allocations`; thread it through the `ReconcileReport(...)` construction in `reconcile_once`.
- [ ] Test: `reconcile_once` reports the counter and promotes into the freed slot in one pass (seed leaked-active on a cap=1 resource + a `requested` queued alloc on the same resource → `reaped_active_allocations==1`, `promoted_allocations==1`). Verify the mixed-pass equality test still passes (new field defaults to 0). lint+type; commit.

### Task 4: ops.reconcile_now data key

**Files:**
- Modify: `src/kdive/mcp/tools/ops/reconcile.py:_reconcile_response`
- Test: `tests/mcp/ops/test_reconcile_now.py`

- [ ] Add `"reaped_active_allocations": str(report.reaped_active_allocations)` to the `data` map.
- [ ] Test asserts the response `data` carries `reaped_active_allocations`. Run `just docs-check` (tool reference unchanged — only `data` payload changed, not the tool signature/docstring). lint+type+test; commit.

### Task 5: Full gate

- [ ] `just lint && just type && just test` (db tests run if Docker present). `just ci`. Fix all warnings. Commit any fixups.
