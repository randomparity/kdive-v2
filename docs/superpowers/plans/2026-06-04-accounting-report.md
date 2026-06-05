# Plan — `accounting.report` (M1.1 P2, issue #97)

Builds the first cross-project read on the platform-RBAC seam (#96, merged). Decision:
[ADR-0043](../../adr/0043-platform-scoped-rbac-tier.md) §3/§4. Contract:
[`m1.1-platform-rbac-tier.md`](../../specs/m1.1-platform-rbac-tier.md). Implementation
reference: [`m1.1-implementation.md`](../../plans/m1.1-implementation.md) Phase B / P2.

The authorization model, the two scope forms, and the audit-by-shape predicate are fully
settled upstream and are NOT re-litigated here. This plan pins the one detail those docs
leave to the implementation: the `report` rollup query and its response shape.

## Settled upstream (do not re-decide)

- **granted-set** form rides `require_role(ctx, p, viewer)` — no platform role. Default set
  is the projects in `ctx.projects` with a **non-None** role (`ctx.roles.get(p) is not None`);
  role-less memberships are dropped, not raised on. (Because `viewer` is rank 0, "ranks ≥
  viewer" is exactly "has any role" — no rank comparison, no reach for the module-private
  `_RANK`.) A **named** `projects` arg authorizes each via `require_role(viewer)`, which
  **raises** `AuthorizationError` for a non-member or role-less project. Zero resolved
  projects → empty rollup (success). Audited to `platform_audit_log` (`platform_role=None`)
  **iff** the **authorized/resolved** set (the project list handed to `report()`, *before* the
  rollup drops projects with no ledger rows) spans >1 project **or** `group_by="principal"`.
  The trigger counts the *read intent*, not the rows returned — a viewer of A+B reads two
  projects and is audited even if only A has spend.
- **all-projects** form gated `require_platform_role(ctx, PLATFORM_AUDITOR)` (satisfied by
  `platform_admin`). Project universe = `SELECT DISTINCT project FROM budgets`. **Always**
  audited on success with the granting role. A denial by a principal holding ≥1 platform role
  is audited (held role recorded) then mapped to a `authorization_denied` failure response; a
  denial by a principal with no platform role writes **no** row.
- Neither form ever writes a per-project `audit_log` row.

## Pinned here: the rollup query + response shape

### Domain — `accounting.report`

`async def report(conn, *, projects, group_by, window) -> Report` in
`src/kdive/domain/accounting.py`.

- `projects: Sequence[str]` — the already-authorized target set (the tool resolves/authorizes;
  the domain layer just aggregates over the set it is handed). Empty set → empty `Report`.
- `group_by: Literal["principal"] | None`.
- `window: tuple[datetime | None, datetime | None] | None` — `(start, end)` half-open bound on
  `ledger.ts` (`ts >= start`, `ts < end`); either side may be `None`.

Per (project) — or per (project, principal) when grouped — sum the **signed ledger**:

- `reserved` = `Σ kcu_delta WHERE event_type='reserved'`
- `reconciled` = `Σ kcu_delta WHERE event_type='reconciled'` (signed credit/debit delta)
- `variance` = `reconciled − reserved` (per ADR-0043 §3 / P2: "variance = reconciled − reserved
  from the signed ledger")

All three pass `quantize_kcu`. The **ungrouped** rollup selects from `ledger` **alone** (no
join), `GROUP BY project`. The `group_by="principal"` rollup adds an **INNER** join
`ledger JOIN allocations ON ledger.allocation_id = allocations.id` and `GROUP BY project,
allocations.principal`; the inner join is provably non-dropping because `ledger.allocation_id`
is `NOT NULL` with an FK to `allocations.id` (schema 0002), so every ledger row — reserved and
reconciled-credit alike — has exactly one matching allocation. Both forms sum the identical
ledger rows; only the grouping key differs. The existing `ledger_allocation_id_idx` covers the
join. Each form is one statement filtered by `ledger.project = ANY(%s)` and the optional `ts`
window — no per-project round trip, so an N-project rollup is one query.

`Report` (frozen dataclass):

```
@dataclass(frozen=True)
class RollupRow:
    project: str
    principal: str | None      # set iff group_by="principal"
    reserved: Decimal
    reconciled: Decimal
    variance: Decimal

@dataclass(frozen=True)
class Report:
    rows: tuple[RollupRow, ...]            # one per (project) or (project, principal)
    total: RollupRow                       # project="*", principal=None; Σ over rows
```

`total.reserved/reconciled/variance` are the sums across all rows (and `variance` of the total
equals `total.reconciled − total.reserved`, consistent with the per-row rule). `total.project`
is `"*"` and `total.principal` is **always** `None` — the total is cross-principal by
definition, even when `group_by="principal"`.

A project in the authorized set with **no** ledger rows contributes no `RollupRow` (an empty
rollup is the natural "nothing spent" answer); the total over an empty set is all-zero. The
row JSON in the response **always includes the `principal` key** (null when ungrouped), so the
response shape is uniform regardless of `group_by`.

### Tool — `accounting.report`

`async def report(pool, ctx, *, scope, projects=None, group_by=None, window=None)` in
`src/kdive/mcp/tools/accounting.py`, registered as `accounting.report`.

- Validate `scope ∈ {granted-set, all-projects}` and `group_by ∈ {None, principal}`
  (else `configuration_error`). Parse `window` (two optional ISO-8601 strings →
  `datetime`; malformed → `configuration_error`).
- **granted-set**: resolve target set (default vs named per the settled rules), then
  `report(...)`, then audit-by-shape. `require_role` raising on a named non-member
  propagates (matches `accounting.usage`).
- **all-projects**: `require_platform_role(...)` in a `try`; on `AuthorizationError`,
  audit the denial iff `ctx.platform_roles` non-empty, then return
  `ToolResponse.failure(AUTHORIZATION_DENIED)`. On success: resolve the universe, `report(...)`,
  always audit with the held role string.
- Response `data`: `scope`, `group_by`, the resolved `projects`, a JSON list of rows
  (`project`, `principal`, `reserved`, `reconciled`, `variance` as strings), and the `total`.
  `suggested_next_actions=["accounting.usage"]`.

Audit `scope` column value: `"all-projects"` for that form; `"granted-set:<comma-joined sorted
projects>"` for the member form. `args` digested = the public tool args (`scope`, `group_by`,
`window`, `projects`).

**Audit transaction semantics.** A report is a pure read — the audit row has nothing to
compose with — so every `record_platform` call (success *and* denial) runs in its **own**
short `conn.transaction()` that commits before the handler returns. The success audit and the
rollup may share the read connection but the audit commits on its own. Tests assert
`platform_audit_log` counts on a **fresh** connection *after* the handler returns, never mid-
flight, so the count reflects committed rows only (mirrors `test_record_platform_composes_in_
caller_transaction`, which proves the writer respects its caller's transaction boundary).

## TDD order (each acceptance bullet → ≥1 test)

Domain (`tests/domain/test_accounting_report.py`):
1. multi-project rollup: per-project + total `reserved/reconciled/variance` match a hand-
   computed ledger across ≥2 projects.
2. `group_by=principal`: per-(project,principal) totals via `ledger ⋈ allocations` match.
3. `window`: rows outside `(start,end)` on `ts` excluded.
4. empty project set → empty `Report` (zero total).

Tool (`tests/mcp/test_accounting_report.py`):
5. all-projects: `platform_auditor` token → rollup ≥2 projects, matches hand-computed; writes
   exactly one `platform_audit_log` row (role set), zero `audit_log` rows.
6. all-projects: `platform_admin` satisfies the auditor gate (rollup + audit row).
7. SoD: project-only `viewer`/`operator`/`admin` token denied all-projects → failure
   `authorization_denied`, **no** `platform_audit_log` row.
8. SoD: `platform_operator` denied all-projects → failure, **one** denial row (held role
   `platform_operator`).
9. granted-set default: viewer on A+B, bare member of C → rollup over exactly A+B (C dropped);
   one audit row (>1 project, `platform_role` null); no `audit_log` row.
9b. granted-set default: viewer on A+B where **only A** has ledger rows → still one audit row
    (the trigger counts the 2-project *authorized* set, not the 1 returned RollupRow).
10. granted-set default: all memberships role-less → empty rollup (success), no audit row.
11. granted-set named non-member or role-less project → `AuthorizationError` raised.
12. granted-set single project, ungrouped → no `platform_audit_log` row.
13. granted-set single project, `group_by=principal` → one audit row (group_by trigger).
14. granted-set `group_by=principal` over a window → per-principal totals match.
15. invalid `scope` / `group_by` / `window` → `configuration_error`.

## Guardrails

`just lint`, `just type`, `just test` green at every commit. No schema change beyond #96's
`platform_audit_log`. No new dependency.
