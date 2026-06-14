# Red-team threat model — provider state-machine races (M0)

**Date:** 2026-06-03 · **Pass 3** (after concurrency-core and provider-XML passes).
**Scope:** the System state machine where DB transitions coordinate with libvirt
provider calls under `LockScope.SYSTEM` — `provision_handler`, `teardown_handler`,
`force_crash_handler`, `power_handler` (`mcp/tools/systems.py`, `mcp/tools/control.py`).
**Method:** drive the handlers as **genuinely concurrent tasks on separate pooled
connections** (real advisory-lock contention) and assert the safety invariants across
many interleavings. The existing suite simulates these races by flipping DB state inside
the fake provider on one connection; this pass attacks the real lock. Tests:
`tests/adversarial/test_provider_state_races.py`.

## Invariants attacked

| Race | Invariant | Result |
|---|---|---|
| `provision` ∥ `teardown` (same System) | `provision()` runs **unlocked**; the post-provision finalize re-reads state under the lock and reaps the domain it created if a teardown raced it terminal. End state `torn_down`, **no leaked domain**, for every interleaving and both start orders. | ✅ corroborated |
| `force_crash` ∥ `teardown` | Both hold `LockScope.SYSTEM` for their whole transition. End state `torn_down` (teardown is the terminal sink); the NMI fires **at most once** and **never** against a System the lock already shows terminal (force_crash's terminal early-return). | ✅ corroborated |
| double `teardown` (lease-lapse double-dispatch) | Two concurrent runs of one teardown job converge: both succeed, the System reaches `torn_down`, the domain is reaped, and **exactly one** `ready->torn_down` audit row is written despite two runs. | ✅ corroborated |

## Why these hold (verified, not assumed)

- **provision/teardown:** `provision_handler` reads state, runs the slow `provision()`
  *without* the lock, then takes `LockScope.SYSTEM` and re-reads `FOR UPDATE`. If the
  state is no longer `provisioning` it does not finalize; if it is *terminal* it reaps
  the domain it created (ADR-0025 §8). `teardown_handler` commits `torn_down` under the
  lock *before* the unlocked, idempotent `destroy`, so a concurrent provision always
  re-reads `torn_down` and compensates. The two lock holders serialize, and the domain
  name is deterministic per System (`kdive-{system_id}`), so an orphan reap can never
  collide with a fresh provision. No interleaving leaks a domain.
- **force_crash:** the `ready` admission check is advisory; the handler re-checks under
  the lock and early-returns on a terminal System before issuing the NMI, so a
  torn-down System is never crashed.
- **double teardown:** the `state != torn_down` guard makes the transition + audit fire
  once; the unconditional unlocked `destroy` runs on both (idempotent), recovering a
  destroy that failed after the commit.

## Outcome

No state-machine defect was reproduced — the System lock discipline, the
provision-then-compensate pattern, and the terminal-state fences hold under genuine
concurrency. Four concurrent regression tests now guard these invariants against the
real lock (the prior suite only simulated them on a single connection).

## Residual / follow-up
- `power_handler` ∥ teardown was reasoned through (power holds the lock; teardown is the
  terminal sink) but not separately raced; low marginal value given force_crash covers
  the same lock pattern.
- The reconciler's real `InfraReaper`-backed leaked-domain reap (`_repair_leaked_domains`)
  remains `NullReaper` in M0; revisit its unlocked-`destroy` TOCTOU when a real reaper is
  wired (#15).
