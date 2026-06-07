# M1.3 — Platform Operations (detailed design)

- **Date:** 2026-06-06
- **Milestone:** M1.3 (the M1.x local-libvirt feature-deepening band)
- **Decisions:** [ADR-0062](../../adr/0062-platform-operations.md)
- **Integration contract:** [`../../specs/m1.3-platform-operations.md`](../../specs/m1.3-platform-operations.md)
- **Parent:** [`top-level-design.md`](../../specs/top-level-design.md) §Roadmap (M1.3)
- **Builds on:** [ADR-0043](../../adr/0043-platform-scoped-rbac-tier.md) (the platform-role
  seam, which M1.1 shipped), [ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md)
  (the `require_role`/audit gate and the three-check destructive gate, refined by
  [ADR-0038](../../adr/0038-system-reprovision-in-place.md) §3 for the per-op role factor),
  [ADR-0021](../../adr/0021-reconciler-loop-drift-repair.md) (the reconciler loop and its
  system-attributed audit), [ADR-0008](../../adr/0008-async-worker-tier-job-queue.md) (the
  worker/queue tier).

## Why this milestone exists

M1.1 settled the **platform-role model** — `platform_admin` / `platform_operator` /
`platform_auditor`, the `require_platform_role` seam, and the `platform_audit_log`
read-access table — but shipped only one consumer (`accounting.report`). ADR-0043 §5
deliberately deferred every *mutating* platform tool and the second/third auditor reads to
"the Platform-operations milestone (M1.3)," on the reasoning that the load-bearing,
expensive-to-change part (the role model) should land first and the tools should be shaped
against real operational use.

M1.3 builds those tools. **Because the authorization machinery already exists, M1.3 adds
tools and one hardening retrofit — not authorization plumbing.** Every new tool gates on the
already-shipped `require_platform_role` and (for cross-tenant actions) records to the
already-shipped `platform_audit_log`.

The deferred set, from ADR-0043 §5 and the roadmap (`top-level-design.md` §M1.3):

- **`platform_operator` infra tools** — host cordon / drain / maintenance status,
  on-demand reconcile, queue pause/resume + cross-project inspection, runtime
  capacity/cost tuning.
- **`platform_admin` break-glass** — cross-project teardown and force-release.
- **The remaining `platform_auditor` reads** — `audit.query`, `inventory.list`.
- **The bare-`require_role` denial-audit retrofit** (ADR-0043 §4).

## Scope decision

The full deferred set ships in M1.3 (no further deferral). Two surfaces that could have been
over-built are scoped down to the minimum with a demonstrated need:

- **Queue/worker control** is **pause/resume + cross-project inspect only** — not job
  requeue/force-fail (the reconciler already auto-repairs abandoned jobs and requeues
  failures; a manual requeue tool would have two actors racing the same row) and not
  worker-level orchestration (speculative on a single operator-run worker).
- **Capacity/cost tuning** is **cost-class coefficients + per-host capacity caps only** —
  the two genuinely fleet-level, runtime-relevant knobs with no existing tool. The global
  reference weights `W_CPU` / `W_MEM` stay code-config (a model-calibration release decision,
  not a live knob); per-project budgets/quotas stay on `accounting.set_budget` /
  `accounting.set_quota` (project `admin`).

## Tool namespace

Two namespaces, by the noun each acts on:

- **`resources.*`** — host-scoped operator actions (the `resources` table is the host noun;
  it already hosts `resources.list` / `.describe`).
- **`ops.*`** — control-plane operator actions (reconcile, queue, tuning) **and** the
  `platform_admin` break-glass tools. Break-glass lives in its own `ops.*` tools rather than
  extending `systems.teardown` / `allocations.release`, so the break-glass path is a separate,
  fully-overriding code path (below) and the per-project tools stay exactly as they are.

Auditor reads extend the existing `audit.*` namespace and add `inventory.*`, per ADR-0043 §3.

## Tool inventory

