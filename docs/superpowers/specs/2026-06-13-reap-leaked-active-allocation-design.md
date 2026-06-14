# Reap leaked `active` allocations — design (#371)

- **Date:** 2026-06-13
- **Issue:** [#371](https://github.com/randomparity/kdive/issues/371)
- **ADR:** [ADR-0105](../../adr/0105-reap-leaked-active-allocation.md)

## Problem

A failed or interrupted lifecycle run can leave an allocation in the `active` state whose
owning System has reached a terminal state (`torn_down` / `failed`) — the System teardown
job (`jobs/handlers/systems.py:teardown_handler`) drives the System to `torn_down` but never
releases the allocation. Because `active` is in admission's `OCCUPYING` set
(`services/allocation/admission.py`), the leaked allocation permanently holds its host-cap
slot. On a remote host with `cap=1` this blocks **all** future allocations on that host.

`ops.reconcile_now` / the periodic reconciler do not collect it: the `→expired` sweep only
reclaims allocations whose **lease window has elapsed** (`lease_expiry < now()`). A leaked
`active` allocation may still be inside its lease, so it reports `expired_allocations:0` and
the slot stays wedged. The only current recovery is the break-glass `ops.force_release`
(`platform_admin`), observed used 3× in one session (MCP coverage campaign, F4).

## The symmetry with the existing orphaned-System repair

The reconciler already has the **inverse** repair, `repair_orphaned_systems`
(`reconciler/systems.py`): a System whose allocation is **terminal** but the System itself is
non-terminal → enqueue a teardown. This new repair is the missing other half: an allocation
that is **non-terminal (`active`)** but whose System is **terminal or absent** → release the
allocation.

## The data model that makes the predicate safe

- There is exactly **one System per Allocation** (`domain/models.py` System docstring; the
  admission read is `... FROM systems WHERE allocation_id = %s ORDER BY created_at, id LIMIT 1`).
  A System and its allocation's `→active` stamp are written in the **same transaction**
  (`services/systems/admission.py:_insert_system_and_activate`), so an `active` allocation
  always has a committed System row.
- A System never moves out of a terminal state. Reprovision is `ready → reprovisioning →
  ready`; it never creates a second System on the allocation. So once the single System is
  `torn_down` / `failed`, no live lifecycle can be running on that allocation.

## Predicate: a genuinely orphaned `active` allocation

Reclaim allocation `a` iff **all** hold:

1. `a.state = 'active'`. We target only `active`. `granted` (pre-provision, lease-guarded),
   `requested` (queued), and `releasing` (mid-release) are deliberately out of scope — each
   is handled by its own existing path and a `granted`-with-no-System window is legitimate.
2. `a` has **no live System**: every `systems` row with `allocation_id = a.id` is in a
   terminal System state, or there are no System rows at all. "Live" is the canonical
   non-terminal-System set already defined in `services/systems/admission.py`
   (`_NON_TERMINAL_SYSTEM = {defined, provisioning, ready, reprovisioning, crashed}`); the
   predicate derives the terminal set from it (`SystemState - _NON_TERMINAL_SYSTEM =
   {torn_down, failed}`) rather than hand-rolling literals, so a future System state cannot
   silently make the reaper treat a live System as orphaned. **`crashed` is live**: a crashed
   System whose `active` allocation is held for crash-capture / drgn debugging is the central
   kdive workflow and MUST be preserved — the predicate does so because `crashed ∈
   _NON_TERMINAL_SYSTEM`. (The "no rows" arm is defensive; per the model an `active`
   allocation has a System, but a manual/foreign-key-less edge must still be reclaimable
   rather than wedge forever.)
3. **Mid-provision-race grace:** `a.updated_at < now() - GRACE`. The `allocations_set_updated_at`
   trigger rewrites `updated_at := now()` on **every** row-changing UPDATE, so `updated_at` is
   precisely "time since the last write to the allocation row" — bumped by the `→active` stamp
   and by a `renew` that extends the lease, but **not** by a System-state change (which writes
   the `systems` row, not `allocations`). So a torn-down System leaves the allocation's
   `updated_at` frozen at its last allocation-write — exactly the leak signal — while a freshly
   renewed/touched allocation is protected. Requiring the row settled ≥ `GRACE` keeps the reaper
   away from an allocation whose System is being recreated right now. Because System-terminal is
   a one-way door, this grace is belt-and-suspenders against the narrow window where another
   transaction is mid-flight against the same allocation under the allocation lock. (Tests age
   `updated_at` by disabling the trigger for the single aging UPDATE, since a plain UPDATE would
   be clobbered back to `now()`.)

The candidate SQL (read phase, no lock):

```sql
SELECT a.id, a.project
FROM allocations a
WHERE a.state = 'active'
  AND a.updated_at < now() - %(grace)s
  AND NOT EXISTS (
        SELECT 1 FROM systems s
        WHERE s.allocation_id = a.id
          AND s.state = ANY(%(live_system_states)s)
  )
```

`live_system_states` is the value tuple of `_NON_TERMINAL_SYSTEM` from
`services/systems/admission.py` (`defined`, `provisioning`, `ready`, `reprovisioning`,
`crashed`), imported — not re-spelled — so it never drifts from admission's own quota
predicate. The `NOT EXISTS` (no row matching any **live** state) covers both the
"all Systems terminal" and "no System rows" arms in one predicate.

## Reclaim mechanic

Per candidate, under `advisory_xact_lock(PROJECT) → advisory_xact_lock(ALLOCATION)` (the same
lock order the expiry sweep and release service take — so the reaper, a concurrent release,
and the expiry sweep serialize and never double-act):

1. Re-read the allocation **under the lock**. Skip if it is no longer `active` (a concurrent
   release/expiry won the race) — this is the idempotency + race guard.
2. Re-check the orphaned predicate **under the lock** (re-run the `NOT EXISTS` for this
   allocation): skip if a live System now exists. This closes the read-then-act gap.
3. Release via the **shared release mechanic** already used by `allocations.release` and
   `ops.force_release`: `services/allocation/release.py:release_with_backstops`, which drives
   `active → releasing → released`, stamps `active_ended_at`, and writes the single
   `reconciled` ledger credit. The reaper passes a **guard-exempt system audit writer**
   (`audit.record_system` under `system:reconciler`), exactly as the expiry sweep's
   `_expire_one` writes its audit, because the reconciler is not a project member.
4. Audit the reclaim transition under `system:reconciler` (the release mechanic already
   writes the per-allocation transition rows via the injected writer; the repair logs an
   INFO line for operator visibility).

Releasing to `released` (not `expired`) is correct: this is an *orphan reclaim*, semantically
"the work is done / abandoned, give the slot back", which is what release expresses; `expired`
is specifically the lease-window-elapsed outcome and is owned by the expiry sweep. Both leave
the allocation terminal and free the cap slot identically; using the existing release path
means zero new transition/ledger logic.

### Why reuse `release_with_backstops` rather than a bespoke `→expired` flip

The expiry sweep's `_expire_one` flips straight `active → expired`. Reusing it would mean a
second copy of the active-ended stamp + reconcile + audit. `release_with_backstops` already
encapsulates all of that, is the path `force_release` uses to recover exactly this leak today,
and already maps `IllegalTransition` / terminal-state / `CategorizedError` to a neutral
outcome. The reaper calls it with the system audit writer and treats a non-`released` outcome
as "skip, retry next pass" (logged, not counted).

## Isolation, ordering, and reporting

- New repair function: `reconciler/allocations.py:reap_orphaned_active_allocations(conn) -> int`,
  returning the number of allocations reclaimed. Each candidate is in its own try/except so one
  failure never starves the rest (the established `sweep_expired_allocations` shape).
- New `ReconcileReport` field `reaped_active_allocations: int = 0` and a new `_RepairSpec` in
  `_repair_plan`, placed **after** `expired_allocations` and **before** `promoted_allocations`,
  so a slot this reaper frees is filled by the promotion sweep in the **same** pass (mirroring
  the expiry→promotion ordering the loop docstring already guarantees).
- `ops.reconcile_now` response (`mcp/tools/ops/reconcile.py:_reconcile_response`) gains
  `reaped_active_allocations` in its `data` map, so the MCP caller sees the counter the
  acceptance criteria require.
- Grace constant: `DEFAULT_ORPHANED_ACTIVE_GRACE = timedelta(minutes=2)`, a module constant in
  `reconciler/allocations.py` (matching the existing `DEFAULT_DEBUG_SESSION_STALE_AFTER` 2-min
  precedent for "settled long enough to be safe"). Not config-surfaced unless a need appears
  (YAGNI); it is referenced through the repair so a future config wire is a one-line change.

## Testing (db-tier, testcontainers; skip cleanly without Docker)

In `tests/reconciler/test_orphaned_active_sweep.py`, using the `tests/reconciler/conftest.py`
seeders (`seed_system(system_state=, alloc_state=)`, `seed_run`):

1. **Leaked active → reclaimed:** `active` allocation + `torn_down` System, `updated_at` aged
   past grace → repair returns 1, allocation is `released`, exactly one `reconciled` ledger row,
   `active_ended_at` stamped, cap slot freed.
2. **Failed-System variant → reclaimed:** same with System `failed`.
3. **No-System arm → reclaimed:** `active` allocation with no System row, aged past grace → 1.
4. **Legitimately active → preserved:** `active` allocation + `ready` System → returns 0,
   allocation still `active`.
5. **Active + provisioning System → preserved:** System `provisioning` (non-terminal) → 0.
5b. **Active + crashed System → preserved:** System `crashed` (debugging-in-progress, the
    central kdive workflow) → 0, allocation still `active`. Guards the most dangerous
    false-positive: reaping a slot a live crash investigation is using.
6. **Mid-provision-race grace → preserved:** `active` + `torn_down` System but `updated_at`
   within grace (freshly stamped) → 0; then age it and a second pass reclaims it.
7. **Idempotent re-run:** two passes → first returns 1, second returns 0, exactly one
   `reconciled` row.
8. **Concurrent release vs reap reconciles once:** a holder pre-takes `PROJECT → ALLOCATION`,
   releases the allocation while the reaper blocks, reaper then sees terminal and skips →
   returns 0, exactly one `reconciled` row (mirrors `test_concurrent_release_vs_sweep_reconciles_once`).
9. **`reconcile_once` reports the counter + frees the slot in one pass:** seed leaked active
   + a queued (`requested`) allocation on the same `cap=1` resource; one `reconcile_once`
   reports `reaped_active_allocations == 1` and `promoted_allocations == 1` (the freed slot is
   filled same pass).
10. **One bad candidate does not starve a good one:** an unpriceable leaked active (no
    persisted size → reconcile raises) rolls back and stays `active`; a sibling good leaked
    active is still reclaimed (per-candidate isolation), mirroring
    `test_unpriceable_allocation_does_not_starve_siblings`.

Plus an `ops.reconcile_now` test (`tests/mcp/ops/test_reconcile_now.py`) asserting the
response `data` carries `reaped_active_allocations`.

## Out of scope

- Reclaiming `granted` / `requested` / `releasing` orphans (each handled elsewhere).
- Tearing down the leaked domain/infra — that is the orphaned-System / leaked-domain reaper's
  job (#372 owns orphaned remote domains); a reclaimed allocation whose System is already
  terminal has no live infra to reap.
- Config-surfacing the grace window.
