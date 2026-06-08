# Implementation plan — `resources.drain` (#143)

**Design of record:** [ADR-0062 §3](../../adr/0062-platform-operations.md) ·
[integration contract](../../specs/m1.3-platform-operations.md). The interface, modes,
RBAC escalation, and partial-failure contract are already settled and merged — this plan
adds no new design, only the build sequence. Blockers #136 (cordon plumbing + `cordoned`
column) and #140 (break-glass attribution path) are both **closed**, so the two primitives
`drain` composes already exist.

## What `resources.drain` is

An **action** (not a persisted state) that sets the host's `cordoned` flag, then either
reports or force-releases the host's live allocations:

- `mode=passive` (default) — **`platform_operator`**: cordon, then return the host's live
  allocations and leave them to finish/expire.
- `mode=force_release` — **`platform_admin` + mandatory non-blank `reason`**: cordon, then
  route **each** live allocation through the *same break-glass attribution path as
  `ops.force_release`* (guard-exempt writer, one `platform_audit_log` row per allocation),
  returning a per-allocation result list (released / failed / skipped). A per-allocation
  failure does **not** roll back already-released ones; the host is left partially drained
  and still `cordoned`, so the action is idempotent over the remaining set and re-invokable.

"Live on a host" = the **`_NON_TERMINAL`** set already defined in
`services/allocation_admission.py` ({REQUESTED, GRANTED, ACTIVE, RELEASING}) — the exact set
that occupies a host capacity slot. Reusing it keeps drain's occupancy notion identical to
the admission cap's.

## Acceptance criteria (from the issue)

1. `mode=passive` (`platform_operator`) leaves live allocations running and returns them.
2. A `platform_operator` calling `mode=force_release` is **denied** (requires `platform_admin`).
3. A `platform_admin` `force_release` empties the host and returns a per-allocation result
   list (released/failed/skipped); a partial drain is observable and re-invokable.
4. Both modes leave the host `cordoned`.

## Build sequence

### Step 1 — Extract a shared break-glass single-allocation release (`ops/breakglass.py`)

The per-allocation break-glass mechanic (write the `platform_audit_log` accountability row,
then `release_with_backstops` with the guard-exempt writer, with a warning log) is currently
inlined in `force_release`. Extract it so `drain` routes through *the same path*:

```python
async def breakglass_release_allocation(
    pool, ctx, *, alloc: Allocation, tool: str, reason: str,
) -> ReleaseOutcome:
    """Audit-then-release one allocation via the break-glass path (shared by
    ops.force_release and resources.drain force_release)."""
    await _record_breakglass(pool, ctx, tool=tool, project=alloc.project,
                             object_id=str(alloc.id), reason=reason)
    _log.warning("break-glass release of allocation %s in project %s by %s via %s",
                 alloc.id, alloc.project, ctx.principal, tool)
    return await release_with_backstops(
        pool, alloc.id, project=alloc.project,
        audit_writer=_breakglass_audit_writer(ctx.principal))
```

Refactor `force_release` to resolve its `alloc`, then call this helper with
`tool=_FORCE_RELEASE_TOOL`. Behavior unchanged — existing `test_breakglass.py` stays green
(it pins the per-allocation `audit_log` row counts and the single `platform_audit_log` row).

### Step 2 — Extract the raw cordon update (`catalog/resources.py`)

`_set_cordoned` couples an operator role check + the `UPDATE resources SET cordoned …
RETURNING *` + audit. `drain` needs the same UPDATE but a **mode-dependent** role check and
a `resources.drain` audit tool. Extract the pure update:

```python
async def _apply_cordon(conn, uid, *, cordoned) -> Resource | None:
    """UPDATE resources SET cordoned = … RETURNING the row, or None if the host is gone."""
```

Reuse it from both `_set_cordoned` and `drain`. (Second use justifies the extraction; the
SQL lives in one place.)

### Step 3 — `drain_resource` handler (`catalog/resources.py`)

Order of operations (mirrors `force_release`'s check ordering so denials are audited
consistently):

1. Parse `mode`; an unknown mode → `_error` (config), unaudited (no role resolved). A
   future `migrate` is not yet supported → same config error.