| Tool | Role | Annotation | Summary |
|------|------|------------|---------|
| `resources.set_status` | `platform_operator` | mutating | set a host's operational status: `available` / `degraded` / `offline` |
| `resources.cordon` | `platform_operator` | mutating | mark a host `cordoned` (admission skips it; health unchanged) |
| `resources.uncordon` | `platform_operator` | mutating | clear `cordoned` back to `available` |
| `resources.drain` | `platform_operator` | mutating | cordon, then `mode=passive` (default; report live allocations) or `mode=force_release` (gated; force-release each live allocation) |
| `ops.reconcile_now` | `platform_operator` | mutating | run one `reconcile_once()` pass on demand; return the repair summary |
| `ops.queue_pause` | `platform_operator` | mutating | set the global `queue_paused` flag the worker checks before `dequeue` |
| `ops.queue_resume` | `platform_operator` | mutating | clear the `queue_paused` flag |
| `ops.jobs_list` | `platform_operator` | read-only | cross-project queue depth / job states (the platform view of `jobs.list`) |
| `ops.set_cost_class_coeff` | `platform_operator` | mutating | upsert a `cost_class → coeff` row (drives all pricing) |
| `ops.set_host_capacity` | `platform_operator` | mutating | update a resource's `concurrent_allocation_cap` |
| `ops.force_teardown` | `platform_admin` | destructive | break-glass cross-project System teardown; bypasses the three-check gate; mandatory `reason`; always audited |
| `ops.force_release` | `platform_admin` | destructive | break-glass cross-project allocation release; same |
| `audit.query` | project `admin` / `platform_auditor` | read-only | read `audit_log`: project-scoped form (`admin` on that project) or cross-project form (`platform_auditor`) |
| `inventory.list` | `platform_auditor` | read-only | cross-project systems/allocations summary |

The denial-audit retrofit adds no tool — it is a dispatch-boundary catch (below).

## Component designs

### 1. Host status: cordon / drain (`area:resources`)

The `resources.status` enum is `available` / `degraded` / `offline` today
(`db/schema/0001_init.sql`). We add one value, **`cordoned`**: a *healthy* host that admission
skips. Keeping `cordoned` distinct from `offline` matters — a host drained for maintenance and
a host that crashed must read differently in `inventory.list` and to the reconciler.

- **Placement becomes status-aware.** Today host selection is by `kind` only — `_pick_resource`
  in `mcp/tools/allocations.py` runs `SELECT * FROM resources WHERE kind = %s ORDER BY
  created_at, id LIMIT 1` with **no status filter** (a single-host walking-skeleton artifact).
  Issue 1 adds the filter that excludes a `cordoned` host (and, for correctness, an `offline`
  one) from the candidate set — this *is* the mechanism by which cordon stops new placement;
  there is no pre-existing status exclusion to extend. Existing allocations on a cordoned host
  are untouched — cordon stops *new* placement only.
- **`resources.set_status`** is the general operator setter for the operational triad
  (`available` / `degraded` / `offline`). It does **not** set `cordoned`; cordon has its own
  verb so the intent (and its audit transition) is unambiguous.
- **`resources.cordon` / `resources.uncordon`** toggle `cordoned ⇄ available`. `uncordon`
  refuses if the current status is `offline`/`degraded` (those are not the cordon axis) — it
  only clears a cordon.
- **`resources.drain`** is an *action*, not a status (decision 1a): it sets `cordoned`, then:
  - `mode=passive` (default) — returns the live allocations on the host and leaves them to
    finish/expire. The operator polls (e.g. `inventory.list` / `ops.jobs_list`) until the host
    is empty, then `resources.set_status offline` for maintenance.
  - `mode=force_release` — force-releases each live allocation via the same internal path as
    `ops.force_release` (breaks running developer work; gated `platform_operator` plus a
    mandatory `reason`, always audited per live allocation).
  - A future `mode=migrate` (M2, when a second host exists) slots in here without reshaping the
    tool.

