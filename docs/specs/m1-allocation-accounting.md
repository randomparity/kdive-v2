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
[0038](../adr/0038-system-reprovision-in-place.md) (reprovision), and
[0039](../adr/0039-ssh-transport-live-introspection.md) (SSH/live introspect). The
provider stays local-libvirt; **no new resource kind** — M1 is depth, not breadth.

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
- **`accounting.estimate(selector, window)`** returns `rate × window_hours` with a
  breakdown; **`accounting.usage(project|investigation_id)`** rolls up `Σ kcu_delta`
  and `budget_remaining`.

## Accounting ledger

Append-only, signed deltas; **reserve-at-grant, reconcile-at-release**:

- **`reserved`** at grant: `kcu_delta = +estimate = rate × lease_window_hours`,
  written **inside the admission transaction** so the reservation counts against
  budget immediately (fail-closed under concurrency).
- **`reconciled`** at release/expiry: `kcu_delta = actual − estimate`, where
  `actual = rate × active_hours` (`active_hours` = time spent `active`; 0 if released
  from `granted` → full `−estimate` credit). A `renew` writes an incremental
  `reserved` delta for the added window.
- **`budget_remaining = limit_kcu − Σ kcu_delta`**; reserved+reconciled net to actual,
  so the running sum is committed-plus-actual exposure and the budget cannot be
  overcommitted.

## Admission control (M1)

The M1 gate **composes** M0's per-host capacity check, it does not replace it
([0007](../adr/0007-metering-budgets-admission.md) decision 5). `allocations.request`
admission, under a fixed **project-before-resource** lock order to stay deadlock-free:

```
acquire LockScope.PROJECT(project)              # M1, new
  check max_concurrent_allocations (non-terminal count)   → quota_exceeded
  estimate = rate(selector) × window_hours
  check budget_remaining ≥ estimate                       → allocation_denied
  acquire LockScope.RESOURCE(resource_id)        # M0, unchanged (ADR-0023)
    check per-host concurrent_allocation_cap             → allocation_denied (at_capacity)
  one transaction:
    insert Allocation (granted, lease_expiry, requested size)
    insert ledger row (reserved, +estimate)
    insert audit row (->granted)
```

