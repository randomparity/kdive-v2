# ADR 0023 — Discovery registration & per-host allocation admission (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #14 (M0: Discovery + Allocation (admission))
- **Depends on:** [ADR-0007](0007-metering-budgets-admission.md) (admission control),
  [ADR-0009](0009-capability-provider-dispatch.md) /
  [ADR-0022](0022-capability-registry-dispatch-impl.md) (provider seam),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (RBAC/audit/gate),
  [ADR-0019](0019-tool-response-envelope.md) (response envelope),
  [ADR-0016](0016-repository-layer-locks-idempotency.md) (advisory locks),
  [ADR-0004](0004-first-slice-local-libvirt.md) (local-libvirt slice)
- **Refines:** the M0 admission and Allocation-lifecycle wording in
  [`../specs/m0-walking-skeleton.md`](../design/m0-walking-skeleton.md)

## Context

Issue #14 wires the first two planes of the walking skeleton onto the existing
domain model: **Discovery** (enumerate the local libvirt host and advertise its
capabilities) and **Allocation admission** (the always-yes, capacity-checked path
that books a host). The durable models (`Resource`, `Allocation`), their state
machines, the advisory-lock helper, the repository layer, RBAC/audit, and the
response envelope all already exist; #14 adds the discovery code, the admission
algorithm, and the `resources.*` / `allocations.*` tool surface that compose them.

Five decisions are not pinned by the parent spec or are pinned in a way that does
not hold up under scrutiny, so they are settled here.

## Decision

### 1. The capacity-admission lock is **per-host (resource-scoped)**, not per-project