2. Mode-dependent role check:
   - `passive` → `require_platform_role(PLATFORM_OPERATOR)`.
   - `force_release` → `require_platform_role(PLATFORM_ADMIN)`.
   On `AuthorizationError`: `audit_platform_denial(tool="resources.drain",
   scope=f"resource:{resource_id}")` then `_denied`. (An operator calling `force_release`
   holds a platform role, so the denial is audited — AC #2.)
3. `force_release` only: blank `reason` → `_config_error`, no cordon, no audit.
4. Resolve `resource_id` (valid uuid + exists). Bad/missing → `_error`, unaudited.
5. **Cordon**: `_apply_cordon(conn, uid, cordoned=True)` + `_audit_host_action(
   tool="resources.drain", detail="cordoned=true")` — one host-action `platform_audit_log`
   row. This is the post-condition both modes guarantee (AC #4).
6. Snapshot live allocations: `SELECT … FROM allocations WHERE resource_id=%s AND
   state = ANY(_NON_TERMINAL) ORDER BY created_at, id`.
7. Branch:
   - **passive** → build a collection of the snapshot: each item
     `success(str(alloc.id), alloc.state.value, data={project, resource_id})`.
   - **force_release** → for each snapshot row, `breakglass_release_allocation(...,
     tool="resources.drain", reason=reason)`, classify the `ReleaseOutcome`:
     `released` → `success(id, "released")`; `STALE_HANDLE` (already terminal) →
     `success(id, "skipped", data={current_status})`; any other category → `failure(id,
     category, data={current_status})`. Tally released/failed/skipped into the envelope data.
8. Return `collection(resource_id, "cordoned", items, data={"mode": …, counts…})`. The
   `cordoned` top-level status names the guaranteed post-condition; `object_id` is the host.

### Step 4 — Register the tool (`catalog/resources.py register()`)

Add `@app.tool(name="resources.drain", annotations=_docmeta.destructive(),
meta={"maturity": "implemented"})`. `force_release` mode evicts cross-project allocations, so
`destructive()` is the honest annotation. Wrapper params: `resource_id`,
`mode: str = "passive"`, `reason: str = ""`. The wrapper calls `drain_resource(pool,
current_context(), resource_id=…, mode=…, reason=…)`.

### Step 5 — Wire the pinned-metadata tests

- `_docmeta.DESTRUCTIVE_TOOLS`: add `"resources.drain"` (it carries `destructiveHint`, like
  `ops.force_release`; it bypasses — does not *reach* — the three-check gate, so the
  `gate_reachers <= DESTRUCTIVE_TOOLS` guard stays satisfied).
- `tests/mcp/core/test_tool_docs.py` `_BEHAVIOR_TESTS`: map `"resources.drain"` →
  `("tests/mcp/catalog/test_resources_tools.py",)`.
- Regenerate the tool-reference doc if `tests/scripts/test_gen_tool_reference.py` pins it.

## Tests (TDD — write failing first), in `tests/mcp/catalog/test_resources_tools.py`

Add allocation/`platform_audit_log`/`audit_log` fixtures (mirror `test_breakglass.py`):

Passive:
- `passive_operator_cordons_and_reports_live_allocations` — operator, host with 2 live + 1
  terminal alloc → status `cordoned`, host row `cordoned=true`, items = the 2 live (terminal
  excluded), allocations **unchanged** (still active/granted). (AC #1, #4)
- `passive_empty_host_cordons_and_returns_no_items` — operator, no allocations → cordoned,
  zero items.
- `passive_non_operator_denied` — a plain member (no platform role) → `authorization_denied`,
  host **not** cordoned, unaudited (project-only denial isn't recorded).
- `passive_auditor_denied` — auditor (platform role, not operator) → denied **and** audited.

Force-release:
- `force_release_operator_denied` — operator → `authorization_denied`, host **not** cordoned,
  one audited denial row (`scope=resource:{id}`). (AC #2)
- `force_release_blank_reason_rejected` — admin, blank reason → config error, not cordoned,
  no audit, allocations unchanged.
- `force_release_admin_empties_host` — admin, 2 active allocs → status `cordoned`, both items
  `released`, both allocations now `released`, host `cordoned=true`, `data.released=2`. One
  cordon `platform_audit_log` row + one break-glass row per allocation. (AC #3, #4)
- `force_release_terminal_allocation_skipped` — admin, host with an already-`released` alloc
  in the snapshot race window: assert a `STALE_HANDLE` outcome maps to `skipped` (drive via
  the classification — seed a RELEASING + a RELEASED, confirm RELEASED→skipped path). Keep the
  snapshot to `_NON_TERMINAL`, so cover `skipped` via a release that loses the race (use the
  `sized=False`+budget config-error fixture for a `failed` item too).
- `force_release_partial_failure_is_observable_and_reinvokable` — admin, one releasable + one
  that fails reconcile (NULL size + budget): items show one `released` + one `failed`; the
  released allocation is gone, the host stays `cordoned`; a **second** `drain force_release`
  call returns the remaining failed one again (idempotent / re-invokable). (AC #3)
- `force_release_bad_uuid` / `unknown_host` — admin, malformed/missing id → config error,
  unaudited, no cordon.
- `unknown_mode_rejected` — `mode="migrate"` → config error, unaudited, host not cordoned.

## Guardrails

`just lint`, `just type`, `just test` (or the CI workflow's individual recipes) green at
every commit; zero warnings. Regenerate any invalidated golden (tool-reference doc).

## Out of scope

- No `draining` persisted state (ADR-0062 decision 1a).
- No System teardown — drain releases *allocations*; Systems on them are untouched
  (force_teardown's job).
- No `mode=migrate` (M2).
