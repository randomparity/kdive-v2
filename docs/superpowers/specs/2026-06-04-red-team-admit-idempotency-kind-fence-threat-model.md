# Red-team threat model — `admit` cross-`kind` idempotency replay (M1)

**Date:** 2026-06-04 · **Pass 7 follow-up** of the red-team engagement. **Scope:** the M1
admission idempotency store (`allocation_admission`), closing the latent observation
recorded in the Pass 7 threat model (`…lease-expiry-renew-race…`). Tests:
`tests/domain/test_admission_budget_quota.py::test_request_does_not_replay_a_renew_key`.

## The finding (confirmed defect, fixed via TDD) — `type:bug`

**Invariant (ADR-0040 §3):** the idempotency store dedups retries per `(principal, key)`,
and a request key and a renew key are distinct in the store — "the same key cannot mean a
grant and a renew" (`allocation_renew` docstring). The `(principal, key)` PK is **shared
across kinds**, so a given pair holds at most one row.

**Falsified.** `allocation_renew._resolve_replay` scopes its lookup with `AND kind =
'allocations.renew'`, but `allocation_admission._resolve_replay` did **not** filter by
`kind`. So a `request` (admit) reusing a `(principal, key)` the same principal had used for
a `renew` resolved the **renew's** stored row and returned that allocation as a grant
(`granted=True`) — with no new grant, reserve, or `spent_kcu` change. The fences were
asymmetric: renew was correctly scoped and its `_record_key` caught the shared-PK
`UniqueViolation`; admit did neither.

**Impact — fail-safe, low severity.** The returned allocation is a real, owned allocation
in the same project; a cross-project replay was already fail-closed; it mints no budget and
takes no extra slot (it *under*-grants — the caller gets an existing allocation instead of a
new one). No money or capacity leak. But it is incorrect idempotency semantics and
contradicted the store's documented contract, so it is closed rather than accepted.

## Fix (`allocation_admission.py`)

Symmetric with the renew path:

1. `_resolve_replay` scopes the lookup to `AND kind = %s` (`_REQUEST_KIND`), so a renew
   key never resolves as a request replay.
2. `_record_key` now catches `UniqueViolation` and raises `CONFIGURATION_ERROR` — a genuine
   cross-kind reuse (a request whose `(principal, key)` already holds a renew row) now fails
   closed with a typed denial and a rolled-back grant, instead of an uncaught driver
   exception escaping `admit`.

A legitimate request replay (same key, request kind) still returns the original allocation
with no double charge (existing `test_replayed_idempotency_key_returns_original_no_double_charge`).

## Test (failing → passing)

`test_request_does_not_replay_a_renew_key` — pre-seed a `(principal, "dup")` row under
`allocations.renew` pointing at a real allocation, then `admit` a request with key `"dup"`.
**Red** before the fix (`granted is True`, returns the renew's allocation). **Green** after:
`granted is False`, `category is CONFIGURATION_ERROR`, no second allocation / ledger row /
spend. Mirrors the renew-side `test_key_reused_across_request_kind_is_rejected`.
