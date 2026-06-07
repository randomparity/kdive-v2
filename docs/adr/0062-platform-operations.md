# ADR 0062 — Platform operations (M1.3)

- **Status:** Proposed
- **Date:** 2026-06-06
- **Deciders:** platform maintainers
- **Builds on (does not supersede):** [ADR-0043](0043-platform-scoped-rbac-tier.md) — which
  settled the `platform_roles` model, the `require_platform_role` seam, and the
  `platform_audit_log` table in M1.1, and **deferred every mutating platform tool and the
  second/third auditor reads to M1.3 (§5)**. Also builds on
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (the `require_role`/audit gate and the
  three-check destructive gate), [ADR-0021](0021-reconciler-loop-drift-repair.md) (the
  reconciler), and [ADR-0008](0008-async-worker-tier-job-queue.md) (the worker/queue tier).
- **Spec:** [`../specs/m1.3-platform-operations.md`](../specs/m1.3-platform-operations.md) ·
  **Detailed design:**
  [`../superpowers/specs/2026-06-06-platform-operations-design.md`](../superpowers/specs/2026-06-06-platform-operations-design.md)

## Context

ADR-0043 built the platform-role model in M1.1 but shipped only one consumer
(`accounting.report`), deferring the rest to "the Platform-operations milestone (M1.3)" on the
reasoning that the role model is the load-bearing, expensive-to-change part and the tools
should be shaped against real operational use. M1.3 (`top-level-design.md` §Roadmap, the M1.x
local-libvirt feature-deepening band) realizes that deferred set: `platform_operator` infra
tools (host cordon/drain/maintenance, on-demand reconcile, queue control, capacity/cost
tuning), `platform_admin` break-glass (cross-project teardown/force-release), the remaining
`platform_auditor` reads (`audit.query`, `inventory.list`), and the bare-`require_role`
denial-audit retrofit (ADR-0043 §4).

Because the authorization machinery already exists, **M1.3 adds tools and one hardening
retrofit, not authorization plumbing.** Each new tool gates on the shipped
`require_platform_role` and (for cross-tenant actions) records to the shipped
`platform_audit_log`. The open decisions are tool *shape*, not the role model.

## Decision

### 1. Ship the full deferred set; scope two surfaces to the minimum with a demonstrated need

M1.3 ships everything ADR-0043 §5 named — nothing is deferred further. Two surfaces that could
be over-built are scoped down: **queue/worker control is pause/resume + cross-project inspect
only** (no manual job requeue/force-fail — the reconciler already auto-repairs abandoned jobs,
and a manual requeue would race it — and no worker orchestration, which is speculative on a
single operator-run worker); **capacity/cost tuning is cost-class coefficients + per-host
capacity caps only** (the two fleet-level knobs with no existing tool; the global `W_CPU`/`W_MEM`
weights stay code-config, and per-project budgets/quotas stay on `accounting.set_*`).

### 2. Two namespaces: `resources.*` for host actions, `ops.*` for control-plane and break-glass

Host-scoped operator actions extend `resources.*` (the host noun already lives there);
control-plane actions (reconcile, queue, tuning) and the break-glass tools take a new `ops.*`
namespace. Break-glass is its own `ops.*` tool, not an extension of `systems.teardown` /
`allocations.release`, so the override path is separate and the per-project tools are untouched.

### 3. `cordoned` is a new healthy host status; `drain` is an action, not a status

The `resources.status` enum gains **`cordoned`** — a healthy host that admission skips — kept
distinct from `offline` so a host drained for maintenance and a crashed host read differently.
`resources.cordon`/`uncordon` toggle it; `resources.set_status` covers the operational triad
(`available`/`degraded`/`offline`) and deliberately cannot reach `cordoned` (separate verb,
separate audit transition). **Drain is modeled as an action, not a persisted state**
(*decision 1a*): `resources.drain` cordons, then `mode=passive` (default — report live
allocations, let them finish) or `mode=force_release` (gated — force-release each). There is no
`draining` status; "a drain is in progress" is not modeled as state. A future `mode=migrate`
(M2) slots in without reshaping the tool.

### 4. Break-glass is a separate path that fully overrides the three-check gate

