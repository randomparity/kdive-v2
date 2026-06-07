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

### 3. Schedulability (`cordoned`) is a separate axis from health (`status`); `drain` is an action

Cordon and host health are **orthogonal** — a host can be cordoned for maintenance *and*
degraded, or healthy *and* cordoned — so they are separate columns, not values of one enum (the
k8s lesson the rationale here invokes: the `Unschedulable` flag lives apart from the `Ready`
condition precisely so a node can be both). We add a boolean **`cordoned`** column to
`resources`, independent of the existing `status` (`available`/`degraded`/`offline`). Putting
`cordoned` into the `status` enum instead would mean a crashed cordoned host cannot read as both,
and `set_status offline` would clobber an operator's cordon (and a recovered host could strand);
separate columns never clobber each other. **Placement becomes schedulability-aware on both
selection paths** (`_resolve_resource`, `mcp/tools/allocations.py`): the pick-by-kind query
(`… WHERE kind=%s … LIMIT 1`, today unfiltered) gains `AND status='available' AND NOT cordoned`,
**and** an explicit `resource_id` that names a `cordoned`/non-`available` host is **rejected** —
placing new work on a cordoned host is exactly what cordon forbids, so naming the host by id must
not be an escape hatch. `resources.cordon`/`uncordon` toggle the boolean; `resources.set_status`
sets the health triad; the verbs stay on their own axes.

**Drain is modeled as an action, not a persisted state** (*decision 1a*): `resources.drain` sets
`cordoned`, then `mode=passive` (default — report live allocations, let them finish) or
`mode=force_release` (force-release each). The two modes **carry different authority**: cordon
plus passive drain is a `platform_operator` action, but `mode=force_release` empties a host of
**every tenant's** allocations — a cross-project destructive action — so it **escalates to
`require_platform_role(PLATFORM_ADMIN)` + a mandatory non-blank `reason`** and routes each release
through the **same break-glass attribution path as `ops.force_release`** (the guard-exempt writer,
one `platform_audit_log` row per allocation; §4). An operator can cordon and passively drain;
only an admin can forcibly evict tenants — filing force-release under `platform_operator` would be
a separation-of-duties hole, since §4 reserves cross-project force-release for `platform_admin`
break-glass. `mode=force_release` returns a **per-allocation result list** (released / failed /
skipped) so a partial drain is observable and the action stays re-invokable (idempotent over the
remaining set — a per-allocation failure does not roll back already-released ones; the host is
left partially drained and still `cordoned`). There is no `draining` flag; "a drain is in
progress" is not modeled as state. A future `mode=migrate` (M2) slots in without reshaping the
tool.

### 4. Break-glass is a separate path that fully overrides the three-check gate

`ops.force_teardown` / `ops.force_release` (`platform_admin`) **bypass
`assert_destructive_allowed` entirely.** The three-check gate (capability scope, project role,
profile opt-in) protects a member acting within their own project; a stuck cross-project object
typically fails all three, which is precisely when break-glass is needed. Authority comes
solely from `require_platform_role(PLATFORM_ADMIN)` + a mandatory non-blank `reason` + an
always-written `platform_audit_log` row. They reuse the per-project tools' internal
teardown/release **execution**, but not its audit attribution: `audit.record()` enforces
`project in ctx.projects` (a misattribution guard), and a break-glass `platform_admin` is by
definition **not a member** of the target project — so the reused path must write its audit
through a guard-exempt attribution writer (the `audit.record_system`/`record_platform` pattern),
recording the platform principal against the target's project. This reuse is **not drop-in**:
the release mechanic couples the state transition with the membership-guarded `audit.record()`
in one helper (`_transition_and_audit` inside `_release_locked`, `mcp/tools/allocations.py`), and
`audit.record()` raises on non-membership — which a break-glass admin always trips. A prerequisite
refactor (in-scope M1.3, issue 6) **parameterizes the audit writer** in that shared transition
helper so break-glass can inject a guard-exempt (`record_system`/`record_platform`-shaped) writer
while the per-project tools keep passing the membership-guarded `record`. **Both authorization and
audit attribution differ** from the per-project tools; only the teardown/release mechanics are
shared. The per-project tools are unchanged.

