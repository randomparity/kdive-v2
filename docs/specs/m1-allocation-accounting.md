# M1 — Allocation/Accounting Depth (Integration Contract)

## Purpose

M1 makes the allocation plane **real**. M0 proved the walking-skeleton path on
local-libvirt with an "always-yes," capacity-only allocation
([0023](../adr/0023-discovery-allocation-admission.md)); M1 adds the depth the
top-level design defers to it: a **cost model + metering ledger**, **enforced
budgets/quotas** via admission control, **real reservation/lease semantics**, and
**RBAC hardening** (the operator/admin separation M0 collapsed). It also sweeps two
M0 deferrals while the planes are fresh: **System reprovision-in-place** and **live
drgn introspection** over SSH.

Like the M0 spec, this is the **integration contract** — it pins the seams (schema
delta, lifecycle changes, tool I/O shapes, admission algorithm) that every M1
sub-project plan implements against. It does not re-argue the decisions: those live
in the [ADRs](../adr/), principally [0007](../adr/0007-metering-budgets-admission.md)
(cost model + gate), [0036](../adr/0036-reservation-lease-semantics.md) (lease),
[0037](../adr/0037-rbac-hardening-role-separation.md) (RBAC),
[0038](../adr/0038-system-reprovision-in-place.md) (reprovision),
[0039](../adr/0039-ssh-transport-live-introspection.md) (SSH/live introspect), and
[0040](../adr/0040-admission-lifecycle-concurrency.md) (admission/lifecycle concurrency:
lock hierarchy, idempotency, atomic reconciliation). The provider stays local-libvirt;
**no new resource kind** — M1 is depth, not breadth.

### What M1 adds to M0

| Concern | M0 | M1 |
|---|---|---|
| Allocation | always-yes, per-**host** capacity cap only | + per-**project** quota + spend **budget**, fail-closed |
| Cost | none | size-weighted **kcu** cost model + metering **ledger** |
| Lease | `lease_expiry` column unused | window set at grant; `renew`; reconciler `→expired` sweep |
| RBAC | operator holds admin (separation unverified) | operator ≠ admin, budget/quota admin-only, negative tests |
| System | provision once, teardown | + **reprovision-in-place** (`ready → reprovisioning → ready`) |
| Debug | gdbstub + offline `introspect.from_vmcore` | + **SSH transport** + live `introspect.run` |

## Non-goals (deferred to M1.5+)

- **No new provider** — still local-libvirt only; remote/cloud/bare-metal are M2+.
  The cost model is *designed* to sum across cost_classes, but only the `"local"`
  coefficient exists in M1.
- **No fault injection** — lease-expiry-mid-job, worker death, transport drop, and
  forced secret-resolution races are stressed by the M1.5 mock provider; M1 wires the
  triggers and the happy/denied paths, M1.5 attacks them.
