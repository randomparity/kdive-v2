# ADR 0016 — Repository layer, advisory-lock helper, idempotency ledger

- **Status:** Proposed
- **Date:** 2026-06-03
- **Deciders:** core-platform
- **Implements:** issue #7; builds on [0005](0005-postgres-object-store-state.md)
  (advisory locks, idempotency key) and [0003](0003-six-durable-objects.md)
  (durable objects / lifecycles).

## Context

The schema (#6) and domain models / lifecycles (#5) exist; nothing yet reads or
writes them. M0 needs a data-access layer that is typed against the domain models,
serializes per-Allocation/per-System operations, and executes run steps idempotently
(see the m0 spec's "Postgres schema" and "Concurrency"). The layer has no callers
yet — every consumer (handlers, worker, reconciler) lands in later issues — so the
contracts chosen here are internal and refactorable under a strict type checker, but
they shape what those issues build on. Four decisions had viable alternatives.

## Decision

**1. Generic repositories, instantiated per table.** A base `Repository[M]` provides
async `insert` / `get`; a `StatefulRepository[M, S]` subclass adds `update_state` and
binds the object's state enum `S`. Eight module-level instances bind these to the
durable objects (write-once `Artifact` uses the base, so it has no `update_state` and
a wrong-enum transition cannot be written). Column names derive from
`model.model_fields` (they match the SQL columns), rows are read via `dict_row` and
re-validated with `model_validate`.

**2. `insert` persists the object as given, but the database owns timestamps.**
`created_at` / `updated_at` take their DB defaults and return via `RETURNING *`;
`id` is caller-minted. The model's timestamp fields are advisory on insert.

**3. Lookups return `None`; mutations fail fast.** `get` returns `M | None`.
`update_state` raises `ObjectNotFound` (a `RuntimeError`) on an unknown id and
`IllegalTransition` on a disallowed edge, after an atomic `SELECT … FOR UPDATE` →
`can_transition` → `UPDATE … RETURNING` inside its own transaction. `FOR UPDATE`
serializes concurrent updaters: a same-target race makes the loser raise
`IllegalTransition` (self-transition on the now-current state); there is no zero-row
compare-and-swap path.

**4. Lock scope is a closed `LockScope` enum.** `advisory_xact_lock(conn, scope,
key)` takes `LockScope.{ALLOCATION,SYSTEM}`, hashes `(scope, key)` to a signed
64-bit int, and calls the single-bigint `pg_advisory_xact_lock` — disjoint from the
two-int migration lock. After acquiring, it raises unless the connection is
`INTRANS` (a transaction is open to hold the lock), catching the silent
auto-release that an autocommit-with-no-transaction connection would cause.

## Consequences

- CRUD logic lives once; a new object is one instance line plus its model. The cost
  is a generic class that must satisfy the strict `ty` config — validated in the
  first implementation step before the eight instances are built.
- No code can depend on caller-supplied timestamps, so moving to server-generated
  ids + `*Create` models later is additive, not a behavioral change.
- A `None` from `get` forces callers to handle the miss (they map it to
  `stale_handle` per ADR-0005); a raised `ObjectNotFound`/`IllegalTransition` keeps
  consistency bugs loud and separate from operational `CategorizedError`s.
- A `LockScope` typo is impossible; the trade-off is that a new lock scope requires
  an enum edit (intended — it forces the scope into the documented set).
- `advisory_xact_lock` is correct only inside a transaction; the post-acquire
  `transaction_status` guard converts a silent locking no-op into an immediate error
  (and, unlike a `conn.autocommit` check, still permits the valid autocommit +
  `conn.transaction()` pattern, since psycopg leaves `autocommit` true there).
- Lock-key hashing can collide, over-serializing two unrelated keys; it never
  under-serializes, so correctness holds and only concurrency is (rarely) reduced.

## Alternatives considered

- **Explicit per-object CRUD functions (no generic).** Rejected: ~8× near-identical
  SQL; the CLAUDE.md rule-of-three is exceeded, so the abstraction is earned, not
  premature.
- **Server-generated id + per-object `*Create` input models.** Rejected for M0: adds
  eight models and edits #5's surface for an ergonomic no caller needs yet (YAGNI).
  Kept cheap to adopt later by giving the DB timestamp authority now.
- **`insert` writes caller-supplied `created_at`/`updated_at`.** Rejected: lets code
  depend on client clocks and calcifies the behavioral contract, making the
  `*Create` move a behavioral change a type checker can't catch.
- **`update_state` returns `M | None` like `get`.** Rejected: a vanished transition
  target is a consistency error; returning `None` hides it behind an
  easy-to-forget check.
- **Free-string lock scope.** Rejected: a typo silently mints a second,
  non-contending lock — a correctness bug with no compile-time signal.
- **Session-scoped advisory locks with manual unlock.** Rejected by ADR-0005:
  transaction scope is pooler-safe; manual unlock leaks a lock on any early return.
- **Shipping `PROJECT_BUDGET` now.** Rejected: admission control is ADR-0007's
  issue; an unused scope value is speculative.