The parent spec's "Concurrency" note says "a per-project lock guards the
capacity-admission check," and [ADR-0007](0007-metering-budgets-admission.md) says
"the budget check and ledger debit must be atomic under a per-project lock." Those
statements are about the **budget/spend** admission gate, whose invariant is
per-project (a project cannot overspend *its* budget). M0 enforces a different
invariant: a **per-host concurrent-Allocation cap** (ADR-0004: "capacity-admitted
against a concrete concurrent-System / resource cap on the host"). A per-project
lock does **not** serialize two different projects racing to allocate the same
host, so it cannot bound a per-host count — two projects could each pass the check
and overshoot the cap.

M0 admission therefore takes a **per-resource advisory lock**
(`LockScope.RESOURCE`, keyed by `resource_id`) for the count-and-grant critical
section. This serializes *all* admission against a given host regardless of
project, which is exactly the scope of the invariant being enforced. The
per-project budget lock returns when budgets land (M1, ADR-0007); the two locks
guard two independent invariants and do not replace each other.

### 2. The per-host cap lives in `resource.capabilities`, sourced from the environment at discovery

The cap is **configuration**, not a discovered hardware property, but it is
per-host, so it travels on the host's `resources` row in the existing
`capabilities` jsonb under the key `concurrent_allocation_cap` (an `int`).
Discovery reads it from `KDIVE_LIBVIRT_ALLOCATION_CAP` (default **1** — fail
closed: never thrash the single host) when it builds the capability set, and
registration persists it onto the row. Admission reads the cap **from the
persisted resource**, never from the environment, so the value that admitted the
first allocation is the value that admits the rest until the host is re-registered.
No schema migration is required.

### 3. Discovery is a libvirt seam with an injected connection factory

`LocalLibvirtDiscovery` takes a zero-argument `connect` callable returning a
libvirt-connection-like object; the production default calls
`libvirt.open(KDIVE_LIBVIRT_URI)`. Unit tests inject a fake connection and never
need a live host, so the discovery logic (capability assembly, arch parsing,
owned-domain enumeration) is fully covered without the `live_vm` environment. The
real `libvirt.open` path is exercised only under the gated `live_vm` marker, which
CI deselects. `import libvirt` carries a scoped `unresolved-import` suppression
(the C extension ships no stubs, per the `pyproject.toml` `[tool.ty.rules]` note).

### 4. Admission counts non-terminal allocations under the lock; a denial creates no row

Inside the per-resource lock, admission counts allocations on the host in a
**non-terminal** state (`requested`, `granted`, `active`, `releasing`) and compares
against the cap:

- **Under cap →** insert the Allocation **directly as `granted`** (the spec's
  sequence diagram: "capacity check + insert Allocation (granted)"; admission is
  synchronous, so the `requested` intermediate is not materialized) and write one
  `audit_log` row (`transition="->granted"`). Return the granted envelope.
- **At cap →** write **no** allocation row and return an `allocation_denied`
  failure envelope. A request that was never admitted has no durable object, so
  persisting a `failed` allocation for it would pollute the table, complicate the
  concurrent-count query, and invent an object the agent never owned. The denial is
  emitted to the structured log (capacity event, not a security event), not to
  `audit_log` — `audit_log` records transitions of durable objects, and a denial
  produces none.

### 5. Add a `granted → releasing` Allocation transition

The committed `AllocationState` machine only reaches `released` via
`active → releasing → released`, and `active` is produced by **provisioning a
System** (issue #15), which #14 does not implement. An agent that requests an
allocation and then abandons it (never provisions) must still be able to release it,
or capacity leaks with no path to reclaim it. M0 therefore adds the
`granted → releasing` edge: `allocations.release` drives `granted → releasing →
released` (each transition audited). The pre-existing `active → releasing` path is
unchanged and applies once provisioning lands. This refines the parent spec's
linear lifecycle drawing, which depicted only the provisioned happy path.

### 6. `ToolResponse.success` / `failure` factories; resources project capabilities as flat strings

The new tools are the first to build envelopes for non-`Job` objects, so
`ToolResponse` gains two classmethods — `success(object_id, status, …)` and
`failure(object_id, category, …)` — that centralize the "category iff failure"
discipline. Because `ToolResponse.data` is `dict[str, str]` (ADR-0019), a resource's
nested `capabilities` jsonb is surfaced to the agent as a **flat string projection**
(`kind`, `arch`, `vcpus`, `memory_mb`, `transports`, `concurrent_allocation_cap`;
`resources.describe` adds `pool`, `cost_class`, `host_uri`). The canonical
structured `capabilities` remain on the `resources` row; the envelope shows a lossy
but agent-sufficient view. Widening `data` to a nested object is a cross-cutting
ADR-0019 change and is deliberately **not** taken here.

## Consequences

- Admission correctly bounds the per-host cap even if a future deployment runs
  multiple projects against one host, at the cost of serializing cross-project
  admission on that host (acceptable: admission is a fast count-and-insert).
- The cap is visible on the resource row and via `resources.describe`, and changing
  it is a re-registration, not a code change.
- Discovery and admission are unit-tested without libvirt or a live host; only the
  thin `libvirt.open` adapter is `live_vm`-gated.
- `LockScope` gains a `RESOURCE` member and `AllocationState` gains one edge, both
  with matching test-table updates; these are additive and bisectable.
- A denied request leaves no trace in Postgres beyond a log line; if M1 needs
  denial analytics it will add them deliberately rather than inheriting an
  accidental `failed`-row trail.
- Registration is an explicit, idempotent function (the discovery→Postgres bridge),
  **not** a server-startup side effect — the server still boots and serves `jobs.*`
  without a reachable libvirt host. Wiring registration into an operator command is
  left to a later issue.

## Alternatives considered

- **Per-project admission lock (the issue's literal wording).** Rejected: it cannot
  enforce a per-host cap across concurrent projects (decision 1). It is the right
  lock for the M1 per-project *budget* gate, which is a separate invariant.
- **A dedicated `max_concurrent_allocations` column on `resources`.** Rejected for
  M0: heavier (a migration plus a `CHECK`/default) than the existing `capabilities`
  jsonb, which already exists to carry per-host advertised properties.
- **Read the cap from the environment at request time.** Rejected: the cap would
  not be visible on the resource row or via `resources.describe`, and a mid-run env
  change could silently move the cap under live allocations.
- **Persist a denied request as a `failed` allocation row.** Rejected: it invents a
  durable object for a request that was never admitted, pollutes `allocations`, and
  forces the concurrent-count query to filter it out — all to obtain a denial trail
  that the structured log already provides.
- **Drive `granted → active → releasing → released` on release (no new edge).**
  Rejected: it would mark an allocation `active` (System provisioned) when no System
  exists — a false state — purely to satisfy the guard table. A
  direct `granted → released` edge was also rejected: it skips `releasing`, the
  state that models teardown-in-progress once a System exists.
- **Widen `ToolResponse.data` to a nested object.** Rejected here: it is a
  cross-cutting change to the ADR-0019 envelope shared by every plane and `from_job`;
  the flat string projection meets the M0 need without it.
- **Register the host as a server-startup side effect.** Rejected: it would make the
  MCP server require a reachable libvirt host to boot, breaking `jobs.*`-only
  deployments and the test harness.