There is **no** persisted `draining` status: "a drain is in progress" is not modeled as state;
the host is simply `cordoned` and the operator drives the rest.

### 2. On-demand reconcile (`area:ops`)

`ops.reconcile_now` runs a single `reconcile_once()` pass against a fresh connection and
returns its repair summary (counts per repair class: expired-allocation sweep, orphaned-system
teardown, abandoned-job reaping, dead-session detach, leaked-domain destroy, idempotency-key
GC, abandoned-upload reaping). It does **not** stop or restart the periodic reconciler loop —
it triggers one extra pass concurrently. Gated `platform_operator`, audited to
`platform_audit_log` (a cross-project control action).

### 3. Queue pause/resume + cross-project inspect (`area:ops`)

- A single-row **`ops_control`** table holds `queue_paused boolean`. The **worker** reads it at
  the top of its claim loop and skips `dequeue` while paused (it keeps heart-beating any job
  already in flight — pause stops *new* claims, never abandons running work). DB-backed, not an
  in-process flag, because the operator's MCP call and the worker run in different processes.
- **`ops.queue_pause` / `ops.queue_resume`** set/clear the flag. Gated `platform_operator`,
  audited.
- **`ops.jobs_list`** is the cross-project view of `jobs.list`: queue depth and per-job state
  across all projects, gated `platform_operator` and read-audited. Per-job *mutation* beyond
  what project-scoped `jobs.cancel` already offers is out of scope.

### 4. Runtime capacity / cost tuning (`area:ops`)

- **`ops.set_cost_class_coeff(cost_class, coeff)`** upserts the `cost_class_coefficients` row.
  The pricing path is already DB-backed and fail-closed on a missing row
  (`domain/cost.py`), so this is a direct upsert; new pricing applies to ledger writes from the
  next charge onward (it does **not** retro-reprice committed ledger entries). Gated
  `platform_operator`, audited.
- **`ops.set_host_capacity(resource_id, concurrent_allocation_cap)`** updates the host's
  `concurrent_allocation_cap` in its `capabilities` jsonb — the value admission's per-host cap
  reads (`domain/allocation_admission.py`). Lowering the cap below the current live count does
  not evict anyone; it blocks *new* placement until the count falls. Gated `platform_operator`,
  audited.

### 5. Break-glass: force teardown / force release (`area:ops`)

`ops.force_teardown(system_id, reason)` and `ops.force_release(allocation_id, reason)` are the
`platform_admin` break-glass path. They **bypass `assert_destructive_allowed` entirely** — the
three-check gate (capability scope, project role, profile opt-in) exists to protect a member
operating within their own project, and a stuck cross-project object typically fails all three
(the caller is not a member; scope/opt-in may be absent), which is exactly when break-glass is
needed. Authority comes solely from:

- `require_platform_role(ctx, PLATFORM_ADMIN)`, **and**
- a non-empty `reason` string (rejected if blank), **and**
- an always-written `platform_audit_log` row (`scope` records the target's project + object id;
  the `reason` rides the `args` digest input, and is also stored verbatim — see the schema
  note).

They reuse the **same internal teardown / release execution** as the per-project tools (enqueue
a teardown job / run the release state transition); only the *authorization* differs. The
per-project `systems.teardown` / `allocations.release` are unchanged.

### 6. Auditor reads: `audit.query`, `inventory.list` (`area:audit`)

Per ADR-0043 §3:

- **`audit.query`** reads `audit_log` with two forms: a **project-scoped** form requiring
  `admin` **on that project** (the audit trail is sensitive), and a **cross-project** form
  requiring `platform_auditor`. Filterable by principal / object / time window / transition.
  The cross-project form is read-audited to `platform_audit_log` — and because the read target
  is `audit_log` while the read-access record lands in `platform_audit_log`, a platform read
  never pollutes the per-project trail it inspects (ADR-0043 §4).
