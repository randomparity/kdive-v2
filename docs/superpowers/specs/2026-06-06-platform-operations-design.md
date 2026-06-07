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
| `resources.drain` | `platform_operator` (`mode=passive`) / `platform_admin` (`mode=force_release`) | mutating / destructive | cordon, then `mode=passive` (default; `platform_operator`; report live allocations) or `mode=force_release` (`platform_admin` + `reason`; force-release each live allocation via the break-glass path; returns a per-allocation result list) |
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

### 1. Host schedulability + health: cordon / drain (`area:resources`)

Schedulability and health are **orthogonal axes** — a host can be cordoned *and* degraded, or
healthy *and* cordoned — so cordon is a **separate boolean column**, not a value of the health
`status` enum. Today `resources.status` is `available` / `degraded` / `offline`
(`db/schema/0001_init.sql`). Folding `cordoned` into that enum would mean a crashed cordoned host
can't read as both, and `set_status offline` would clobber the operator's cordon (and a recovered
host could strand); the k8s split (`Unschedulable` flag apart from the `Ready` condition) exists
for exactly this reason. We therefore add a boolean **`cordoned`** column to `resources`,
independent of `status`.

- **Placement becomes schedulability-aware — on both selection paths.** Host selection lives in
  `_resolve_resource` (`mcp/tools/allocations.py`), which has **two** paths and today filters on
  neither: (a) the pick-by-kind query `SELECT * FROM resources WHERE kind = %s ORDER BY created_at,
  id LIMIT 1` with **no status/cordon filter** (a single-host walking-skeleton artifact), and
  (b) an explicit `resource_id`, which returns `RESOURCES.get(conn, resource_id)` **directly** with
  no schedulability check. Cordon must close both: issue 1 adds `AND status='available' AND NOT
  cordoned` to path (a), **and** makes path (b) **reject** an explicit `resource_id` that names a
  `cordoned`/non-`available` host. If only path (a) were filtered, cordon would be trivially
  bypassed by naming the host's id — and placing new work on a cordoned host is exactly what cordon
  forbids. This *is* the mechanism by which cordon stops new placement; there is no pre-existing
  exclusion to extend. Existing allocations on a cordoned host are untouched — cordon stops *new*
  placement only.
- **`resources.cordon` / `resources.uncordon`** set/clear the `cordoned` boolean. They are
  independent of `status`: a host may be cordoned at any health, and `uncordon` clears the cordon
  without touching `status` (a host that went `offline` while cordoned returns to its prior
  schedulability when health is restored — the columns never clobber each other).
- **`resources.set_status`** is the operator setter for the health triad
  (`available` / `degraded` / `offline`). It does **not** touch `cordoned`; the two verbs stay on
  their own axes, each with its own audit transition.
- **`resources.drain`** is an *action*, not a state (decision 1a): it sets `cordoned`, then:
  - `mode=passive` (default) — returns the live allocations on the host and leaves them to
    finish/expire. The operator polls (e.g. `inventory.list` / `ops.jobs_list`) until the host
    is empty, then `resources.set_status offline` for maintenance.
  - `mode=force_release` — force-releases each live allocation via the same internal path as
    `ops.force_release`. This empties the host of **every tenant's** allocations — a cross-project
    destructive action — so it **escalates to `require_platform_role(ctx, PLATFORM_ADMIN)` plus a
    mandatory non-blank `reason`** (not `platform_operator`: filing it under operator would be a
    separation-of-duties hole, since §5 reserves cross-project force-release for `platform_admin`
    break-glass). Each release routes through the **same break-glass attribution path** as
    `ops.force_release` (the guard-exempt writer, one `platform_audit_log` row per allocation). The
    drain iterates the host's live allocations and force-releases each, returning a
    **per-allocation result list** (`released` / `failed` / `skipped`) so the outcome of each is
    observable. A per-allocation failure is reported in that list and does **not** roll back
    already-released ones — the host is simply partially drained and still `cordoned`, so the
    operator can re-invoke `drain` to finish; the action is idempotent over the remaining set.
  - A future `mode=migrate` (M2, when a second host exists) slots in here without reshaping the
    tool.

