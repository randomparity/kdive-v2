# ADR 0022 — Capability registry & dispatch implementation shapes (M0)

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-03
- **Deciders:** kdive maintainers
- **Refines:** [ADR-0009](0009-capability-provider-dispatch.md) (capability-based
  provider dispatch)

## Context

[ADR-0009](0009-capability-provider-dispatch.md) decided *that* the core dispatches
to providers by matching a requested `(plane, operation, resource_kind)` against
advertised capabilities — never by provider name — and *that* a multi-match is
resolved deterministically by "an explicit pin → health → `cost_class` → a stable
tiebreak". The [m0 spec](../specs/m0-walking-skeleton.md) "Plane interfaces" sketches
`OpContract`, `Capability`, and the eight provider-plane `Protocol`s as `TypedDict`
/ `Protocol` *illustrations*. Issue #13 owns the concrete seam:
`src/kdive/providers/capability.py`, `src/kdive/providers/interfaces.py`, and
`tests/providers/` — the registry, the dispatcher, and the typed plane contracts,
tested against **fake** providers. The local-libvirt provider that honors these
Protocols is a later issue (#15); the `AllocationPlane` is core, not a provider
Protocol (spec).

Four implementation shapes the spec leaves open bound this issue:

1. **Representation.** A `Capability` is a registry *key component* and must be
   hashable and comparable; an `OpContract` is metadata that must reject malformed
   input (an unknown `cleanup` value is a programming error, not a silent default).
   `TypedDict` — the spec's illustration form — is neither hashable nor validated.
2. **Deterministic ordering, concretely.** ADR-0009 names the policy
   (health → `cost_class` → stable tiebreak) but not its realization. `health` and
   `cost_class` are per-*resource* in the spec's schema (`resources.cost_class`,
   the Discovery plane's `health`), yet dispatch is keyed by `resource_kind`, so the
   selection metadata must be captured *per registration*. `cost_class` is a free
   `str` (`resources.cost_class` is unconstrained) with no intrinsic order.
3. **Advertised-but-unhonored.** ADR-0009 requires that a capability a provider
   advertises but cannot honor fails with a typed `not_implemented` "at dispatch, not
   silently". The mechanism — when and how the unhonored claim is detected — is
   unstated.
4. **What dispatch returns.** ADR-0009 says the contract flags "drive job routing,
   the destructive-op gate, and the reconciler". A bare bound method drops the
   contract; callers would have to re-derive it.

## Decision

**Representation.** `OpContract`, `Capability`, and the dispatch result `BoundOp` are
**frozen, slotted dataclasses** (`@dataclass(frozen=True, slots=True)`) — immutable,
hashable, usable as / within registry keys, and `__post_init__`-validated. `Plane`
and `CleanupGuarantee` are `StrEnum`s (closed sets — the eight planes, the three
cleanup guarantees), matching the repository's lifecycle-enum convention
(`ResourceKind`, `JobKind`). `operation` stays a `str` (open per plane);
`resource_kind` reuses the existing `domain.models.ResourceKind` enum. We do **not**
use Pydantic here: these are in-memory value types with no persistence/serialization
need, and a frozen dataclass is the lighter hashable carrier.

**Registry binding.** `register(provider, capabilities, *, provider_id, health,
cost_class)` records, per advertised `Capability`, a candidate registration bundling
the provider object and its selection metadata. `health` reuses
`domain.state.ResourceStatus` (`available`/`degraded`/`offline` — already an ordered
health notion). `provider_id` is the stable tiebreak and must be unique across
registrations. Capabilities are keyed by `(plane, operation, resource_kind)` to a
list of candidates.

**Deterministic ordering.** `dispatch(plane, operation, resource_kind, *, pin=None)
-> BoundOp`. Candidate selection:

1. if `pin` (a `provider_id`) is given, the candidate with that `provider_id` wins
   outright; if none matches the pin, dispatch raises `not_implemented` (a pin to a
   provider that does not advertise the op is a denied request, not a fall-through);
2. else order candidates by `health` (`available` ≺ `degraded` ≺ `offline`),
3. then by `cost_class` **ascending lexicographically**,
4. then by `provider_id` ascending (stable tiebreak),

and bind the first. The lexicographic `cost_class` order is a **documented
placeholder**: `cost_class` is an unstructured `str` in M0, so any total order is
arbitrary; a structured cost rank arrives when M1+ gives `cost_class` meaning. The
order is total and deterministic regardless, which is what the acceptance test pins.

**Advertised-but-unhonored.** A capability is *honored* iff the provider exposes a
callable attribute named for its `operation`. This is checked **twice**: at
`register` (fail fast — a provider that advertises an op it has no method for is a
construction error) and again at `dispatch` (defence in depth) — both raise
`CategorizedError(ErrorCategory.NOT_IMPLEMENTED)`. The deeper ADR-0009 check
(advertised claims reconciled against a `list_owned`/`reconcile` surface) is M2; M0
checks method presence only.

**Dispatch result.** `dispatch` returns a `BoundOp` frozen dataclass carrying
`(provider_id, operation, contract, call)` where `call` is the bound provider method.
Callers read `contract` for job routing (`long_running`), the destructive-op gate
(`destructive`), and cancel/reconcile (`cancelable`, `cleanup`) without re-deriving
it.

## Consequences

- The registry is a pure in-memory structure with no I/O; its tests are fast and
  need neither Postgres nor libvirt (unlike most M0 layers).
- Selection metadata (`health`, `cost_class`, `provider_id`) is passed to `register`
  explicitly rather than read off a `Resource` row. In M0 the caller supplies it; a
  later issue may wire it from the Discovery plane / `resources` table without
  changing the registry contract.
- `health` is a registration-time snapshot. M0 does not re-poll health on dispatch;
  a provider whose health changes re-registers (or a later issue adds a refresh
  path). Recorded as a limitation, not a silent gap.
- The lexicographic `cost_class` order will reorder if M1 introduces a cost rank;
  any test that pins a `cost_class` winner is coupled to the placeholder order and
  must be revisited then. Flagged here so that coupling is intentional.
- `operation` being a free `str` means a typo in an advertised `operation` yields a
  `not_implemented` at dispatch rather than a type error. Acceptable for M0 (the
  honored-method check catches the common case — an advertised op with no method);
  tightening to a per-plane operation enum is deferred (no current need).

## Alternatives considered

- **`TypedDict` per the spec illustration.** Rejected: not hashable (cannot key the
  registry) and unvalidated (a malformed `cleanup` string passes silently). The spec
  block is an interface sketch, not a representation mandate.
- **Pydantic models for `Capability`/`OpContract`.** Rejected: these are in-memory
  value types with no serialization or DB-round-trip need; Pydantic's machinery buys
  nothing here over a frozen dataclass, which is hashable out of the box and lighter.
  (Domain *durable objects* remain Pydantic — they persist; capabilities do not.)
- **Read `health`/`cost_class` from a `Resource` row at dispatch.** Rejected for M0:
  couples the registry to the repository layer and to a resource lookup on the hot
  path; the metadata is stable enough to capture at registration, and the provider
  that owns a resource is the authority on its cost class anyway.
- **Order `cost_class` by a hardcoded rank table.** Rejected: invents structure
  (`cheap`/`standard`/`premium`) the M0 schema does not define; a wrong guess is
  worse than an explicit, documented placeholder order.
- **Return a bare bound method from `dispatch`.** Rejected: drops the `OpContract` the
  caller needs for job routing, the gate, and the reconciler — every caller would
  re-dispatch or re-look-up the contract.
- **Resolve a multi-match by registration order.** Rejected by ADR-0009 explicitly:
  order of registration is not a selection policy; selection must be a stated, stable
  function of provider properties.