### 5. `require_role` over-reach denials are audited via a dedicated `RoleDenied` exception caught at the dispatch boundary

This milestone **introduces** a dedicated **`RoleDenied`** subclass of `AuthorizationError`
(it does not exist pre-M1.3 — today `require_role` raises the bare `AuthorizationError`).
`require_role` denies in two cases (`rbac.py`): the caller is **not a member** of the project
(the non-member raise site), or is a member whose role **ranks below** the requirement (the
rank-below raise site). `RoleDenied` is raised at the **rank-below site only**; the non-member
site keeps the base `AuthorizationError`. The MCP tool-dispatch boundary catches **`RoleDenied`
specifically** — not the `AuthorizationError` base — records a denial row, and re-raises. Because
the non-member site never raises `RoleDenied`, its denial is never caught by that handler, never
audited, and so cannot be amplified (below). Catching the base class instead would also swallow
`require_platform_role` denials and `DestructiveOpDenied` (both `AuthorizationError` subclasses,
the latter already audited by its own handler), double-writing them; the dedicated subclass is
the discriminator that confines the catch to exactly the member-over-reach per-project gate. It is
still **one** code path covering every current and future tool with no edits to the ~30 call sites
— the discriminator lives at the one `require_role` raise site that matters, not in the tools.

`RoleDenied` **carries the `project`** (alongside the `principal` and the held/required role) set
at the raise site, where `require_role` already holds `project` in hand. This is load-bearing, not
decoration: object-resolving tools (e.g. `allocations.release`, `mcp/tools/allocations.py`) resolve
the audited `project` from the **row at runtime** (`alloc.project`), not from a tool argument, so
the dispatch boundary cannot recover `project` from the call args it sees. The exception is the only
carrier — hence the denial row's `project` comes from `RoleDenied`, and `audit_log.project` stays
`NOT NULL`. This is *not* the rejected per-field exception enrichment (§Alternatives): what is
rejected is per-call-site / object-level context at the ~30 sites; the `project` the gate already
holds is not that.

The retrofit audits the **member-over-reach** case only. The non-membership case is the routine
"no grant" denial on a broadly-registered tool, and auditing it would let any authenticated token
amplify calls into unbounded `audit_log` INSERTs — the exact write-amplification ADR-0043 §4
declined to take on for platform denials; the same reasoning carries here (a non-member *probing*
objects is real signal, but recording it safely needs a rate-limited mechanism, deferred). Because
a member-over-reach denial means the project **is** in `ctx.projects`, the `project` carried on
`RoleDenied` is always resolvable — so denial rows keep a **non-null `project`** and are visible to
`audit.query`'s project-scoped form (the auditor read this milestone also ships).

Denial rows land in **`audit_log`** (*decision 2a*) via a new guard-exempt
`audit.record_denial()` writer, following `audit.record_system()`'s precedent (it already writes
`audit_log` without the membership guard). The boundary is object-agnostic — it knows the actor,
tool, and project, but not the object the handler would have resolved — so the retrofit makes
`audit_log.object_kind` / `object_id` **nullable** (`project` stays `NOT NULL`), guarded by a
CHECK keyed on `transition = 'denied'`: object columns may be NULL only on a `'denied'` row, and
every real-transition row retains the original invariant.