`ops.force_teardown` / `ops.force_release` (`platform_admin`) **bypass
`assert_destructive_allowed` entirely.** The three-check gate (capability scope, project role,
profile opt-in) protects a member acting within their own project; a stuck cross-project object
typically fails all three, which is precisely when break-glass is needed. Authority comes
solely from `require_platform_role(PLATFORM_ADMIN)` + a mandatory non-blank `reason` + an
always-written `platform_audit_log` row. They reuse the per-project tools' internal
teardown/release execution; only authorization differs. The per-project tools are unchanged.

### 5. `require_role` denials are audited by a centralized dispatch-boundary catch into `audit_log`

The retrofit catches `AuthorizationError` **once at the MCP tool-dispatch boundary**, records a
denial row, and re-raises — one code path covering every current and future tool, with no edits
to the ~40 `require_role` call sites (*centralized*, not per-site, not via an enriched
exception). Denial rows land in **`audit_log`** (*decision 2a*) via a new guard-exempt
`audit.record_denial()` writer, following `audit.record_system()`'s precedent (it already
writes `audit_log` without the membership guard): a denial records a would-be-non-member, which
is legitimate, not the misattribution `audit.record()` defends against. Because a
dispatch-boundary catch is object-agnostic, the retrofit makes `audit_log.project` /
`object_kind` / `object_id` **nullable**, guarded by a CHECK that keeps them `NOT NULL` for
every transition except `'denied'` — every real-transition row retains the original invariant.
`require_platform_role` denials stay out of scope (already audited to `platform_audit_log` in
M1.1).

## Consequences

- The full `platform_operator`/`platform_admin` tool surface becomes callable; the
  separation-of-duties the role model expressed (operator ≠ data reader ≠ break-glass admin) is
  now exercised by real tools and tests.
- New migrations: a `cordoned` enum value, a single-row `ops_control` table (`queue_paused`),
  and an `audit_log` change (nullable object columns + CHECK + a `reason` column). The
  `audit_log` change relaxes a long-standing `NOT NULL` invariant — mitigated by the CHECK, but
  any consumer that assumed non-null object columns must now handle `'denied'` rows.
- The worker gains a pre-`dequeue` read of `queue_paused` — one extra cheap query per claim
  loop while idle.
- Break-glass is a deliberately un-gated-by-the-three-checks path: its only brakes are the
  `platform_admin` grant and the audit trail. This is the intended trade for break-glass
  utility; the audit row + mandatory reason is the accountability mechanism.
- A new audit pattern (denial rows in `audit_log`) joins the read-access pattern
  (`platform_audit_log`) from M1.1; future tools inherit both via the dispatch boundary and the
  `require_platform_role` seam.
- No core/plane interface changes — M1.3 is tools over settled seams, consistent with the
  roadmap's "add tools, not machinery" framing for the M1.x band.

## Alternatives considered

- **Defer the mutating tools again** (minimal: auditor reads + denial-audit only). Rejected:
  ADR-0043 already deferred them once to shape against use; the operational need (cordon a host
  for maintenance, break-glass a stuck cross-project allocation) is concrete now, and leaving
  operators without remediation tools is the worse failure mode.
- **Extend `systems.teardown`/`allocations.release` for break-glass** (admin substitutes the
  role check only). Rejected: it still requires capability scope + profile opt-in, so it cannot
  unstick the common stuck case (scope/opt-in absent) — barely break-glass — and it entangles
  the per-project tools with cross-project override logic.
- **A persisted `draining` status** distinct from `cordoned`. Rejected (decision 1a): drain is
  driven by the operator as an action; persisting an in-progress flag adds an enum state and
  transitions to maintain for observability the operator can get from `inventory.list` /
  `ops.jobs_list`.
- **Denials to `platform_audit_log` or a new `access_denials` table** (2b/2c). Rejected
  (decision 2a): per-project denials belong with the per-project trail; reusing `audit_log` via
  a guard-exempt writer keeps one table and follows the existing `record_system` precedent. A
  dedicated table is more separation than the data warrants.
- **Per-call-site denial try/except** or **enriching `AuthorizationError`** with structured
  fields. Rejected: per-site is ~40 near-identical edits, easy to miss, and re-required for
  every new tool; enriching the exception raises fidelity but was declined in favor of the
  smallest central change — it remains a possible later refinement, not M1.3 scope.
- **Job requeue/force-fail and worker orchestration** in queue control; **runtime `W_CPU`/`W_MEM`
  weights** in cost tuning. Rejected as over-build (decision 1): no current caller, and they
  overlap the reconciler (requeue) or belong in version-controlled config (weights).