There is **no** persisted `draining` flag: "a drain is in progress" is not modeled as state; the
host is simply `cordoned` and the operator drives the rest.

### 2. On-demand reconcile (`area:ops`)

`ops.reconcile_now` runs a single `reconcile_once()` pass against a fresh connection and
returns its repair summary (counts per repair class: expired-allocation sweep, orphaned-system
teardown, abandoned-job reaping, dead-session detach, leaked-domain destroy, idempotency-key
GC, abandoned-upload reaping). It calls the **same `reconcile_once()`** as the periodic loop, so
it inherits that pass's lock discipline unchanged — each repair takes its per-Project /
per-Allocation / per-System `advisory_xact_lock` (`reconciler/loop.py`). An on-demand pass and a
concurrent periodic pass therefore serialize on the same advisory locks and cannot double-act on
one object; there is no second, lock-free code path. It does **not** stop or restart the periodic
reconciler loop — it triggers one extra pass concurrently. Gated `platform_operator`, audited to
`platform_audit_log` (a cross-project control action).

### 3. Queue pause/resume + cross-project inspect (`area:ops`)

- A single-row **`ops_control`** table holds `queue_paused boolean`. The **worker** reads it at
  the top of its claim loop and skips `dequeue` while paused (it keeps heart-beating any job
  already in flight — pause stops *new* claims, never abandons running work). DB-backed, not an
  in-process flag, because the operator's MCP call and the worker run in different processes.
- **`ops.queue_pause` / `ops.queue_resume`** set/clear the flag. Gated `platform_operator`,
  audited. **Scope of the freeze:** pause stops the *worker's* claim loop only — the reconciler's
  periodic pass keeps running and may still *enqueue* repair/teardown jobs, which simply queue up
  until resume. Pause is a processing freeze, not a control-plane freeze; an operator who needs
  the reconciler quiet as well stops the reconciler process out-of-band. This is stated so pause
  is not mistaken for a full freeze.
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
a teardown job / run the release state transition) — but **not** its audit attribution.
`audit.record()` enforces `project in ctx.projects` (a misattribution guard, `security/audit.py`
raises on a non-granted project), and a break-glass `platform_admin` is by definition not a member
of the target project, so the reused path would trip that guard. Break-glass therefore writes its
audit through a guard-exempt attribution writer (the `audit.record_system` / `record_platform`
pattern), recording the platform principal against the target's project.

This reuse is **not drop-in**, and naming the prerequisite is in-scope M1.3 work (issue 6). The
release mechanic couples the state transition with the membership-guarded `audit.record()` in one
helper — `_transition_and_audit`, called inside `_release_locked` (`mcp/tools/allocations.py`) — so
"reuse the execution" cannot avoid that `audit.record()` call as written. Issue 6 therefore
**parameterizes the audit writer** in that shared transition helper: the per-project release keeps
passing the membership-guarded `record`, and break-glass injects a guard-exempt
(`record_system` / `record_platform`-shaped) writer. **Both authorization and audit attribution
differ** from the per-project tools; only the teardown/release mechanics are shared. The
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

**Mechanism — a new `RoleDenied` exception caught at the dispatch boundary.** This milestone
**introduces** a dedicated **`RoleDenied`** subclass of `AuthorizationError` — it does not exist
today; `require_role` currently raises the bare `AuthorizationError` (`security/rbac.py`).
`require_role` has **two** raise sites: the non-member site (`project not in ctx.projects`) and the
rank-below site (held role ranks below the requirement). `RoleDenied` is raised at the
**rank-below site only**; the non-member site keeps raising the base `AuthorizationError`. The MCP
tool-dispatch wrapper catches **`RoleDenied` specifically**, records a denial row, and re-raises.
Catching the `AuthorizationError` *base* would be a bug: `require_platform_role` denials and
`DestructiveOpDenied` (`security/gate.py`) are also `AuthorizationError` subclasses, and the
destructive gate already audits its own denials — a base catch would double-write both; the
non-member denial would also be swept in. The dedicated subclass raised at the one site is the
discriminator that confines the catch to exactly the member-over-reach per-project gate. It is
still **one** code path covering every current and future tool, with no edits to the ~30 call
sites — the discriminator lives at the one rank-below raise site inside `require_role`, not in the
tools.

