# Plan — Seeded decision-keyed fault engine + per-op cancel-policy declaration (M1.5 issue 3, #182)

- **Spec:** [`docs/specs/m1.5-fault-injection-provider.md`](../../specs/m1.5-fault-injection-provider.md) §Decomposition issue 3
- **ADR:** [`docs/adr/0072-fault-injection-provider-seeded-engine.md`](../../adr/0072-fault-injection-provider-seeded-engine.md)
- **Depends on:** issue 2 (happy-path mock provider, migration 0018) — merged.

This plan implements **only** the engine (the pure decision function), the per-plane fault
catalog over the existing `ErrorCategory`, the per-op cancel-policy declaration, and the
cross-`PYTHONHASHSEED` determinism guard. It does **not** wire faults into the live worker
ops (the `attempt`-from-durable-state threading and the latency-as-cancel-lever consumption
are issues 5/6/7). The engine is a self-contained, side-effect-free unit the later issues call.

## Design (settled by ADR-0072 — not reopened here)

`fault_for(seed, system_id, plane, attempt, facet) -> float in [0,1)` is a pure function of
stable inputs over a process-independent hash. Three facets key three independent draws:
`fail`, `category`, `latency`. The engine reads `fault_rate` (per-plane map) and
`max_latency_s` (per-plane map) — never wall-clock or `os.urandom`.

### Module: `src/kdive/providers/fault_inject/engine.py`

1. **`FaultPlane(StrEnum)`** — the six perturbable planes: `PROVISION`, `INSTALL`, `BOOT`,
   `CONNECT`, `CONTROL`, `RETRIEVE`. Values are the stable wire strings used as hash key
   material (`"provision"`, …). This is the canonical plane vocabulary the engine keys on.

2. **`FaultFacet(StrEnum)`** — `FAIL = "fail"`, `CATEGORY = "category"`, `LATENCY = "latency"`.

3. **`fault_for(*, seed, system_id, plane, attempt, facet) -> float`** — the load-bearing
   pure draw. Canonical byte encoding of the key `(seed, system_id, plane, attempt, facet)`
   joined with an unambiguous separator (NUL byte — absent from UUID/enum text and the int
   decimal forms, so no two distinct keys collide on the joined bytes), hashed with
   `hashlib.blake2b(digest_size=8)`, the 64-bit digest divided by `2**64` to land in `[0,1)`.
   - `system_id` is a `UUID`; encoded as its canonical 36-char string.
   - `attempt` is an `int` supplied by the caller from durable state (Run boot ordinal /
     attach ordinal / persisted retry count). The engine never reads or increments a counter.
   - Validate `attempt >= 1` (durable ordinals are 1-based; a 0/negative attempt is a caller
     bug — raise `ValueError`, fail fast, do not silently draw).

4. **Per-plane fault catalog** — a frozen `{FaultPlane: tuple[ErrorCategory, ...]}` mapping to
   **existing** categories only, ≥1 per plane and ≥2 where the ADR lists two so the `category`
   draw has something to bucket:
   - `PROVISION → (PROVISIONING_FAILURE,)`
   - `INSTALL → (INSTALL_FAILURE, BOOT_TIMEOUT)`
   - `BOOT → (READINESS_FAILURE, BOOT_TIMEOUT)`
   - `CONNECT → (TRANSPORT_FAILURE,)`
   - `CONTROL → (CONTROL_FAILURE,)`
   - `RETRIEVE → (INFRASTRUCTURE_FAILURE,)`

5. **`FaultDecision` (frozen dataclass)** — the engine's structured result for one
   `(system_id, plane, attempt)`: `fail: bool`, `category: ErrorCategory | None` (the bucketed
   category when `fail`, else `None`), `latency_s: float` (the scaled delay, always ≥0).

