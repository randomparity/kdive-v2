# ADR 0074 — Wiring the seeded fault engine into the fault-inject ports (M1.5)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0072](0072-fault-injection-provider-seeded-engine.md)
  (the seeded decision-keyed engine this ADR consumes), [ADR-0063](0063-typed-provider-runtime.md)
  (the typed ports the wiring perturbs), [ADR-0021](0021-reconciler-loop-drift-repair.md)
  (the reconciler passes the wired faults drive), [ADR-0036](0036-reservation-lease-semantics.md)
  (the lease window a latency-perturbed op races).
- **Spec:** [`../specs/m1.5-fault-injection-provider.md`](../design/m1.5-fault-injection-provider.md)
  §Validation surface (issue 5).

## Context

ADR-0072 landed the seeded `FaultEngine` (`fault_for`, `FaultDecision`, per-plane
`fault_rate` / `max_latency_s`) **pure and unwired**: it computes a decision but no provider
port consults it, so no spine op is actually perturbed. ADR-0072 also fixed the levers — "a
fault is a `fail` raise mapped to an existing `ErrorCategory`" and "latency is the
reconciler/cancel lever (the same engine emitting a latency with no failure)". Issue 5
(reconciler/teardown drift-repair validation) needs each reconciler pass driven by a **real,
seed-pinned** fault, not a hand-faked drift row — so the engine must reach the ports.

Two wiring decisions ADR-0072 left open, both with viable alternatives:

1. **Where does a port get `attempt` and the engine?** ADR-0072 mandates `attempt` derive
   from durable state (never a process-local counter) and the engine be built from the
   resource `capabilities`. The ports today take none of this.
2. **How is `latency_s` realized without making CI tests slow or flaky?** A real blocking
   sleep for `latency_s` past a deliberately-short lease is the production behavior, but
   `max_latency_s` is sized **above** the lease on purpose (ADR-0072), so a literal sleep in a
   unit test would block for that whole bound.

## Decision