**`RoleDenied` carries the `project`.** Set at the raise site (where `require_role` already has
`project` in hand), alongside `principal` and the held/required role. This is load-bearing: the
dispatch boundary cannot recover `project` from the call args for **object-resolving tools**, which
resolve the audited `project` from the row at runtime, not from a tool argument —
`allocations.release` (`mcp/tools/allocations.py`) reads `alloc.project` from the fetched row and
only then calls `require_role(ctx, alloc.project, …)`. The exception is the only carrier, so the
denial row's `project` comes from `RoleDenied` and `audit_log.project` can stay `NOT NULL`. This is
*not* the rejected per-field exception enrichment: what `RoleDenied` carries is the type plus the
`project` the raise site already holds, not per-call-site object context at the ~30 sites.

**Which denials are audited — member over-reach only.** `require_role` denies in two cases
(`security/rbac.py`): the caller is **not a member** of the project, or is a member whose role
**ranks below** the requirement. The retrofit audits the **member-over-reach** case only.
Auditing the non-membership case would let any authenticated token spray calls at tools it lacks
membership for and amplify each into an unbounded `audit_log` INSERT — the exact
write-amplification ADR-0043 §4 declined for platform denials; the same reasoning applies here. A
non-member *probing* objects is real signal, but recording it safely needs a rate-limited
mechanism, deferred. The member-over-reach case carries no amplification risk (it requires valid
membership) and is the high-value signal (a real user exceeding their grant).

**Target table (decision 2a) + a resolvable project.** Denial rows land in `audit_log` via a new
guard-exempt `audit.record_denial()` writer, following the precedent of `audit.record_system()`
(which already writes `audit_log` without the `project in ctx.projects` membership guard). Because
a member-over-reach denial means the project **is** in `ctx.projects`, the `project` carried on
`RoleDenied` is always resolvable — so denial rows carry a **non-null `project`** and are visible
to `audit.query`'s project-scoped form (the auditor read this same milestone ships). The boundary
remains object-agnostic, though: it knows the actor, tool, and project (the last via the
exception), but not the object the handler would have resolved *after* the gate.

**Schema consequence.** Only `object_kind` / `object_id` need to be nullable (`project` stays
`NOT NULL`), guarded by a CHECK that keeps them `NOT NULL` for every transition except the denial
transition:

```sql
ALTER TABLE audit_log ALTER COLUMN object_kind DROP NOT NULL;
ALTER TABLE audit_log ALTER COLUMN object_id   DROP NOT NULL;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_object_present_unless_denied
  CHECK (transition = 'denied'
         OR (object_kind IS NOT NULL AND object_id IS NOT NULL));
```

Every real-transition row keeps the original invariant; only rows whose `transition` is the bare
literal `'denied'` may omit the object. The accountability goal is *the actor, the project, and the
attempt* — matching how the destructive-gate denial-audit records the attempt rather than the
(absent) object.

**The two denial kinds coexist under this one CHECK — by transition discriminator.** The
destructive gate writes its denial with `transition=f"{op.kind}:denied"` (`security/gate.py`, e.g.
`force_crash:denied`) and **always carries the object it gated** (the allocation), so its row
satisfies the CHECK's *object-present* branch with no exemption. The `RoleDenied` retrofit has no
object, so it must write the **reserved bare `'denied'` literal** to take the CHECK's
*transition-exempt* branch — the `tool` column already records which tool was denied, so the bare
transition loses no information. Were the retrofit to follow the `{op}:denied` convention (e.g.
`allocations.request:denied`), its row would carry neither an object nor the exempt transition and
the CHECK would reject it. The CHECK stays keyed on `transition = 'denied'`; the destructive gate's
existing convention is untouched.