6. **`FaultEngine` (frozen dataclass)** — holds `seed: int`, `fault_rate: Mapping[str, float]`,
   `max_latency_s: Mapping[str, float]` (the capability maps; absent plane ⇒ rate 0 / bound 0).
   - `decide(*, system_id, plane, attempt) -> FaultDecision`:
     - `fail = fault_for(... facet=FAIL) < fault_rate.get(plane, 0.0)`.
     - `category` (only when `fail`): bucket `fault_for(... facet=CATEGORY)` across the plane's
       catalog tuple by `int(draw * len(catalog))` (clamped to the last index — `draw<1` so the
       clamp only guards a float-edge `draw==…`).
     - `latency_s = fault_for(... facet=LATENCY) * max_latency_s.get(plane, 0.0)`.
   - `from_capabilities(capabilities: Mapping) -> FaultEngine` — read `seed` / `fault_rate` /
     `max_latency_s` from the resource `capabilities` jsonb via the issue-2
     `capabilities.py` keys; default absent maps to empty / seed to 0. Validate `fault_rate`
     values are in `[0,1]` and `max_latency_s` values are `>= 0` (a malformed capability is a
     `CONFIGURATION_ERROR`, not a silent clamp).

### Module: `src/kdive/providers/fault_inject/cancel_policy.py`

7. **`CancelPolicy(StrEnum)`** — `CLEAN_ROLLBACK`, `BEST_EFFORT`, `ORPHAN_FLAGGED`.

8. **`CANCEL_POLICY: Mapping[FaultPlane, CancelPolicy]`** — each op's **declared** policy,
   consumed by issue 7. Declared from each op's compensation reality:
   - `PROVISION → ORPHAN_FLAGGED` (a half-minted domain may outlive the cancel; the reconciler
     leaked-domain pass reaps it — so the op flags rather than guarantees rollback).
   - `INSTALL → BEST_EFFORT` (install side effects are torn down on a best-effort basis).
   - `BOOT → BEST_EFFORT`.
   - `CONNECT → CLEAN_ROLLBACK` (a transport opened mid-op is closed cleanly on cancel).
   - `CONTROL → CLEAN_ROLLBACK` (a power/crash op is idempotent/atomic — nothing to orphan).
   - `RETRIEVE → BEST_EFFORT` (a partial capture artifact is dropped best-effort).
   - `cancel_policy_for(plane) -> CancelPolicy` accessor; every `FaultPlane` has an entry (a
     missing plane is a `KeyError`-by-construction, asserted by a totality test).

## Tests (TDD — write first, watch fail, then implement)

`tests/providers/fault_inject/test_engine.py`:

- **Determinism guard (acceptance):** launch two subprocesses via `subprocess.run` with
  `PYTHONHASHSEED=0` and `PYTHONHASHSEED=1`, each computing `fault_for` for a fixed key and
  printing the float; assert byte-identical stdout. Proves the draw is process-independent
  (the builtin-`hash()` trap). This is the headline acceptance criterion.
- **No nondeterministic seed source reachable:** assert the `engine` module source contains no
  `os.urandom` / `random.` / `time.` / `secrets.` reference (an AST/static guard so a future
  edit that reintroduces a wall-clock seed fails). Drives the ADR "no nondeterministic draw
  path" obligation.
- **`fault_for` range:** draws land in `[0,1)` across a sweep of keys.
- **Facet independence:** the three facets for the same `(system_id, plane, attempt)` differ
  (not the same draw reused).
- **`attempt` sensitivity:** attempt 1 vs attempt 2 give different draws for the same op.
- **`attempt < 1` raises `ValueError`.**
- **`decide` fail boundary:** `fault_rate=1.0` ⇒ always `fail` for that plane; `0.0` (and an
  absent plane) ⇒ never `fail`. (Acceptance: pin a seed + high rate ⇒ deterministic failure.)
- **`category` ∈ the plane's catalog** whenever `fail`; `None` when not `fail`.
- **`latency_s` scales:** `0 <= latency_s < max_latency_s[plane]`; absent bound ⇒ `0.0`.
- **`from_capabilities`** reads the issue-2 keys and round-trips; malformed `fault_rate` (>1 or
  <0) and negative `max_latency_s` raise `CONFIGURATION_ERROR`.

`tests/providers/fault_inject/test_cancel_policy.py`:

- **Totality:** every `FaultPlane` has a `CANCEL_POLICY` entry (no undefined op).
- **`cancel_policy_for`** returns the declared policy per plane (table assertion).

## Guardrails

`just lint`, `just type`, `just test` green at every commit. `just ci` green before push. No
new agent tools ⇒ no `just docs` regeneration. No DDL (0018 landed in issue 2).

## Out of scope (later issues)

- Wiring faults into the live worker ops / threading durable `attempt` (issues 5/6/7).
- Latency-as-cancel-lever and lease-expiry consumption (issues 5/7).
- Secret resolution/redaction (issue 4 / ADR-0073).