Any failing check returns a typed failure with **no** allocation/ledger/audit row
(ADR-0023's denial rule). `quota_exceeded` = over-count; `allocation_denied` =
over-budget or over-host-cap.

**`systems.provision`** gains the second quota check under the same per-project lock:
`max_concurrent_systems` (non-terminal System count) → `quota_exceeded`. Systems are
created in the provisioning plane, so the system-concurrency cap is enforced there,
not at request time.

`quotas(project, max_concurrent_allocations, max_concurrent_systems)` and
`budgets(project, limit_kcu)` are seeded by `0002`; a project with no row fails closed
to a conservative configured default. Both are set **admin-only** via
`accounting.set_quota` / `accounting.set_budget` ([0037](../adr/0037-rbac-hardening-role-separation.md)).

## Reservation / lease (M1)

Per [0036](../adr/0036-reservation-lease-semantics.md):

- **Window at grant** — `allocations.request({…, window})`; clamped
  `window = min(requested, KDIVE_LEASE_MAX)`, default `KDIVE_LEASE_DEFAULT`
  (proposed 4h default / 24h max); `lease_expiry = now() + window`.
- **`allocations.renew(allocation_id, extend)`** — extends `lease_expiry` under the
  per-project lock, re-checks budget for the **added** window only, writes an
  incremental `reserved` delta; over budget → `allocation_denied`, window unchanged.
  `operator` role; non-terminal only (else `stale_handle`).
- **New terminal state `expired`** — reached from `granted`/`active` by the reconciler
  when `lease_expiry < now()`. Distinct from `released` (explicit) and `failed`
  (operation failure). The owning Run still becomes `failed(lease_expired)` via the
  existing compensation — allocation `expired` ≠ Run `failed`.
- **Reconciler `→expired` sweep** — selects expired non-terminal allocations,
  transitions them `→expired` (audited), hands the System to the **existing** M0
  orphaned-System teardown (drain → force-kill → Run `failed(lease_expired)`), and the
  release reconciliation writes the `reconciled` ledger credit. Idempotent; one
  structured-log line per reclaim.

## Auth, RBAC & attribution (M1)

Per [0037](../adr/0037-rbac-hardening-role-separation.md) — the M0 machinery
(`Role` rank, `require_role`, the three-check destructive gate) is unchanged; M1
makes the separation **real and tested**:

| Role | Surface |
|------|---------|
| `viewer` | read-only + `accounting.estimate`/`accounting.usage` |
| `operator` | lifecycle: `allocations.request`/`.renew`/`.release`, `systems.provision`/`.reprovision`/`.teardown`, `runs.*`, `debug.*`, `introspect.*` |
| `admin` | operator + project administration (`accounting.set_budget`/`.set_quota`) + the destructive-gate role factor |

- **Destructive gate role factor is `admin`** (force_crash, power off/cycle/reset,
  teardown). The ADR-0006 operator-with-opt-in path is **not** exercised in M1.
- **Exception — `systems.reprovision` is `operator`** ([0038](../adr/0038-system-reprovision-in-place.md)):
  it is destructive (wipes the OS) so it still passes the capability-scope and
  profile-opt-in factors, but reprovisioning your own granted System is iterating, not
  administering.
- **Test envs grant separated roles** — distinct `viewer`/`operator`/`admin`
  principals per project; every privileged M1 tool ships its **negative** test (the
  lower role is refused).

## Postgres schema (M1 delta — migration `0002`)

Additive to the M0 schema ([0005](../adr/0005-postgres-object-store-state.md)).
Forward-only ([0015](../adr/0015-sql-migration-runner.md)).

```
-- new tables
cost_class_coefficients(cost_class PK, coeff numeric NOT NULL, updated_at)
                                                  -- seed: ('local', 1.0)
budgets(project PK, limit_kcu numeric NOT NULL, updated_at)
quotas(project PK, max_concurrent_allocations int NOT NULL,
       max_concurrent_systems int NOT NULL, updated_at)
ledger(id, ts, project, allocation_id→allocations, resource_id→resources,
       cost_class, event_type CHECK IN ('reserved','reconciled'),
       kcu_delta numeric NOT NULL, note)          -- append-only, signed
       INDEX (project), INDEX (allocation_id)

-- altered columns
allocations  ADD requested_vcpus int, ADD requested_memory_gb int
allocations  state CHECK += 'expired'             -- + state.py edges granted/active → expired
systems      state CHECK += 'reprovisioning'      -- + state.py edges ready ↔ reprovisioning, → failed
```

`lease_expiry` and `capability_scope` (already present, dormant in M0) become live.
No object-store layout change. New advisory-lock scope `LockScope.PROJECT`.

## MCP tool surface (M1 delta)

New and changed tools (M0 tools unchanged unless noted):

```
Allocation  allocations.request({selector:{vcpus,memory_gb,…}, project, window?})
              → {allocation_id, status:"granted"|"denied", reason?, estimate_kcu}
            allocations.renew(allocation_id, extend) → {allocation_id, lease_expiry}      # new, operator
Accounting  accounting.estimate({selector, window}) → {estimate_kcu, rate_kcu_per_hr, breakdown}   # new, viewer
            accounting.usage(project | investigation_id) → {spent_kcu, budget_remaining, by_cost_class}   # new, viewer
            accounting.set_budget(project, limit_kcu) → {project, limit_kcu}              # new, admin
            accounting.set_quota(project, max_allocations, max_systems) → {…}             # new, admin
Provision   systems.provision(...)        # + max_concurrent_systems quota check (quota_exceeded)
            systems.reprovision(system_id, provisioning_profile) → {job_id}              # new, operator, destructive
Debug       debug.start_session(run_id, transport:"gdbstub"|"ssh") → {debug_session_id}   # + "ssh"
            introspect.run(session_id, helper) → {result}   # new, live drgn over ssh, redacted
```

Every tool keeps the M0 envelope ([0019](../adr/0019-tool-response-envelope.md)):
object id, `status`, `suggested_next_actions`, references — never dumps.

## Provider / plane delta (local-libvirt)

| Plane | M1 addition |
|-------|-------------|
| Provisioning | `reprovision` op (`ready → reprovisioning → ready`); `dedup_key=(system_id,"reprovision",profile_digest)`; destructive/best-effort contract |
| Connect | second transport `kind="ssh"` (port v1 SSH backend); credential by `ssh_credential_ref` resolved + registry-registered at the worker boundary |
| Debug | live `introspect.run` — drgn over SSH, same helper set as offline, output redacted |

The `ProvisioningPlane`/`ConnectPlane`/`DebugPlane` `Protocol`s are **unchanged**;
M1 additions are new capabilities + backends behind them — a concrete test of the
"new transport/op = provider change only" seam ([0009](../adr/0009-capability-provider-dispatch.md)).

## Error taxonomy (M1)

Reuse the stable `ErrorCategory`. M1 makes first real use of `quota_exceeded`
(over-count admission) and `allocation_denied` (over-budget / over-host-cap), and
emits `lease_expired` from the new `→expired` sweep. Reprovision and SSH/introspect
emit `provisioning_failure`, `transport_failure`, `stale_handle`, `transport_conflict`
as their M0 siblings do. Pick the most specific; do not invent strings.

## Reconciler (M1 delta)

Adds the **`→expired` sweep** (ADR-0036): expired non-terminal allocations → `expired`
→ existing orphaned-System teardown → existing lease-expiry compensation → ledger
reconciliation credit. Idempotent; observable via structured log. All other M0
reconciler passes (orphaned System, abandoned job, dead DebugSession, leaked domain)
are unchanged.

## Exit criteria

M1 is done when, on the local-libvirt stack, each is demonstrably true (the
falsifiable signal, as M0's six were):

1. **Budget denial** — a request whose `estimate` exceeds `budget_remaining` returns
   `allocation_denied` with **no** allocation, ledger, or audit row; a request within
   budget grants and writes exactly one `reserved` ledger row.
2. **Quota denial** — at `max_concurrent_allocations`, `allocations.request` returns
   `quota_exceeded`; at `max_concurrent_systems`, `systems.provision` returns
   `quota_exceeded`; both write no durable object.
3. **Ledger reconciliation** — after grant→active→release, the ledger holds a
   `reserved` and a `reconciled` row whose sum equals `rate × active_hours`, and
   `accounting.usage` reflects it within tolerance of `accounting.estimate`.
4. **Lease expiry** — an allocation past its window is moved to `expired` by the
   reconciler, its System is torn down, its Run is `failed(lease_expired)`, and the
   reservation is credited back — distinct from an explicit `release`.
5. **Renewal** — `allocations.renew` extends the window and writes an incremental
   `reserved` delta; a renewal over budget is denied and leaves the window unchanged.
6. **Role separation** — an `operator` is refused `accounting.set_budget`/`.set_quota`
   and `force_crash` (`authorization_error`); an `admin` succeeds; `systems.reprovision`
   succeeds for `operator`.
7. **Reprovision-in-place** — `systems.reprovision` cycles `ready → reprovisioning →
   ready` on the **same** `system_id` under the **same** Allocation, with no new
   allocation and no new System row.
8. **Live introspection** — `debug.start_session(transport="ssh")` then
   `introspect.run` returns task/module/sysinfo from the **live** guest, with a planted
   secret redacted in the response and the raw transcript `sensitive`.

These eight are the precondition for the M1.5 fault-injection provider, which stresses
exactly the seams M1 wires (lease-expiry-mid-job, secret-resolution races,
admission races) before any real remote provider.