The `RoleDenied` retrofit must write the **reserved bare transition literal `'denied'`** — not the
`{op}:denied` convention the destructive gate uses (`security/gate.py` writes
`transition=f"{op.kind}:denied"`, e.g. `force_crash:denied`). The two denial kinds are designed to
coexist under the one CHECK: (a) the destructive-gate denial always carries an object (the
allocation it gated), so its `{op}:denied` row satisfies the CHECK's *object-present* branch with
no exemption needed; (b) the `RoleDenied` denial has no object, so it uses the bare `'denied'`
literal to take the CHECK's *transition-exempt* branch. The tool being denied is recorded in the
`tool` column, so the bare transition loses no information. Were the `RoleDenied` row written by the
`{op}:denied` convention (e.g. `allocations.request:denied`), it would carry neither an object nor
the exempt transition and the CHECK would reject it. `require_platform_role` denials stay out of
scope (audited to `platform_audit_log` in M1.1, and now excluded from the boundary catch by the
`RoleDenied` discriminator).

## Consequences

- The full `platform_operator`/`platform_admin` tool surface becomes callable; the
  separation-of-duties the role model expressed (operator ≠ data reader ≠ break-glass admin) is
  now exercised by real tools and tests.
- New migrations: a `cordoned` boolean column on `resources` (orthogonal to `status`), a
  single-row `ops_control` table (`queue_paused`), and an `audit_log` change (nullable
  `object_kind`/`object_id` + CHECK + a `reason` column; `project` stays `NOT NULL`). The
  `audit_log` change relaxes the object columns' `NOT NULL` for `'denied'` rows only — mitigated
  by the CHECK, but any consumer that assumed non-null object columns must now handle `'denied'`
  rows.
- `ops.reconcile_now` runs the **same advisory-locked `reconcile_once()` pass** as the periodic
  reconciler — it shares that pass's lock discipline (per-Project / per-Allocation / per-System
  `advisory_xact_lock`, `reconciler/loop.py`) rather than introducing a second code path, so an
  on-demand pass and a concurrent periodic pass serialize on the same locks and cannot double-act
  on one object. It triggers one extra pass; it does not stop or restart the periodic loop.
- The worker gains a pre-`dequeue` read of `queue_paused` — one extra cheap query per claim
  loop while idle. `queue_pause` freezes the **worker's claim loop only**; the reconciler's
  periodic pass keeps running and may still enqueue repair/teardown jobs. Pause is a processing
  freeze, not a control-plane freeze — stated so an operator does not mistake it for the latter;
  jobs enqueued while paused simply wait for resume.
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
- **Catch the `AuthorizationError` base at the boundary** (the obvious "one catch" form).
  Rejected: `require_platform_role` and `DestructiveOpDenied` are also `AuthorizationError`,
  so the base catch double-audits platform and destructive-gate denials. A dedicated `RoleDenied`
  subclass on the `require_role` raise site is the minimal discriminator (§5) — not per-field
  exception enrichment, and not a per-call-site change.
- **Per-call-site denial try/except** or **enriching `AuthorizationError`** with structured
  per-field, object-level context at every call site. Rejected: per-site is ~30 near-identical
  edits, easy to miss, and re-required for every new tool; per-call-site object enrichment is more
  than the central catch needs. What `RoleDenied` carries is **the type plus the `project` the
  `require_role` raise site already holds** — not per-site object context. The `project` is not
  optional decoration: the dispatch boundary cannot recover it from call args for object-resolving
  tools (which resolve `project` from the row at runtime), so it must travel on the exception (§5).
  Higher-fidelity object attribution remains a possible later refinement, not M1.3 scope.
- **Audit non-membership denials too** (audit *every* `require_role` denial). Rejected (§5): a
  broadly-registered tool's non-member denial is the routine no-grant case, and recording it
  lets any authenticated token amplify writes into `audit_log` — the amplification ADR-0043 §4
  already declined for platform denials. Member-over-reach denials carry no such risk (they
  require valid membership) and are the high-value signal; non-member probe detection is deferred
  to a rate-limited mechanism.
- **Job requeue/force-fail and worker orchestration** in queue control; **runtime `W_CPU`/`W_MEM`
  weights** in cost tuning. Rejected as over-build (decision 1): no current caller, and they
  overlap the reconciler (requeue) or belong in version-controlled config (weights).
