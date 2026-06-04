# Red-team threat model — `→expired` sweep vs. concurrent lease renew (M1)

**Date:** 2026-06-04 · **Pass 7** of the red-team engagement. **Scope:** the M1
allocation/accounting plane that landed in PRs #72–#85 — admission (`allocation_admission`),
lease renew (`allocation_renew` / `lease`), the metering ledger (`accounting`), and the
reconciler `→expired` sweep (`reconciler/loop`). **Method:** for each stated invariant,
attempt to falsify it on real Postgres (testcontainers); verify reachability before filing.
Tests: `tests/adversarial/test_lease_expiry_renew_race.py`.

## The finding (confirmed defect, fixed via TDD) — `type:bug`

**Invariant (ADR-0036 §4):** the reconciler reclaims an allocation **only while its lease
has actually elapsed** (`lease_expiry < now()`), and the sweep is idempotent — a re-pass
"sees an `expired` allocation and skips it."

**Falsified.** `_sweep_expired_allocations` selects candidates (`state` non-terminal `AND
lease_expiry < now()`) in **one transaction that closes**, then expires each in a
**separate** per-allocation transaction via `_expire_one`. Between the select and the
per-allocation lock, a `allocations.renew` can commit a future `lease_expiry`. The locked
re-read in `_expire_one` fenced on **terminal state only** — but **a renew extends
`lease_expiry` without changing state** (ADR-0036 §3), and renewing a *lapsed* lease is
explicitly supported (`lease.clamp_extension_hours` bills from `now`). So the terminal-state
fence misses a renew, and the sweep expired an allocation the project had just paid to
extend.

**Why the lock made the race bounded *and* the fix sound.** `renew` and `_expire_one` both
take `LockScope.PROJECT`, so they serialize: the race window is select→lock, and once the
lock is acquired a re-read observes the committed renewal. The fix re-validates the lease
window under the lock against Postgres `now()` (never a Python clock), the second predicate
that selected the candidate — symmetric with the existing terminal-state fence.

**Impact — availability, not overspend.** `accounting.reconcile` credits `actual − Σ
reserved` over **all** reserved rows, so the renewal's reservation is refunded — no budget
leak. The damage is reliability: an agent actively renewing to keep working has its
allocation moved to `expired`, which orphans its `System`; `_repair_orphaned_systems` (same
pass) then enqueues a teardown and destroys the live VM out from under the agent. Severity:
medium (silent, charged-then-refunded loss of a live environment; reachable by any project
that renews near a sweep tick — the default sweep interval is 30 s).

### Fix (`reconciler/loop.py`)

`_expire_one` adds a lease-window re-check under the `PROJECT → ALLOCATION` lock
(`_lease_elapsed`, evaluated in Postgres): if `lease_expiry` is null or `≥ now()`, return
`False` (skip) instead of expiring. Docstrings on `_expire_one` /
`_sweep_expired_allocations` corrected to state both fences (release **and** renew).

### Tests (failing → passing)

| Test | Asserts |
|---|---|
| `test_expire_one_skips_a_renewed_lease` | the deterministic interleave (candidate selected, then a future `lease_expiry` committed) — `_expire_one` returns `False`, the allocation stays `active`, `active_ended_at` stays null. **Red** before the fix (`expired is True`). |
| `test_expire_one_still_expires_a_genuinely_lapsed_lease` | the fence does not over-correct — a still-lapsed lease is reclaimed (`expired`). |
| `test_renew_durable_against_concurrent_sweep` | genuinely concurrent: a real `renew` raced against `_expire_one` on distinct connections; whoever wins the `PROJECT` lock, a renewal that returned `renewed=True` never leaves the allocation `expired` (24 rounds). |

## Invariants attacked and corroborated (no defect)

| Invariant | Source | Result |
|---|---|---|
| `renew` cannot extend past `now + KDIVE_LEASE_MAX` even under concurrency | ADR-0036 §3 | ✅ held — clamp + `PROJECT`-lock serialization (existing suite + this pass) |
| `renew` vs `→expired` sweep cannot double-reconcile / over-charge one allocation | ADR-0040 §4 | ✅ held — both take `PROJECT`; the only gap was the expiry fence above (not a money gap) |
| release vs `→expired` sweep single-reconciliation | ADR-0040 §4 | ✅ held — `PROJECT → ALLOCATION` + terminal-state fence |
| `_apply_to_spent` keeps `spent_kcu` == ledger Σ under the lock | ADR-0007 §3 | ✅ held |

## Observation (RESOLVED in PR #87) — cross-`kind` idempotency replay in `admit`

> **Resolved 2026-06-04** by the Pass 7 follow-up
> (`…admit-idempotency-kind-fence…` threat model, PR #87): `admit._resolve_replay` is now
> scoped to `_REQUEST_KIND` and `admit._record_key` fails closed on the shared-PK
> collision, symmetric with the renew path. Left below as the original observation.


`allocation_renew._resolve_replay` filters the idempotency store by `kind = 'allocations.renew'`,
but `allocation_admission._resolve_replay` does **not** filter by `kind`. The
`(principal, key)` PK is shared across kinds, so a `request` whose `(principal, key)` was
previously used for a `renew` resolves the **renew's** stored row and returns that existing
allocation as a "grant" (`granted=True`) with no new reserve. It is fail-safe (returns a
real owned allocation in the same project, fails closed cross-project, mints no budget and
grabs no extra slot — it under-grants) and contradicts the renew docstring's claim that a
request key and a renew key "never collide in the store." Low severity; recorded for a
maintainer call rather than fixed here (the symmetric fix is to add `AND kind = %s` to
`admit`'s replay query). Not re-filed beyond this note.
