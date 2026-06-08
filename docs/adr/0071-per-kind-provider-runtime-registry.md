# ADR 0071 — Per-kind ProviderRuntime registry (M1.5)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0063](0063-typed-provider-runtime.md) (the typed
  `ProviderRuntime` seam this generalizes from one global to a per-kind map),
  [ADR-0065](0065-provider-component-references.md) (the component-source capabilities a
  per-kind runtime still advertises), [ADR-0066](0066-remove-capability-registry-prototype-from-src.md)
  (the capability-registry prototype this does **not** revive).
- **Partly answers:** ADR-0063's requirement that "a future multi-provider milestone must
  first write a new ADR that defines how `ProviderRuntime`, handler registration, and job
  routing select a provider" — answered here with **static-kind selection**, not capability
  dispatch.
- **Spec:** [`../specs/m1.5-fault-injection-provider.md`](../specs/m1.5-fault-injection-provider.md)

## Context

The active provider seam is `providers.runtime.ProviderRuntime` (ADR-0063): startup builds
**one** local-libvirt implementation per typed port in
`providers.composition.build_default_provider_runtime()` and passes that single runtime to
MCP registrars and worker handlers. There is exactly one runtime in the process and it is a
constant — no code path selects a provider, because there is only one provider.

M1.5 introduces a **second** provider (the fault-injection mock, ADR-0072) that must live
**behind the same plane interfaces** so it stresses the real reconciliation, secret, and
admission seams rather than a bypass. Two providers in one stack means the worker and MCP
boundaries must, for the first time, **choose** which runtime serves a given job — and that
choice must key on something durable and already recorded, not a process-wide constant.

Every long-running operation already targets a durable object that resolves to a Resource:
a job carries a `system_id` / `allocation_id`, a System references its Allocation, an
Allocation references its Resource, and a Resource row carries `kind` (today only
`local-libvirt`, constrained by a CHECK; ADR-0001/§Domain model). So the selection key
already exists in the schema — the missing piece is a registry that maps it to a runtime.

ADR-0063 deferred **capability dispatch** (advertise `(plane, operation, resource_kind)`,
ask a `CapabilityRegistry` for a `BoundOp`) to "a future multi-provider milestone," and
ADR-0066 removed the prototype. M1.5 needs provider selection, but it does **not** need
capability matching: there is exactly one operation set per kind and the kinds are known at
startup. Reaching for the full registry now would re-introduce the dispatch layer ADR-0063
deliberately shelved, with no second *capability shape* to justify it.

## Decision

We will replace the single global `ProviderRuntime` with a **static `ResourceKind →
ProviderRuntime` registry**, resolved for **post-System ops** at the worker and MCP
boundaries from the **System's Resource `kind`** (the pre-grant allocation plane and
discovery do not key on a Resource — see below).

- `providers.composition` becomes the assembly point for a **map** of runtimes
  (`{local-libvirt: build_local_runtime(), fault-inject: build_faultinject_runtime()}`),
  not a single `build_default_provider_runtime()`. It stays the **only** production place
  concrete providers are constructed.
- A new **`ResourceKind.FAULT_INJECT = "fault-inject"`** enum member, and a forward-only
  migration (`0018`, ADR-0072 §schema) widening the `resources_kind_check` CHECK to admit it
  — mirroring how `0003` widened `jobs_kind_check`.
- **Runtime resolution is scoped to post-System ops, and the key source is named — not
  "the target Resource" in general.** Only ops that act on a provisioned System
  (provision/reprovision/teardown, build/install/boot, connect, control, retrieve, debug)
  resolve a runtime, and they key on the **System's Resource `kind`**
  (`job → system → allocation → resource.kind`), which is non-null by then. The
  **pre-grant allocation plane does not resolve a runtime**: `allocations.request` /
  `.release` are core admission logic, a `requested` queued allocation has a **null
  `resource_id`** (ADR-0069) and so has no resolvable kind, and a selector names a kind or a
  resource *for admission*, not a runtime. **Discovery** keys on the **map entry's own
  kind** (the fan-out below), not on a Resource that does not yet exist. An implementer must
  not hang the resolver on a boundary that can run before a System's Resource is fixed.
- Worker handlers and post-System MCP tools that today close over *the* runtime instead
  receive a `kind`-keyed **resolver**. local-libvirt's resolved runtime is **byte-identical**
  to today's — the `local-libvirt` branch builds the same ports. This is a **checkable**
  claim split across two gates: the **CI-enforceable** half — issue 1 diffs **no** behavior
  under `providers/local_libvirt/*` and the CI-run local-libvirt unit/integration tests are
  untouched and green (only wiring changes) — and the **operator-run** half — the live-stack
  e2e suite (operator-run, not CI, per ADR-0042) passes unchanged out of band. So M1.5
  changes the *selection*, not the *behavior*, of the shipped provider.
