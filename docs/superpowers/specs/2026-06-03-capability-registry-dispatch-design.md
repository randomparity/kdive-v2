# Capability Registry, Dispatch & Plane Interfaces (M0) — Design

**Issue:** #13 (M0) · **Depends on:** #5 (domain models & error taxonomy —
merged), #7 (repository layer — merged) · **Decisions:**
[ADR-0022](../../adr/0022-capability-registry-dispatch-impl.md), refining
[ADR-0009](../../adr/0009-capability-provider-dispatch.md) · **Parent spec:**
[`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("Plane interfaces", "Provider / capability model")

## Goal

The provider extension seam: the typed plane `Protocol`s every provider implements
against, the value types that describe an advertised operation and its contract, and
the in-memory registry that dispatches a requested operation to a provider **by
capability match, never by name** ([ADR-0009](../../adr/0009-capability-provider-dispatch.md)).
Two new modules under `src/kdive/providers/`:

- `interfaces.py` — the **eight** provider-plane `Protocol`s (Discovery,
  Provisioning, Build, Install, Connect, Debug, Control, Retrieve) and the handle /
  value aliases their signatures reference. The ninth plane, **Allocation**, is the
  always-yes capacity-checked core path (spec) and is **not** a provider Protocol.
- `capability.py` — `Plane` and `CleanupGuarantee` enums, the `OpContract` and
  `Capability` value types, the `BoundOp` dispatch result, and `CapabilityRegistry`
  (`register` + `dispatch`).

This is the **seam only**, exercised against fake providers. The local-libvirt
provider that honors these Protocols is issue #15; no `Protocol` is implemented here,
no libvirt or Postgres is touched. The registry is pure in-memory Python — its tests
are the fastest in the M0 suite.

## Non-goals

- **No provider implementation.** No plane `Protocol` is implemented in `src/` here;
  conformance is proven against fakes in `tests/providers/`. The local-libvirt
  realization is #15.
- **No `AllocationPlane` Protocol.** Allocation is core, not a provider plane (spec,
  ADR-0009). It does not appear in `interfaces.py`.
- **No health re-polling.** `health` is a registration-time snapshot
  (ADR-0022); dispatch does not re-query a provider's health. A changed health
  re-registers.
- **No structured `cost_class` order.** M0 orders `cost_class` lexicographically as a
  documented placeholder (ADR-0022); a cost rank is an M1+ concern.
- **No reconcile-surface honesty check.** Advertised-but-unhonored is detected by
  method presence only; the deeper `list_owned`/`reconcile` cross-check (ADR-0009) is
  M2.
- **No wiring into the MCP tool layer.** Handlers consume `dispatch` in later issues;
  this issue lands the registry and its contract, not a caller.
- **No persistence.** The registry is rebuilt in-process; the `resources.capabilities`
  jsonb column (its serialized form) is owned by a later issue.

## Components

### `capability.py`

```python
class Plane(StrEnum):
    DISCOVERY = "discovery"
    PROVISIONING = "provisioning"
    BUILD = "build"
    INSTALL = "install"
    CONNECT = "connect"
    DEBUG = "debug"
    CONTROL = "control"
    RETRIEVE = "retrieve"

class CleanupGuarantee(StrEnum):
    CLEAN_ROLLBACK = "clean-rollback"
    BEST_EFFORT = "best-effort"
    ORPHAN_FLAGGED = "orphan-flagged"

@dataclass(frozen=True, slots=True)
class OpContract:
    idempotent: bool
    destructive: bool
    cancelable: bool
    long_running: bool          # True → routed as a job
    cleanup: CleanupGuarantee

@dataclass(frozen=True, slots=True)
class Capability:
    plane: Plane
    operation: str
    resource_kind: ResourceKind
    contract: OpContract

@dataclass(frozen=True, slots=True)
class BoundOp:
    provider_id: str
    operation: str
    contract: OpContract
    call: Callable[..., object]   # the bound provider method
```

`CapabilityRegistry`:

- `register(provider, capabilities, *, provider_id, health, cost_class)` — for each
  `Capability`, append a candidate `(provider, provider_id, health, cost_class,
  capability)` under the key `(plane, operation, resource_kind)`. Validates:
  - `provider_id` is non-empty and **unique** across all prior registrations
    (`ValueError` — a programming error, not an `ErrorCategory`);
  - for every capability, `getattr(provider, capability.operation)` is callable —
    else `CategorizedError(NOT_IMPLEMENTED)` (advertised-but-unhonored, caught early).
- `dispatch(plane, operation, resource_kind, *, pin=None) -> BoundOp` — look up the
  candidate list:
  - empty / missing key → `CategorizedError(NOT_IMPLEMENTED)`;
  - if `pin` given: select the candidate whose `provider_id == pin`; none →
    `CategorizedError(NOT_IMPLEMENTED)` (a pin to a non-advertising provider is denied,
    not a fall-through);
  - else order candidates by the key `(health_rank, cost_class, provider_id)` and take
    the first, where `health_rank` maps `available→0, degraded→1, offline→2`;
  - re-check the honored method (defence in depth) → `not_implemented` if gone;
  - return `BoundOp(provider_id, operation, capability.contract, bound_method)`.

`health` is typed `ResourceStatus`; `cost_class` is `str`; `pin` is `str | None`.

### `interfaces.py`

The eight `Protocol`s, signatures per the spec "Plane interfaces" block, plus the
handle / value aliases they reference. M0 keeps the cross-plane handle types as thin
aliases (`NewType`/`TypeAlias`) so the Protocols are importable and structurally
checkable without pulling in provider internals; the concrete handle classes land
with the provider (#15). Aliases:

| Alias | M0 form | Owned by |
|-------|---------|----------|
| `SystemHandle`, `TransportHandle` | opaque `NewType(str)` | provider #15 |
| `KernelArtifact`, `ArtifactRef` | `NewType(str)` (object-store ref) | #11 / store |
| `BreakLocation`, `Registers`, `PowerAction`, `BreakpointId` | minimal `TypeAlias` | debug/control #15+ |
| `ResourceRecord`, `OwnedInfra` | `TypeAlias` to a `TypedDict` shape | discovery / reconciler |
| `Allocation`, `Run` | `domain.models` (existing) | #5 |
| `ProvisioningProfile`, `BuildProfile` | `domain.models` jsonb dicts (existing) | #5 / #11 |

The Protocols (Discovery, Provisioning, Build, Install, Connect, Debug, Control,
Retrieve) carry exactly the methods the spec lists. The `read_memory` `length ≤ 4096`
cap is a documented invariant on the `DebugPlane` docstring (enforced by the provider,
#15 — not the Protocol).

## Dispatch ordering (worked)

For candidates advertising the same `(plane, operation, resource_kind)`:

| Step | Rule | Loser eliminated when |
|------|------|-----------------------|
| 1 | explicit `pin` wins outright | a `provider_id` matches the pin |
| 2 | `available` ≺ `degraded` ≺ `offline` | one is healthier |
| 3 | `cost_class` ascending (lexicographic, placeholder) | one sorts earlier |
| 4 | `provider_id` ascending | stable final tiebreak |

The order is **total** — two registrations cannot share a `provider_id` (enforced at
`register`), so step 4 always resolves. The acceptance test asserts the winner at each
level.

## Error handling

Every failure is a typed `CategorizedError` or a `ValueError`, never a silent default:

| Condition | Result |
|-----------|--------|
| dispatch for an unregistered `(plane, op, kind)` | `CategorizedError(NOT_IMPLEMENTED)` |
| `pin` names a provider that does not advertise the op | `CategorizedError(NOT_IMPLEMENTED)` |
| advertised capability whose `operation` is not a provider method | `CategorizedError(NOT_IMPLEMENTED)` — at `register` and at `dispatch` |
| duplicate `provider_id` at `register` | `ValueError` (construction bug) |
| empty `provider_id` at `register` | `ValueError` |

`CategorizedError.details` carries the lookup key (`plane`, `operation`,
`resource_kind`, and `pin` when set) so a handler can populate a failure response and
`suggested_next_actions` without re-deriving context. The key is provider-named only
when a pin was supplied; it never embeds guest output or secrets (none flow through
the registry).

## Testing

Behavior, edges, and every error path — no DB, no libvirt. Fakes live in
`tests/providers/conftest.py`: a `FakeProvider` exposing the plane methods named by
the operations it advertises, plus a `partial` variant that advertises an op it has no
method for.

`tests/providers/test_capability.py`:

- **dispatch by capability** — a registered fake is selected for its advertised
  `(plane, op, kind)`; `BoundOp.call(...)` invokes the fake's method (acceptance #1).
- **unregistered → not_implemented** — dispatch for an unadvertised op raises
  `CategorizedError(NOT_IMPLEMENTED)`; `details` carries the key (acceptance #2).
- **deterministic multi-match** — two providers advertise the same key; assert the
  winner for each tiebreak in isolation: pin overrides a healthier/cheaper rival;
  health beats cost_class; cost_class beats provider_id; provider_id breaks an
  otherwise-equal tie (acceptance #3).
- **pin to a non-advertising provider → not_implemented** (not a fall-through to the
  default order).
- **advertised-but-unhonored** — the `partial` fake raises `not_implemented` at
  `register`; a fake mutated to drop the method after registration raises at
  `dispatch` (ADR-0009 / issue scope).
- **duplicate / empty `provider_id` → ValueError.**
- **`OpContract`/`Capability` are frozen and hashable** — assignment raises;
  usable in a `set` / as a dict key.
- **malformed `cleanup`** — constructing `OpContract` with a non-`CleanupGuarantee`
  cleanup raises (enum coercion / `__post_init__`).

`tests/providers/test_interfaces.py`:

- a `FakeProvider` is accepted where each plane `Protocol` is expected
  (`isinstance`-against-`runtime_checkable` or a typed assignment), proving the
  Protocols are structurally satisfiable and the method names match.
- the eight planes are all present and distinct in `Plane`; `AllocationPlane` is
  absent from `interfaces.py`.

**Test command:** `uv run python -m pytest tests/providers -q` — no Docker, no env
gating.

## Guardrails

`uv run ruff check` + `uv run ruff format`, `uv run ty check src` (and `tests` —
the pre-commit hook checks both), `uv run python -m pytest -q`. Zero warnings.
