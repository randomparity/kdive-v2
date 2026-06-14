# ADR 0007 — Metering + budgets/quotas with an admission-control gate

- **Status:** Proposed
- **Date:** 2026-06-04 (filled for M1; stub dated 2026-06-03)
- **Implements core decision:** #7 in [`../specs/top-level-design.md`](../design/top-level-design.md)
- **Refined by M1:** [`../specs/m1-allocation-accounting.md`](../design/m1-allocation-accounting.md)
- **Composes with:** [ADR-0023](0023-discovery-allocation-admission.md) (the M0 per-host
  capacity check, which M1 wraps, not replaces),
  [ADR-0036](0036-reservation-lease-semantics.md) (the lease window that bounds the
  reserved estimate)
- **Concurrency/idempotency owned by:** [ADR-0040](0040-admission-lifecycle-concurrency.md)
  (lock hierarchy, request idempotency, atomic check-then-debit, single reconciliation)

## Context

A metering ledger plus enforced budgets/quotas gates allocation via admission
control on `allocations.request`. The budget check and ledger debit must be atomic
under a per-project lock so concurrent requests cannot overspend; "always-yes"
local allocation is still capacity-admitted. This ADR also owns the **cost model**:
the normalized reference unit, the per-`cost_class` coefficients, and the
`cost_class` assignment per Resource — so an Investigation's cost rollup across
allocations and cost_classes (local VM + cloud + bare metal) is a meaningful sum.
See the spec's "Allocation", "Investigation", and "Cross-cutting concerns"
(Accounting ledger, Concurrency).

The M0 stub left this `_TBD_`; M1 ("Allocation/accounting depth") makes it real
while still single-provider. Two things must be pinned: **the cost model** (so the
ledger has a unit to record) and **the admission gate** (so budgets/quotas are
enforced, fail-closed).

## Decision

### 1. The normalized reference unit is the **kcu** (kdive cost unit), size-weighted over time

Cost is **size × time**, expressed in a dimensionless reference unit so a local-VM
Run and a future cloud Run sum meaningfully:

```
rate(kcu/hr) = coeff(cost_class) × (W_CPU × vcpus + W_MEM × memory_gb)
cost(kcu)    = rate × hours
```

- **`W_CPU` and `W_MEM` are global reference weights**, pinned here, not per-class:
  `W_CPU = 1.0` kcu per vcpu-hour, `W_MEM = 0.25` kcu per GB-hour. They define the
  *shape* of the normalized unit (one vcpu-hour ≈ four GB-hours of cost). The
  absolute values are a reference scale; what is load-bearing is that they are
  fixed and documented, so every provider's cost is expressed on the same axis.
- **`coeff(cost_class)` is the only per-class number** a future provider must
  supply — the multiplier that places a resource kind on the cost scale relative to
  the local baseline. It lives in a `cost_class_coefficients(cost_class, coeff,
  updated_at)` table, seeded by migration `0002`. Local-libvirt's class is
  `"local"` with `coeff = 1.0` (the baseline). A cloud class would land later as a
  new row (`"cloud-standard" → 4.0`, etc.) with **zero code change** — adding a
  provider adds a coefficient row, not a cost-model branch.
- **`cost_class` is assigned per Resource at discovery/registration** and persisted
  on the existing `resources.cost_class` column (ADR-0023 already populates it). The
  coefficient is resolved from the table at estimate/admission time, **from the
  persisted class**, never from request data — the same fail-closed discipline as
  the per-host cap. A `cost_class` with no coefficient row fails closed
  (`configuration_error`), never "free".

### 2. Size comes from the request **selector**, so estimate is computable before a System exists

`allocations.request({selector, project, window})` carries the desired `vcpus` and
`memory_gb` in its selector (the same fields the provisioning profile will later
fix). The rate is therefore known at request time — before any System is
provisioned — which is what makes `accounting.estimate(selector, window)` and the
reserve-at-grant debit possible. The selector size is persisted on the allocation
(`requested_vcpus`, `requested_memory_gb`) so the reconciliation step recomputes the
same rate. A provisioned System whose profile exceeds the allocation's selector size
is a `configuration_error` at `systems.provision` (you cannot silently provision
more than you were charged for).

**The selector and window are validated before they reach the ledger.** Because both
feed **signed** `kcu_delta` arithmetic, **both** `accounting.estimate` and admission
reject — as `configuration_error` — any selector with `vcpus < 1` or `memory_gb < 0` and
any `window ≤ 0`. This guarantees `rate ≥ 0` and `estimate ≥ 0`: a negative-size or
negative-window request cannot pass the budget check (`budget_remaining ≥ estimate` is
trivially true for a negative estimate) and write a **negative `reserved`** row that would
*increase* `budget_remaining` — i.e. mint budget. The **≤ resource-caps** check (a
selector may not exceed the target Resource's advertised capabilities, the size being
billed) is **admission-only**: only `allocations.request` has a chosen Resource, while
`accounting.estimate` prices a hypothetical size with no target host. Validating at
admission, not only at `systems.provision`, closes the admission-time mint-budget exploit.