- **`inventory.list`** is a cross-project systems/allocations summary (host, status, project,
  lease/lifecycle state), gated `platform_auditor`, read-audited. This is the fleet-wide view
  the operator uses to confirm a drain has emptied a host.

### 7. Denial-audit retrofit (`area:security`)

Today a bare `require_role` denial raises `AuthorizationError` and is caught at the
handler/transport boundary with **no** audit row — only the destructive gate audits denials
(ADR-0043 §4 named the broader retrofit as deferred to M1.3).

**Mechanism (centralized at the dispatch boundary):** the MCP tool-dispatch wrapper catches
`AuthorizationError` once, records a denial row, and re-raises. One code path covers every
current and future tool — no edits to the ~40 `require_role` call sites, and no per-tool
discipline to remember.

**Target table (decision 2a):** denial rows land in `audit_log` via a new guard-exempt
`audit.record_denial()` writer, following the precedent of `audit.record_system()` (which
already writes `audit_log` without the `project in ctx.projects` membership guard, for the
reconciler). A denial is, by definition, an attempt by someone who lacks access — recording
"non-member X was denied on project P" is legitimate, not the misattribution `audit.record()`'s
guard defends against.

**Schema consequence:** a dispatch-boundary catch is object-agnostic — it reliably knows the
actor (`principal`, `agent_session`), the `tool`, and the call `args`, but not necessarily the
`project`/`object_kind`/`object_id` (a tool often resolves those from an object lookup *inside*
the handler, after the gate). `audit_log` has all three `NOT NULL`. The retrofit therefore
makes `project`, `object_kind`, `object_id` **nullable**, guarded by a CHECK that keeps them
`NOT NULL` for every transition except the denial transition:

```sql
ALTER TABLE audit_log ALTER COLUMN project     DROP NOT NULL;
ALTER TABLE audit_log ALTER COLUMN object_kind DROP NOT NULL;
ALTER TABLE audit_log ALTER COLUMN object_id   DROP NOT NULL;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_object_present_unless_denied
  CHECK (transition = 'denied'
         OR (project IS NOT NULL AND object_kind IS NOT NULL AND object_id IS NOT NULL));
```

Every real-transition row keeps the original invariant; only `'denied'` rows may omit the
object. The denial row records `project`/`object_*` when the wrapper can extract them
(best-effort from the call), and NULL otherwise — the accountability goal is *the actor and the
attempt*, matching how the destructive-gate denial-audit already records the attempt rather
than the (absent) object. This retrofit deliberately does **not** enrich `AuthorizationError`
to carry structured fields (a considered, rejected option — see ADR-0062); raising fidelity
that way is a possible later refinement, not M1.3 scope.

`require_platform_role` denials are out of this retrofit's scope — they are already audited to
`platform_audit_log` (M1.1, ADR-0043 §4) for a principal holding ≥1 platform role.

## Schema deltas (summary)

1. **`resources.status`** gains the value `cordoned` (a healthy, admission-skipped state).
2. **`ops_control`** — a new single-row table: `queue_paused boolean NOT NULL DEFAULT false`
   (plus an `updated_at` / single-row guard).
3. **`audit_log`** — `project` / `object_kind` / `object_id` become nullable, guarded by a
   CHECK that requires them for every non-`denied` transition; add a `reason text` column used
   by break-glass and denial rows (NULL elsewhere).

No new table for cost coefficients or host caps — both are already DB-backed
(`cost_class_coefficients`; resource `capabilities` jsonb).

## Authorization & audit summary

| Tool | Gate | Audited to |
|------|------|-----------|
| `resources.*` (set_status/cordon/uncordon/drain) | `require_platform_role(PLATFORM_OPERATOR)` | `platform_audit_log` |
| `ops.reconcile_now` / `queue_pause` / `queue_resume` / `jobs_list` | `require_platform_role(PLATFORM_OPERATOR)` | `platform_audit_log` |
| `ops.set_cost_class_coeff` / `set_host_capacity` | `require_platform_role(PLATFORM_OPERATOR)` | `platform_audit_log` |
| `ops.force_teardown` / `force_release` | `require_platform_role(PLATFORM_ADMIN)` + `reason` | `platform_audit_log` (always) |
| `audit.query` (project form) | `require_role(project, admin)` | — (matches `accounting.usage`) |
| `audit.query` (cross-project) / `inventory.list` | `require_platform_role(PLATFORM_AUDITOR)` | `platform_audit_log` |
| denial-audit retrofit | n/a (catch) | `audit_log` (`transition='denied'`) |