`require_platform_role` denials are out of this retrofit's scope — they are already audited to
`platform_audit_log` (M1.1, ADR-0043 §4) for a principal holding ≥1 platform role, and the
`RoleDenied`-specific catch excludes them from the boundary.

## Schema deltas (summary)

1. **`resources`** gains a boolean **`cordoned`** column (`NOT NULL DEFAULT false`), orthogonal
   to the existing health `status` enum.
2. **`ops_control`** — a new single-row table: `queue_paused boolean NOT NULL DEFAULT false`
   (plus an `updated_at` / single-row guard).
3. **`audit_log`** — `object_kind` / `object_id` become nullable (`project` stays `NOT NULL`),
   guarded by a CHECK that requires the object columns for every non-`denied` transition; add a
   `reason text` column used by break-glass and denial rows (NULL elsewhere).

No new table for cost coefficients or host caps — both are already DB-backed
(`cost_class_coefficients`; resource `capabilities` jsonb).

## Authorization & audit summary

| Tool | Gate | Audited to |
|------|------|-----------|
| `resources.set_status` / `cordon` / `uncordon` / `drain` (`mode=passive`) | `require_platform_role(PLATFORM_OPERATOR)` | `platform_audit_log` |
| `resources.drain` (`mode=force_release`) | `require_platform_role(PLATFORM_ADMIN)` + `reason` (break-glass path) | `platform_audit_log` (per allocation) |
| `ops.reconcile_now` / `queue_pause` / `queue_resume` / `jobs_list` | `require_platform_role(PLATFORM_OPERATOR)` | `platform_audit_log` |
| `ops.set_cost_class_coeff` / `set_host_capacity` | `require_platform_role(PLATFORM_OPERATOR)` | `platform_audit_log` |
| `ops.force_teardown` / `force_release` | `require_platform_role(PLATFORM_ADMIN)` + `reason` | `platform_audit_log` (always) |
| `audit.query` (project form) | `require_role(project, admin)` | — (matches `accounting.usage`) |
| `audit.query` (cross-project) / `inventory.list` | `require_platform_role(PLATFORM_AUDITOR)` | `platform_audit_log` |
| denial-audit retrofit | n/a (`RoleDenied` catch; member over-reach only) | `audit_log` (`transition='denied'`) |

`platform_admin` satisfies `platform_auditor` (ADR-0043 §2), so an admin can call the auditor
reads; `platform_operator` satisfies neither the auditor reads nor the admin break-glass.

## Decomposition into single-PR issues

| # | Issue | area | Depends on |
|---|-------|------|-----------|
| 1 | Host `cordoned` boolean column + `resources.set_status` / `cordon` / `uncordon` + placement skips cordoned/non-available on **both** `_resolve_resource` paths (filter the pick-by-kind query **and** reject an explicit `resource_id` naming a cordoned/non-available host) | `area:resources` | — |
| 2 | `resources.drain` (`mode=passive` = `platform_operator`; `mode=force_release` = `platform_admin` + `reason`, break-glass attribution, per-allocation result list) | `area:resources` | 1, 6 |
| 3 | `ops.reconcile_now` | `area:ops` | — |
| 4 | `ops_control` table + `ops.queue_pause` / `queue_resume` + worker honors the flag + `ops.jobs_list` | `area:ops` | — |
| 5 | `ops.set_cost_class_coeff` + `ops.set_host_capacity` | `area:ops` | — |
| 6 | `ops.force_teardown` + `ops.force_release` (break-glass) **+ parameterize the audit writer in the shared `_transition_and_audit` / `_release_locked` transition helper** so break-glass injects a guard-exempt writer | `area:ops` | — |
| 7 | `audit.query` + `inventory.list` (auditor reads) | `area:audit` | — |
| 8 | Denial-audit retrofit (new `RoleDenied` exception **carrying `project`**, raised at the **rank-below site only** + `audit_log` nullable-object migration + `record_denial` writing the bare `'denied'` literal + dispatch-boundary catch, member over-reach only) | `area:security` | — |