### 3. Cost hits the ledger **reserve-at-grant, reconcile-at-release** (fail-closed)

The ledger is `ledger(id, ts, project, allocation_id, resource_id, cost_class,
event_type, kcu_delta, note)` — append-only, signed deltas.

- **At grant**, inside the admission transaction, write a `reserved` row with
  `kcu_delta = +estimate`, where `estimate = rate(selector) × lease_window_hours`
  ([ADR-0036](0036-reservation-lease-semantics.md) bounds the window). The
  reservation counts against budget **immediately**, so two concurrent grants cannot
  both pass a budget check before either debits — the property debit-at-release
  cannot give.
- **At release or expiry**, write a `reconciled` row with `kcu_delta = actual − Σ
  reserved`, summed over **all** of the allocation's `reserved` rows (initial grant +
  every renewal), where `actual = rate(selector) × active_hours`. Reconciling against
  `Σ reserved` rather than the single initial estimate is required for correctness: a
  renewal ([ADR-0036](0036-reservation-lease-semantics.md)) writes an **additional**
  `reserved` delta for the added window (under the same per-project lock and budget
  check), so `actual − estimate` would leave every renewal permanently over-debited.
  `active_hours = 0` if the allocation was released from `granted` without ever going
  `active` → a full `−Σ reserved` credit.
- **`active_hours` has an explicit source**, not a derived one. Migration `0002` adds
  `allocations.active_started_at` (stamped on `granted → active`, when the first System
  reaches `ready`) and `allocations.active_ended_at` (stamped on `active → releasing` /
  `active → expired`); `active_hours = active_ended_at − active_started_at`. It is
  **never** reconstructed from `updated_at`, which every transition overwrites and so
  cannot carry the billing interval.
- **`budget_remaining = limit_kcu − spent_kcu`**, where `spent_kcu` is a **running
  total maintained on the `budgets` row**, adjusted by every `kcu_delta` in the same
  transaction that writes its ledger row (always under the project lock). Admission and
  renew read it `O(1)`; they never `Σ` the full append-only ledger on the locked hot path
  — that would make the critical section cost grow with project history. The ledger
  remains the audit trail and the `by_cost_class` source for `accounting.usage`. Because
  reserved and reconciled deltas net to `actual`, `spent_kcu` is committed-plus-actual
  exposure; the budget can never be overcommitted.

`accounting.usage(project)` reports `spent_kcu`, `budget_remaining`, and the
`by_cost_class` breakdown (the latter `Σ`-d from the ledger off the hot path);
`accounting.usage(investigation_id)` rolls up the deltas of the allocations its Runs
touched — the cross-allocation, cross-cost_class Investigation sum the top-level
design calls for, in one unit.

**Attribution is per Allocation and must not double-count.** Cost is metered per
Allocation, but reprovision-in-place ([ADR-0038](0038-system-reprovision-in-place.md))
lets one Allocation back sequential Systems whose Runs may belong to **different**
Investigations. So `usage(investigation_id)` sums only allocations whose Runs are
**solely** in that investigation; an Allocation shared across investigations is
attributed to none individually and appears only in the project rollup as `shared_kcu`.
Per-investigation sums therefore never double-count and never exceed the project total;
in the expected one-allocation-per-investigation pattern the rollup is exact. (Metering
at a finer Run grain to apportion a shared allocation is deferred — it is not needed
while the common case is one allocation per investigation.)

### 4. Quotas are **two per-project concurrency caps**, enforced at the right plane

Distinct from the spend budget, two count caps bound a project's footprint
(`quotas(project, max_concurrent_allocations, max_concurrent_systems)`). Fail-closed
is **literal**, not a permissive fallback: a project with **no quota row is denied**
(`quota_exceeded`) and a project with **no budget row is denied** (`allocation_denied`,
read as `limit_kcu = 0`) — a project cannot allocate until an admin has set both
(decision 6). There is no silent configured default; a deployment that wants a project
to allocate seeds its budget/quota rows explicitly, so every grant traces to a
deliberate limit. The two caps:

- **`max_concurrent_allocations`** is checked at `allocations.request` — the
  per-*project* analogue of M0's per-*host* cap (ADR-0023), counting the project's
  non-terminal allocations under the per-project lock.
- **`max_concurrent_systems`** is checked at `systems.provision` — Systems are
  created in the provisioning plane, a later step than admission, so the natural
  enforcement point is provision time, counting the project's non-terminal Systems
  under the same per-project lock.

Both hard-deny: over the alloc cap → `quota_exceeded` at request; over the system
cap → `quota_exceeded` at provision. A denial writes no durable object (ADR-0023's
rule), only a structured-log capacity event.

### 5. The admission gate composes M0's per-host check with the per-project budget/quota check

This decision owns *what* admission checks (the budget/quota **policy**); *how* it stays
correct under concurrency and retries — the lock order, the idempotency key, the atomic
check-then-debit — is owned by **[ADR-0040](0040-admission-lifecycle-concurrency.md)** and
only referenced here.

M1 adds the per-project invariant on top of M0's per-host cap. `allocations.request`
admission, after input validation (decision 2) and idempotency resolution (ADR-0040 §3):