- **The `fault-inject` entry is opt-in, absent from the default production composition.**
  The map is assembled per deployment: production composition registers only `local-libvirt`;
  the `fault-inject` entry **and its discovery registrar** are added only under an explicit
  config/env opt-in (the same gate the CI/operator fault-injection stack sets). A default
  production deployment therefore has **no** bookable fault-inject Resource — a tenant cannot
  allocate one, pass admission against it, or receive injected failures in prod.
- Selection is **static and exhaustive**: an unknown `kind` is a `configuration_error` at
  resolution, never a silent fallthrough to local-libvirt. There is **no capability
  matching, no `BoundOp`, no dynamic registration** — the registry is a `dict` populated at
  composition time. Capability dispatch (ADR-0063) stays deferred; this ADR does not revive
  it.

## Consequences

- **This is the seam M2 reuses.** The M2 remote-libvirt provider becomes
  "add a `remote-libvirt` entry to the composition map plus a provider package" — and the
  top-level design's falsifiable hypothesis ("adding a provider touches zero `core/*` and
  zero MCP-tool-surface lines") is now *testable against a real second provider* for the
  first time. The registry itself lives in `providers/composition.py` (the provider seam,
  the expected change-surface), so building it does **not** falsify that hypothesis: M1.5 is
  where the selection seam is built and proven, precisely so M2 does not have to invent it
  under remote-provider pressure.
- **New obligation: every runtime consumer threads a resolver.** The worker dispatch path
  and the MCP registrars that close over the runtime today must take the `kind`-keyed
  resolver instead. This is the bulk of M1.5 issue 1 and the one change that reaches
  worker/MCP wiring — bounded, mechanical, and behavior-preserving for local-libvirt.
- **Migration `0018`** widens `resources_kind_check`; no other DDL (the fault-inject
  resource's `seed`/`fault_rate`/`secret_ref` ride the existing `capabilities` jsonb,
  ADR-0072).
- **Discovery stays per-kind and fans out only over the *registered* map.** Each runtime
  keeps its own `discovery_registrar` (local-libvirt registers the local host; fault-inject
  registers a synthetic resource), so startup registration fans out over whatever entries the
  deployment composed. Because `fault-inject` is opt-in (Decision), the synthetic resource is
  registered **only** in a fault-injection deployment — startup in default production
  registers no fault-inject row to begin with.
- **An unknown kind fails closed.** Because resolution is exhaustive, a Resource row whose
  `kind` has no registered runtime raises `configuration_error` rather than being silently
  served by the wrong provider — the fail-fast posture the project standard requires.
- **CHECK↔registry parity is pinned by a test, not by convention.** The DB
  `resources_kind_check` allow-list and the composition map can drift across milestones — a
  migration could widen the CHECK to a kind the map lacks (rows admit, every op on them
  throws) or the map could gain a kind the CHECK forbids (discovery insert fails). A test
  asserts every `resources_kind_check`-allowed kind has a registered, reachable runtime (and
  the reverse), so the two cannot silently diverge. Note the parity is over the **set of
  buildable kinds**, independent of which a given deployment opts to compose.

## Alternatives considered

- **Process-wide config swap** (`KDIVE_PROVIDER=fault-inject` builds the whole process as
  one provider). Simplest — no registry, no resolver — but cannot run local-libvirt and
  fault-inject in one stack, so it cannot host the **admission-control race** tests (which
  need a real resource contended concurrently) alongside a normal provider, and it builds
  **none** of the selection seam M2 needs. Rejected: it defers the hard part to M2 instead
  of de-risking it now, which is the whole reason M1.5 precedes the provider milestones.
- **Capability dispatch now** (revive the ADR-0009/0022 registry). Maximally general, but
  ADR-0063 already rejected wiring it with no second *capability shape* to validate the
  layer, and M1.5's two providers have identical operation sets — static-kind selection
  covers them exactly. Rejected: it re-adds the dispatch layer ADR-0066 removed, paying for
  generality M1.5 does not exercise.
- **Wrapping decorator** (fault-inject ports wrap the real local-libvirt ports). Avoids a
  registry entirely, but conflates "a distinct provider behind the seam" with "a fault
  filter over the real one": the resource stays `local-libvirt`, so it never proves the
  selection path, never exercises a second `kind` end-to-end, and forces the secret/redaction
  test to run against the real provider's emissions. Rejected: it tests chaos, not the
  provider seam M1.5 exists to validate.