Issue 2 depends on issue 1 (drain needs the `cordoned` state and cordon plumbing) and on issue 6
(`mode=force_release` routes through the break-glass attribution path issue 6 builds). Issues 1, 3,
4, 5, 6, 7, 8 are mutually independent → one orchestration wave of seven, then issue 2.

## Exit criteria

Falsifiable; each becomes a test.

1. **Cordon excludes a host from placement on both paths, on its own axis.** A `cordoned` host is
   skipped by the pick-by-kind placement query, **and** a request that names the cordoned host by
   explicit `resource_id` is **rejected** (cordon is not bypassable by naming the host's id); its
   existing allocations are untouched; `uncordon` restores placement on both paths. Cordon and
   `status` are independent: a host can be `cordoned` while `degraded`; `set_status offline` does
   not clear `cordoned`, and `uncordon` does not change `status` (the columns never clobber).
2. **Drain modes behave and carry the right authority.** `mode=passive` (`platform_operator`)
   returns the live allocations and leaves them running; a `platform_operator` calling
   `mode=force_release` is **denied** (it requires `platform_admin`); a `platform_admin`
   `force_release` force-releases each, the host reaches zero live allocations, and the call
   returns a per-allocation result list (released/failed/skipped) — a partial drain is observable
   and re-invokable; both modes leave the host `cordoned`.
3. **`reconcile_now` runs one pass and shares the reconciler's locks.** A pending repair (e.g. an
   orphaned system) is resolved by a single `ops.reconcile_now` call, which returns a summary; the
   periodic loop is unaffected; the on-demand pass runs the same advisory-locked `reconcile_once()`
   as the periodic loop, so it cannot double-act with a concurrent periodic pass.
4. **Queue pause stops new claims, not running work.** With `queue_paused=true`, the worker
   claims no new job but completes one already in flight; `resume` restores claiming.
5. **Tuning takes effect.** After `set_cost_class_coeff`, the next charge uses the new coeff
   (committed ledger rows are unchanged); after `set_host_capacity`, admission honors the new
   cap; lowering below the live count blocks new placement without evicting.
6. **Break-glass overrides the gate and is audited under platform attribution.**
   `ops.force_teardown` / `force_release` succeed against a cross-project object whose capability
   scope and profile opt-in would fail the three-check gate, for a `platform_admin` who is
   **not** a member of the object's project; the audit write succeeds despite the non-membership
   (it uses the guard-exempt attribution writer, not `audit.record()`); a blank `reason` is
   rejected; every call writes a `platform_audit_log` row; a `platform_operator` (non-admin)
   token is denied.
7. **Auditor reads are correct and gated.** `audit.query` project form requires `admin` on that
   project; its cross-project form and `inventory.list` require `platform_auditor` (satisfied by
   `platform_admin`), are denied to a project-only token, and write a `platform_audit_log` row
   (not an `audit_log` row).
8. **`require_role` over-reach denials are audited; non-membership and sibling denials are not.**
   A **member-over-reach** denial (member whose role ranks below the requirement) writes one
   `audit_log` row with the reserved bare `transition='denied'` recording
   principal/agent_session/tool/**project** (object NULL), the `project` taken from the
   `RoleDenied` exception; a **non-membership** denial (base `AuthorizationError`, raised at the
   other site) writes **no** row (amplification guard); a `require_platform_role` denial and a
   `DestructiveOpDenied` are **not** caught by the `RoleDenied`-specific boundary (no double-write);
   the CHECK rejects a non-`denied` row with a NULL object **but accepts a destructive-gate
   `{op}:denied` row because it carries its object**; the success path is unchanged.