- checks the per-project **`max_concurrent_allocations`** quota (decision 4) → `quota_exceeded`;
- computes `estimate` and checks **`(limit_kcu − spent_kcu) ≥ estimate`** → `allocation_denied`;
- checks the M0 per-host **`concurrent_allocation_cap`** (ADR-0023, unchanged) → `allocation_denied`;
- on success, **in one transaction** (the atomic check-then-debit, ADR-0040 §2): insert the
  `granted` Allocation (`lease_expiry` per ADR-0036, `requested_vcpus`/`requested_memory_gb`),
  write the `reserved` ledger row, increment `budgets.spent_kcu`, record the idempotency
  key, write the audit row.

Any failing check returns a typed failure (`quota_exceeded` / `allocation_denied`) with
**no** allocation, ledger, or audit row — admission is the all-or-nothing point.
`allocation_denied` is the over-budget category; `quota_exceeded` is the over-count
category, so audit/SLO can tell spend from concurrency. The lock acquisition order
(`PROJECT < RESOURCE < ALLOCATION < SYSTEM`) and the deadlock/idempotency/atomicity
guarantees are [ADR-0040](0040-admission-lifecycle-concurrency.md).

### 6. Budgets and quotas are **admin-only**, set per project

Reading usage is `viewer`; requesting allocations is `operator`; **setting a
project's budget or quota is `admin`** (`accounting.set_budget`,
`accounting.set_quota`). This is the concrete project-administration surface that
[ADR-0037](0037-rbac-hardening-role-separation.md) makes `admin`-only, separating it
from the `operator` lifecycle role M0 collapsed.

**`accounting.usage` is `viewer` of the *target* project, both call forms.** The
`project` form checks `require_project` + `require_role(viewer, project)`. The
`investigation_id` form first **resolves the investigation's owning project**, then
applies the identical check on that project — a `viewer` cannot read another project's
spend by passing a foreign `investigation_id`. Without this resolution the
`investigation_id` form would be a cross-project read bypass; it is a tenant-isolation
boundary, enforced and negatively tested ([ADR-0037](0037-rbac-hardening-role-separation.md)).

## Consequences

- The cost model is one multiplication; the only per-provider knob is a coefficient
  row, so M2+ adds a cost_class without touching `core/*` (the falsifiable
  seam-holds hypothesis applies to accounting, not just dispatch).
- Reserve-at-grant makes the budget fail-closed under concurrency at the cost of
  briefly over-reserving a window that releases early — corrected by the
  reconciliation credit. This is the intended trade (never overspend > never
  over-reserve).
- The per-project lock serializes a project's admissions; cross-project admissions
  on one host still serialize on the resource lock (ADR-0023). Fixed lock ordering
  keeps the two-lock section deadlock-free.
- Migration `0002` adds `ledger`, `budgets` (with the `spent_kcu` running total),
  `quotas`, `cost_class_coefficients`, an `idempotency_keys` store (request/renew
  retry-dedup, PK `(principal, key)`), and the `allocations` columns `requested_vcpus`/`requested_memory_gb`
  (rate inputs) and `active_started_at`/`active_ended_at` (the `active_hours` billing
  interval); all additive.
- Admission reads `budget_remaining` in `O(1)` from `budgets.spent_kcu` rather than
  aggregating the append-only ledger under the lock, so the critical section does not
  grow with project history — the `Σ` is reserved for the off-hot-path `usage` report.
- `allocations.request`/`renew` are retry-safe: a replayed `idempotency_key` cannot
  double-allocate or double-charge, the synchronous counterpart to the M0 job dedup.
- `accounting.usage` gives the Investigation cost rollup the top-level design
  promised, in kcu, across allocations and cost_classes.

## Alternatives considered

- **Debit-at-release only (no reservation).** Rejected (decision 3): two concurrent
  grants can both pass a budget check before either debits, so the budget is not
  fail-closed under concurrency — the exact race the per-project lock exists to
  prevent, reintroduced through the ledger.
- **Linear time×coefficient (no size weighting).** Rejected for M1 by the size-aware
  choice: a 2-vcpu and a 64-vcpu VM on one host would cost the same, which is
  meaningless once a host runs heterogeneous Systems. Size-weighting is one extra
  term and is still summable across cost_classes.
- **Per-operation metered surcharges (build/provision/vmcore).** Deferred: most
  schema and the most ledger-emit sites, premature while single-provider. The signed
  `event_type` column leaves room to add them later without a migration.
- **Per-cost_class CPU/memory weights.** Rejected: the weights define the normalized
  unit's shape and must be global, or sums across classes stop being comparable;
  the per-class knob is the single `coeff`.
- **System-concurrency quota enforced at `allocations.request`.** Rejected: no
  System exists yet at request time, so the gate would guess; provision time is when
  a System is actually created and is the honest enforcement point (decision 4).
- **A `projects` table to hang budgets/quotas on.** Rejected (consistent with the
  M0 spec): `project` is an identity/RBAC scope, not a domain object; budgets and
  quotas are keyed by the `project` string like every other project-scoped row.