- **No per-operation metered surcharges** — cost is size×time; build/provision/vmcore
  surcharges are left for later (the ledger's signed `event_type` leaves room).
- **No calendar/refilling budget periods** — a project budget is a single running
  kcu cap in M1; periodic reset is a later concern.
- **No hard per-tenant sandboxing** — still designed-for, deferred
  ([0008](../adr/0008-async-worker-tier-job-queue.md)).
- **No ABAC / policy engine** — the three-role model stands ([0006](../adr/0006-oidc-rbac-attribution.md)).

## Cost model

Per [0007](../adr/0007-metering-budgets-admission.md). The normalized reference unit
is the **kcu** (kdive cost unit); cost is **size × time**:

```
rate(kcu/hr) = coeff(cost_class) × (W_CPU × vcpus + W_MEM × memory_gb)
cost(kcu)    = rate × hours
W_CPU = 1.0 kcu/(vcpu·hr)   W_MEM = 0.25 kcu/(GB·hr)   (global reference weights)
```

- **`coeff(cost_class)`** is the only per-class number — `cost_class_coefficients`
  table, seeded `"local" → 1.0`. A `cost_class` with no row fails closed
  (`configuration_error`).
- **Size** comes from the request **selector** (`vcpus`, `memory_gb`), persisted on
  the allocation (`requested_vcpus`, `requested_memory_gb`), so estimate is computable
  before a System exists and the reconciliation recomputes the same rate.
- **Inputs are validated before they reach the ledger.** Because selector size and
  window flow into **signed** `kcu_delta` arithmetic, **both** `accounting.estimate` and
  admission reject `vcpus < 1`, `memory_gb < 0`, or `window ≤ 0` (`configuration_error`),
  so `rate` and `estimate` are always `≥ 0` and no negative-size/window request can mint
  budget by writing a negative `reserved` row. The **≤ resource-caps** check (a selector
  may not exceed the target Resource's advertised capabilities, since the size is
  *billed*) is **admission-only** — only `allocations.request` has a chosen Resource;
  `accounting.estimate` prices a hypothetical size with no target host.
- **`accounting.estimate(selector, window)`** returns `rate × window_hours` with a
  breakdown; **`accounting.usage(project|investigation_id)`** rolls up `Σ kcu_delta`
  and `budget_remaining`.

## Accounting ledger

Append-only, signed deltas; **reserve-at-grant, reconcile-at-release**:

- **`reserved`** at grant: `kcu_delta = +estimate = rate × lease_window_hours`,
  written **inside the admission transaction** so the reservation counts against
  budget immediately (fail-closed under concurrency). A `renew` writes an **additional**
  `reserved` delta for the added window, so one allocation may carry several `reserved`
  rows.
- **`reconciled`** at release/expiry: `kcu_delta = actual − Σ reserved`, summed over
  **all** of the allocation's `reserved` rows (initial grant + every renewal), where
  `actual = rate × active_hours`. Reconciling against `Σ reserved` — not the initial
  estimate — is what keeps the books balanced once an allocation has been renewed
  (`actual − estimate` would leave each renewal's reservation permanently debited).
  `active_hours = 0` if released from `granted` without ever going `active` → a full
  `−Σ reserved` credit.
- **Active-interval source:** the billing interval is recorded explicitly on the
  allocation. `active_started_at` is stamped on `granted → active` (the first System
  reaches `ready`); `active_ended_at` on `active → releasing` / `active → expired`.
  `active_hours = active_ended_at − active_started_at`, computed from these columns —
  **never** reconstructed from `updated_at`, which every transition overwrites.
- **`budget_remaining = limit_kcu − spent_kcu`**, where `spent_kcu` is a **running total
  maintained on the `budgets` row**, adjusted by every `kcu_delta` in the **same
  transaction** that writes its ledger row (always under the project lock — see lock
  hierarchy). Admission and renew read it `O(1)`; they never `Σ` the full append-only
  ledger on the locked hot path (which would grow without bound as history accumulates).
  The ledger stays the audit trail and the source for `accounting.usage`'s
  `by_cost_class` breakdown. `reserved` + `reconciled` net to `actual`, so `spent_kcu` is
  committed-plus-actual exposure and the budget cannot be overcommitted.
- **Investigation rollup attributes per Allocation and never double-counts.** Cost is
  metered per Allocation. `accounting.usage(investigation_id)` sums the ledger of
  allocations whose Runs belong **solely** to that investigation; an Allocation reused
  across investigations — possible once reprovision-in-place lets one Allocation back
  sequential Systems whose Runs differ — is attributed to none individually and surfaces
  only in the project rollup as `shared_kcu`. Per-investigation sums therefore never
  double-count and never exceed the project total; in M1's expected
  one-allocation-per-investigation pattern the rollup is exact.

## Admission control (M1)

The M1 gate **composes** M0's per-host capacity check, it does not replace it
([0007](../adr/0007-metering-budgets-admission.md) decision 5). `allocations.request`
admission, under a fixed **project-before-resource** lock order to stay deadlock-free:

```
validate selector (vcpus≥1, memory_gb≥0, ≤ resource caps) + window>0  → configuration_error
resolve (principal, idempotency_key) → if already seen, return the stored result (no re-grant)
acquire LockScope.PROJECT(project)              # M1, new
  check max_concurrent_allocations (non-terminal count)   → quota_exceeded
  estimate = rate(selector) × window_hours       # always ≥ 0 (validated above)
  check (limit_kcu − spent_kcu) ≥ estimate                → allocation_denied
  acquire LockScope.RESOURCE(resource_id)        # M0, unchanged (ADR-0023)
    check per-host concurrent_allocation_cap             → allocation_denied (at_capacity)
  one transaction:
    insert Allocation (granted, lease_expiry, requested size)
    insert ledger row (reserved, +estimate); budgets.spent_kcu += estimate
    record (principal, idempotency_key) → allocation_id
    insert audit row (->granted)
```

Any failing check returns a typed failure with **no** allocation/ledger/audit row
(ADR-0023's denial rule). `quota_exceeded` = over-count; `allocation_denied` =
over-budget or over-host-cap.

**Request idempotency** (owned by [0040](../adr/0040-admission-lifecycle-concurrency.md)).
`allocations.request` and `allocations.renew` carry a client
`idempotency_key`, **scoped to the caller's `principal`** — the store's primary key is
`(principal, key)` and the resolve matches the caller, so one client's key can never
resolve another's allocation and a colliding key string across principals/projects is
harmless. Admission resolves the key **before** granting: a replay returns the original
result (same allocation, no second grant, no second `reserved` debit) — the synchronous
analogue of the M0 job `dedup_key`, which only covers async tools, and without which a
retry after a lost response would double-allocate and (reserve-at-grant) double-charge.
Three lifecycle rules: two **concurrent** same-key requests are made safe by the
`(principal, key)` PK — the loser's insert conflicts, then re-reads and returns the
winner's stored result rather than erroring; **denials are not cached** (the key is
recorded only in the success transaction), so a request denied over budget is
re-evaluated on retry (correct — the budget may have changed); and the append-only store
is **GC'd by the reconciler** past a retention window.

**Lock hierarchy** (owned by [0040](../adr/0040-admission-lifecycle-concurrency.md)).
Every path that takes more than one advisory lock acquires them in the fixed total order
`PROJECT < RESOURCE < ALLOCATION < SYSTEM`, so no two paths can deadlock — a single pairwise rule is not enough once `renew`, `provision`, `release`,
and the reconciler also take `PROJECT`. `allocations.request` takes PROJECT→RESOURCE;
`allocations.renew` takes PROJECT only; `allocations.release` takes PROJECT→ALLOCATION;
`systems.provision` takes PROJECT (system-quota check) →SYSTEM; the `→expired` sweep
takes PROJECT→ALLOCATION→SYSTEM. The M0 per-Allocation / per-System critical sections
keep their place at the tail of this order.

**Release vs. expiry — exactly one reconciliation** (owned by [0040](../adr/0040-admission-lifecycle-concurrency.md)). `allocations.release` and the
`→expired` sweep both end an allocation and write its `reconciled` credit, so they must
not both fire on one allocation. Each takes the per-**Allocation** lock and performs its
terminal transition **and** the `reconciled` write in one transaction under it: whichever
reaches the ALLOCATION lock first makes the allocation terminal (`released` or `expired`);
the other then reads a terminal state and skips (`release` on a terminal allocation →
`stale_handle`; the sweep selects only non-terminal allocations). A double credit is
therefore impossible even when a lease expires at the instant the agent releases.

**`systems.provision`** gains the second quota check under the same per-project lock:
`max_concurrent_systems` (non-terminal System count) → `quota_exceeded`. Systems are
created in the provisioning plane, so the system-concurrency cap is enforced there,
not at request time.

`quotas(project, max_concurrent_allocations, max_concurrent_systems)` and
`budgets(project, limit_kcu)` are **explicit, admin-set** rows
(`accounting.set_quota` / `accounting.set_budget`, admin-only —
[0037](../adr/0037-rbac-hardening-role-separation.md)). Fail-closed is **literal**: a
project with **no budget row is denied** (`allocation_denied`, read as `limit_kcu = 0`)
and a project with **no quota row is denied** (`quota_exceeded`) — a project cannot
allocate until an admin has set both. There is **no silent permissive default**. A
deployment that wants a project to allocate seeds its budget/quota rows explicitly
(the test harnesses and any walking-skeleton run do this in setup), so a grant is
always traceable to a deliberate limit, never to an inherited default. (Consequence:
the M0 e2e harness, which had no budgets, must seed a budget + quota for its project
once `0002` lands — folded into issue ⑨.)

## Reservation / lease (M1)

Per [0036](../adr/0036-reservation-lease-semantics.md):

- **Window at grant** — `allocations.request({…, window})`; a requested window must be
  `> 0` (else `configuration_error`) and is clamped `window = min(requested,
  KDIVE_LEASE_MAX)`, defaulting to `KDIVE_LEASE_DEFAULT` when omitted (proposed 4h
  default / 24h max); `lease_expiry = now() + window`. `renew`'s `extend` is validated
  the same way (`> 0`).
- **`allocations.renew(allocation_id, extend, idempotency_key)`** — extends
  `lease_expiry` under the per-project lock, re-checks budget for the **added** window
  only, writes an incremental `reserved` delta (and increments `spent_kcu`); over budget
  → `allocation_denied`, window unchanged. Idempotent on `idempotency_key` (a replay
  neither re-extends nor re-charges). `operator` role; non-terminal only (else
  `stale_handle`).
- **New terminal state `expired`** — reached from `granted`/`active` by the reconciler
  when `lease_expiry < now()`. Distinct from `released` (explicit) and `failed`
  (operation failure). The owning Run still becomes `failed(lease_expired)` via the
  existing compensation — allocation `expired` ≠ Run `failed`.
- **Reconciler `→expired` sweep** — selects expired non-terminal allocations and, per
  allocation, **under the per-Allocation lock**, transitions it `→expired` and writes the
  `reconciled` credit in one transaction (audited, stamping `active_ended_at`) — so it
  cannot double-reconcile against a concurrent `allocations.release` (see "Release vs.
  expiry"). It then hands the System to the **existing** M0 orphaned-System teardown. The teardown
  **honors the same in-flight-job grace window M0 already uses**: an in-flight
  provision/job is drained within the grace window and force-killed only after, so
  flipping the allocation to `expired` never bypasses the drain. The Run becomes
  `failed(lease_expired)` and the release reconciliation writes the `reconciled` ledger
  credit. Idempotent; one structured-log line per reclaim.
  - **M1 vs M1.5 scope:** M1 covers the *idle* expiry path end-to-end (an allocation
    past its window with **no** in-flight job) and the grace-window drain of a cleanly
    completing job. The adversarial lease-expiry-**mid-job** failures — worker death
    *during* the drain, a `provision` that half-applies as the lease lapses — remain the
    M1.5 fault-injection target. M1 wires the trigger and the clean path; M1.5 attacks
    the race.

## Auth, RBAC & attribution (M1)

Per [0037](../adr/0037-rbac-hardening-role-separation.md) — the M0 machinery
(`Role` rank, `require_role`, the three-check destructive gate) is unchanged; M1
makes the separation **real and tested**:

| Role | Surface |
|------|---------|
| `viewer` | read-only + `accounting.estimate`/`accounting.usage` |
| `operator` | lifecycle: `allocations.request`/`.renew`/`.release`, `systems.provision`/`.reprovision`, `control.power on`, `runs.*`, `debug.*`, `introspect.*` |
| `admin` | operator + project administration (`accounting.set_budget`/`.set_quota`) + the destructive-administration ops (`control.force_crash`, `control.power off`/`cycle`/`reset`, `systems.teardown`) |

- **Destructive gate role factor is `admin`** (force_crash, power off/cycle/reset,
  teardown). The ADR-0006 operator-with-opt-in path is **not** exercised in M1.
- **Exception — `systems.reprovision` is `operator`** ([0038](../adr/0038-system-reprovision-in-place.md)):
  it is destructive (wipes the OS) so it still passes the capability-scope and
  profile-opt-in factors, but reprovisioning your own granted System is iterating, not
  administering.
- **Reads are project-scoped — including the `investigation_id` form.**
  `accounting.usage(investigation_id)` resolves the investigation's owning `project` and
  enforces `require_project` + `require_role(viewer)` **on that project**, exactly like
  the `project` form. A `viewer` cannot read another project's spend by passing a foreign
  `investigation_id`; this is a tenant-isolation boundary, not a convenience read.
- **Test envs grant separated roles** — distinct `viewer`/`operator`/`admin`
  principals per project; every privileged M1 tool ships its **negative** test (the
  lower role is refused), including the cross-project `usage(investigation_id)` refusal.

## Postgres schema (M1 delta — migration `0002`)

Additive to the M0 schema ([0005](../adr/0005-postgres-object-store-state.md)).
Forward-only ([0015](../adr/0015-sql-migration-runner.md)).

```
-- new tables
cost_class_coefficients(cost_class PK, coeff numeric NOT NULL, updated_at)
                                                  -- seed: ('local', 1.0)
budgets(project PK, limit_kcu numeric NOT NULL,
        spent_kcu numeric NOT NULL DEFAULT 0, updated_at)  -- O(1) running total; budget_remaining = limit−spent
quotas(project PK, max_concurrent_allocations int NOT NULL,
       max_concurrent_systems int NOT NULL, updated_at)
ledger(id, ts, project, allocation_id→allocations, resource_id→resources,
       cost_class, event_type CHECK IN ('reserved','reconciled'),
       kcu_delta numeric NOT NULL, note)          -- append-only, signed (audit + by_cost_class source)
       INDEX (project), INDEX (allocation_id)
idempotency_keys(key, principal, project, kind, result jsonb, created_at,
                 PRIMARY KEY (principal, key))     -- request/renew retry-dedup, scoped per principal

-- altered columns
allocations  ADD requested_vcpus int, ADD requested_memory_gb int
allocations  ADD active_started_at timestamptz, ADD active_ended_at timestamptz
                                                  -- billing interval = active_hours source (NOT updated_at)
allocations  state CHECK += 'expired'             -- + state.py edges granted/active → expired
systems      state CHECK += 'reprovisioning'      -- + state.py edges ready ↔ reprovisioning, → failed
```

`lease_expiry` (already present, dormant in M0) becomes live. **`capability_scope` is
unchanged from M0** — the destructive-op gate's capability factor behaves exactly as in
M0 (ADR-0020); defining a richer per-allocation capability scope is **deferred** (not
made "live" in M1), so the gate's strength is not silently altered. No object-store
layout change. New advisory-lock scope `LockScope.PROJECT`.

## MCP tool surface (M1 delta)

New and changed tools (M0 tools unchanged unless noted):

```
Allocation  allocations.request({selector:{vcpus,memory_gb,…}, project, window?, idempotency_key})
              → {allocation_id, status:"granted"|"denied", reason?, estimate_kcu}        # idempotency_key: retry-dedup
            allocations.renew(allocation_id, extend, idempotency_key) → {allocation_id, lease_expiry}      # new, operator
Accounting  accounting.estimate({selector, window}) → {estimate_kcu, rate_kcu_per_hr, breakdown}   # new, viewer
            accounting.usage(project | investigation_id) → {spent_kcu, budget_remaining, by_cost_class, shared_kcu?}   # new, viewer (scoped to target project; investigation_id resolves to its project; shared_kcu = cost of allocations spanning investigations, project form only)
            accounting.set_budget(project, limit_kcu) → {project, limit_kcu}              # new, admin
            accounting.set_quota(project, max_allocations, max_systems) → {…}             # new, admin
Provision   systems.provision(...)        # + max_concurrent_systems quota check (quota_exceeded)
            systems.reprovision(system_id, provisioning_profile) → {job_id}              # new, operator, destructive; refused under a non-terminal Run (stale_handle)
Debug       debug.start_session(run_id, transport:"gdbstub"|"ssh") → {debug_session_id}   # + "ssh"
            introspect.run(session_id, helper) → {result}   # new, live drgn over ssh, redacted
```

Every tool keeps the M0 envelope ([0019](../adr/0019-tool-response-envelope.md)):
object id, `status`, `suggested_next_actions`, references — never dumps.

## Provider / plane delta (local-libvirt)

M1 stays on the typed `ProviderRuntime` seam accepted in
[0063](../adr/0063-typed-provider-runtime.md). Startup wires concrete local-libvirt ports in
`src/kdive/providers/composition.py`; MCP tools and worker handlers consume those typed ports
directly. Capability-dispatch language from [0009](../adr/0009-capability-provider-dispatch.md)
is historical ADR context, not the active M1 extension path.

| Runtime port | M1 addition |
|--------------|-------------|
| `Provisioner` | `reprovision(system_id, profile)` for `ready → reprovisioning → ready`; jobs dedup by `(system_id, "reprovision", profile_digest)` and keep the destructive-op gate |
| `Connector` | second transport kind, `open_transport(system, "ssh")`; the guest credential comes from `ssh_credential_ref` and is registered for redaction before opening the transport |
| Debug runtime | `ProviderRuntime.debug_engine` and `attach_seam` continue to serve the gdb-MI tools over a live `DebugSession` |
| Introspection ports | `LiveIntrospector.run(...)` powers `introspect.run` over SSH; `VmcoreIntrospector` remains the offline vmcore path, with the same helper set and redacted output |

## Error taxonomy (M1)

Reuse the stable `ErrorCategory`. M1 makes first real use of `quota_exceeded`
(over-count admission) and `allocation_denied` (over-budget / over-host-cap), and
emits `lease_expired` from the new `→expired` sweep. Reprovision and SSH/introspect
emit `provisioning_failure`, `transport_failure`, `stale_handle`, `transport_conflict`
as their M0 siblings do. Pick the most specific; do not invent strings.

## Reconciler (M1 delta)

Adds the **`→expired` sweep** (ADR-0036): expired non-terminal allocations → `expired`
→ existing orphaned-System teardown → existing lease-expiry compensation → ledger
reconciliation credit. Idempotent; observable via structured log. A second, minor new
pass **GCs `idempotency_keys` past a retention window** (the append-only retry-dedup
store has no other reaper). All other M0 reconciler passes (orphaned System, abandoned
job, dead DebugSession, leaked domain) are unchanged.

## Exit criteria

M1 is done when, on the local-libvirt stack, each is demonstrably true (the
falsifiable signal, as M0's six were):

1. **Budget denial + input validation + idempotency** — a request whose `estimate`
   exceeds `budget_remaining` returns `allocation_denied` with **no** allocation, ledger,
   or audit row; a request within budget grants and writes exactly one `reserved` row; a
   **malformed** request (`vcpus < 1`, `memory_gb < 0`, selector over the host's caps, or
   `window ≤ 0`) is rejected `configuration_error` with no row; and a **replayed**
   request (same `idempotency_key`) returns the original allocation with **no** second
   grant or `reserved` debit — proving neither negative inputs nor retries mint budget.
2. **Quota denial** — at `max_concurrent_allocations`, `allocations.request` returns
   `quota_exceeded`; at `max_concurrent_systems`, `systems.provision` returns
   `quota_exceeded`; both write no durable object.
3. **Ledger reconciliation** — at grant, `accounting.estimate` equals the `reserved`
   row (`rate × window`); after grant→active→release, the allocation's `reserved` +
   `reconciled` rows sum to `rate × active_hours` (the reconciled **actual**, computed
   from `active_started_at`/`active_ended_at`, not the estimate), and `accounting.usage`
   reports exactly that sum as `spent_kcu`. Estimate (reservation) and actual (usage)
   are asserted **separately** — they coincide only when a lease runs its full window.
   An Investigation whose Runs span two Allocations sums both; an Allocation shared by two
   Investigations is counted in **neither** per-investigation rollup (only the project's
   `shared_kcu`), so no kcu is double-counted.
4. **Lease expiry (idle)** — an allocation past its window **with no in-flight job** is
   moved to `expired` by the reconciler, its System torn down, its Run
   `failed(lease_expired)`, and the unused reservation credited back — distinct from an
   explicit `release`. (The expiry-**mid-job** race is an M1.5 target, not an M1 gate.)
5. **Renewal** — `allocations.renew` extends the window and writes an incremental
   `reserved` delta; a renewal over budget is denied and leaves the window unchanged.
6. **Role separation** — an `operator` is refused the `admin` ops
   (`accounting.set_budget`/`.set_quota`, `control.force_crash`,
   `control.power off`/`cycle`/`reset`, `systems.teardown`): the bare `require_role` ops
   raise `AuthorizationError`, while `force_crash`'s gate returns the `authorization_denied`
   envelope (ADR-0020 convention). An `admin` succeeds; `systems.reprovision` and
   `control.power on` succeed for `operator`; a `viewer` is refused a cross-project
   `accounting.usage(investigation_id)`.
7. **Reprovision-in-place** — `systems.reprovision` cycles `ready → reprovisioning →
   ready` on the **same** `system_id` under the **same** Allocation, with no new
   allocation and no new System row.
8. **Live introspection** — `debug.start_session(transport="ssh")` then
   `introspect.run` returns task/module/sysinfo from the **live** guest, with a planted
   secret redacted in the response and the raw transcript `sensitive`.

These eight are the precondition for the M1.5 fault-injection provider, which stresses
exactly the seams M1 wires (lease-expiry-mid-job, secret-resolution races,
admission races) before any real remote provider.