`platform_admin` satisfies `platform_auditor` (ADR-0043 §2), so an admin can call the auditor
reads; `platform_operator` satisfies neither the auditor reads nor the admin break-glass.

## Decomposition into single-PR issues

| # | Issue | area | Depends on |
|---|-------|------|-----------|
| 1 | Host status `cordoned` + `resources.set_status` / `cordon` / `uncordon` + admission skips cordoned | `area:resources` | — |
| 2 | `resources.drain` (`mode=passive` / `force_release`) | `area:resources` | 1 |
| 3 | `ops.reconcile_now` | `area:ops` | — |
| 4 | `ops_control` table + `ops.queue_pause` / `queue_resume` + worker honors the flag + `ops.jobs_list` | `area:ops` | — |
| 5 | `ops.set_cost_class_coeff` + `ops.set_host_capacity` | `area:ops` | — |
| 6 | `ops.force_teardown` + `ops.force_release` (break-glass) | `area:ops` | — |
| 7 | `audit.query` + `inventory.list` (auditor reads) | `area:audit` | — |
| 8 | Denial-audit retrofit (`audit_log` nullable-object migration + `record_denial` + dispatch-boundary catch) | `area:security` | — |

Issue 2 is the only hard dependency (drain needs the `cordoned` state and cordon plumbing from
issue 1). Issues 1, 3, 4, 5, 6, 7, 8 are mutually independent → one orchestration wave of
seven, then issue 2.

## Exit criteria

Falsifiable; each becomes a test.

1. **Cordon excludes a host from placement.** A `cordoned` host is skipped by admission; its
   existing allocations are untouched; `uncordon` restores placement. `set_status` cannot
   reach `cordoned` and `cordon` cannot reach the operational triad (verbs don't cross axes).
2. **Drain modes behave.** `mode=passive` returns the live allocations and leaves them running;
   `mode=force_release` force-releases each and the host reaches zero live allocations; both
   leave the host `cordoned`.
3. **`reconcile_now` runs one pass.** A pending repair (e.g. an orphaned system) is resolved by
   a single `ops.reconcile_now` call, which returns a summary; the periodic loop is unaffected.
4. **Queue pause stops new claims, not running work.** With `queue_paused=true`, the worker
   claims no new job but completes one already in flight; `resume` restores claiming.
5. **Tuning takes effect.** After `set_cost_class_coeff`, the next charge uses the new coeff
   (committed ledger rows are unchanged); after `set_host_capacity`, admission honors the new
   cap; lowering below the live count blocks new placement without evicting.
6. **Break-glass overrides the gate and is audited.** `ops.force_teardown` /
   `force_release` succeed against a cross-project object whose capability scope and profile
   opt-in would fail the three-check gate, for a `platform_admin` who is **not** a member of the
   object's project; a blank `reason` is rejected; every call writes a `platform_audit_log` row;
   a `platform_operator` (non-admin) token is denied.
7. **Auditor reads are correct and gated.** `audit.query` project form requires `admin` on that
   project; its cross-project form and `inventory.list` require `platform_auditor` (satisfied by
   `platform_admin`), are denied to a project-only token, and write a `platform_audit_log` row
   (not an `audit_log` row).
8. **`require_role` denials are audited.** A denied per-project tool call writes one `audit_log`
   row with `transition='denied'` recording principal/agent_session/tool (and project/object
   when resolvable, NULL otherwise); the CHECK rejects a non-`denied` row with a NULL object;
   the success path is unchanged.