**A `FaultedProvisioning` / `FaultedInstall` decorator wraps the happy-path mock ports.** The
happy-path ports (ADR-0072 issue 2) stay untouched and synthetic; a thin faulting wrapper
consults a `FaultEngine` before delegating. The wrapper is constructed in
`build_faultinject_runtime` only when the resource `capabilities` carry a non-empty
`fault_rate` / `max_latency_s` (so the happy-path composition is unchanged when no fault is
configured). This keeps the happy-path regression surface and the fault surface separate
(the spec's "don't fold the engine into the provider skeleton" rule), one decision per
commit.

**`attempt` is supplied by the caller from durable state, defaulting to 1.** The provision
port has no natural retry ordinal in its signature, so the wrapper accepts an injected
`attempt_for(system_id) -> int` resolver (default: constant 1 — a first attempt). The
validation tests pin `attempt` explicitly; production wiring that threads the Run boot
ordinal / job retry count is a later issue (not issue 5's scope — issue 5 asserts the engine
*reaches* the port, with `attempt` an explicit input, exactly as ADR-0072's engine test
already does). This honours "attempt derives from durable state, never a process-local
counter": the wrapper holds **no** counter of its own.

**`latency_s` is realized through an injected sleep seam, not wall-clock.** The provider ports
are **synchronous** (the handler offloads each call via `asyncio.to_thread`), so the wrapper
takes a **sync** `sleep_s: Callable[[float], None]` defaulting to `time.sleep` — a blocking
sleep on the worker thread, exactly how a real slow provider behaves, without stalling the event
loop. Production sleeps for real; a test injects a recording no-op sleep that captures the
requested delay and returns immediately. The test then asserts the **engine-computed `latency_s` exceeds the
lease window** (the real, seed-derived value) and drives the already-proven reconciler
lease-expiry repair against a job seeded with the lapsed lease that delay would have produced.
This keeps the *fault decision* and the *latency magnitude* real (seed-derived, asserted)
while removing real wall-time from CI — the engine math is exercised, only the blocking is
stubbed.

**The five drift cases map to distinct triggers — keep them separate.** They are driven by a
`fail`-draw lever, a `latency`-only lever, **or** the inventory seam, and they reach *different*
states, so the plan must not conflate them:

- A drawn **`fail`** raises `CategorizedError(decision.category)` **iff `decision.fail`** is
  true (`decision.category` is guaranteed non-None in that branch; the wrapper never raises with
  a `None` category). The category is a **catalog** value (`PROVISIONING_FAILURE`,
  `INSTALL_FAILURE`, …), **never** `lease_expired`. On **provision**, the existing
  `provision_handler` turns this into `System → **failed**` via `_record_provision_failure`. On
  **install/boot**, the existing handler only abandons the run step and **re-raises** (`runs.py`);
  it does **not** transition the owning Run — the worker's `queue.fail` dead-letters the *job*,
  and the *Run* is failed only downstream. Issue 5's drift cases therefore do **not** assert a
  Run reaches `failed` from an install/boot `fail` draw.
- The **orphaned-System** case is **not** a `fail` draw. It is a **successful** provision
  (happy-path or latency-only — the System reaches a *non-terminal* live state) **followed by an
  allocation release/expiry**, so the System outlives its Allocation. `_repair_orphaned_systems`
  selects Systems whose state is **not** terminal (`_ORPHANED_SYSTEM_TERMINAL_STATES =
  (TORN_DOWN, FAILED)`) and whose Allocation **is** terminal → enqueues teardown. A provision
  `fail` draw drives the System to `failed`, which the orphan reaper **excludes**, so a `fail`
  draw is the *wrong* trigger for this case — it demonstrates a different outcome (System→failed),
  not the orphan repair.
- The **lease-expiry-mid-job** case uses the **`latency` lever with no `fail` draw**: a slow
  op (`latency_s × …` > the short lease) whose job lease lapses while running. The owning Run
  reaches `failed(**lease_expired**)` **only** through the reconciler's `_repair_abandoned_jobs`
  compensation — `lease_expired` is the reconciler's category, **not** a fault-catalog category,
  and is distinct from `canceled`. This case asserts no catalog `fail` was drawn for the op.
- The **abandoned-job** case (worker death) is the same `_repair_abandoned_jobs` sweep on a
  job with a lapsed lease and `attempt >= max_attempts`; the slow op (latency) or a simulated
  worker death lapses the lease, and the run-bearing job's owning Run is compensated to
  `failed(lease_expired)`. It shares the lease-expiry mechanism; the distinction is framing
  (worker-death vs lease-window), not a different repair path.
- The **dead-DebugSession** case is a `connect` **`TRANSPORT_FAILURE`** `fail` draw, but its
  reconciler trigger is **indirect**: the dropped transport means the worker stops beating, so
  `worker_heartbeat_at` goes stale, and `_repair_dead_sessions` detaches the `live` session on
  the **stale heartbeat** (`worker_heartbeat_at < now() - stale_after`), **not** on the fault
  category. The fault is the upstream *cause* of the staleness, which is the actual trigger; the
  test seeds the stale-heartbeat state the dropped transport would have produced.
- The **leaked-provider-infra** case uses **no engine draw at all**: it is driven by the
  **inventory seam** (`FaultInjectInventory` / `FaultInjectReaper`) reporting an owned domain
  whose `systems` row is absent or `torn_down`; `_repair_leaked_domains` reaps it via the
  `InfraReaper` port. This is the one case orthogonal to the fault engine — it exercises the
  mock's infra-inventory seam (ADR-0072), not a `fail`/`latency` decision.

No handler changes are required for issue 5: the provision `fail` path and the lease-expiry
latency path both terminate in *already-shipped* transitions.

## Consequences

- Each of issue 5's five drift cases is driven by a **real seeded engine decision**: the
  fail/latency that produces the drift is `engine.decide(...)`, not a fabricated row. A
  reconciler bug the fault surfaces is a finding, not a fixture artifact (ADR-0072's purpose).
- The happy-path mock (issue 2) and its tests are unchanged: the faulting wrapper is additive
  and only assembled when fault config is present.
- The injected `sleep` seam is the **only** non-production substitution in the validation
  tests — and it changes *timing*, not the *decision* — so the tests still assert the real
  engine output. The seam mirrors the existing reconciler tests, which seed an already-lapsed
  lease (`lease_seconds=-60`) rather than waiting real time.
- `attempt` as an explicit wrapper input (not a port-held counter) keeps ADR-0072's
  third determinism leg intact; threading the durable ordinal end-to-end is deferred to a
  production-wiring issue and called out as out of scope here.

## Considered & rejected

- **A real blocking sleep for `latency_s` in the validation tests.** Faithful to production but
  `max_latency_s` is deliberately sized above the lease, so a unit test would block for that
  bound (seconds-to-minutes) — slow and a flakiness vector under a loaded CI runner. Rejected:
  the seam changes only timing, and the lapsed-lease seed pattern is already the repo's
  reconciler-test convention.
- **Fold the engine consult into the existing happy-path ports** (a single
  `FaultInjectProvisioning` that both mints the domain and draws faults). Fewer types, but it
  collapses the happy-path regression surface and the fault surface into one unbisectable
  unit — exactly what the spec's "happy-path first, don't fold the fault engine in" rule
  forbids. Rejected for the decorator split.
- **A port-held `attempt` counter** incremented per call. Simplest signature, but it
  reintroduces the process-local, order-dependent counter ADR-0072 exists to kill — under a
  retry or concurrent attach the same physical op would draw different `attempt`s across runs.
  Rejected: `attempt` is a caller-supplied durable input.
- **Hand-seed the drift rows directly** (no engine consult), asserting only the reconciler
  repair. This is what the pre-existing reconciler tests already do; issue 5's *distinct*
  value is proving the **engine** produces the drift, so a fabricated row would make the test
  vacuous w.r.t. the fault engine. Rejected: the fault must be a real `engine.decide`.
