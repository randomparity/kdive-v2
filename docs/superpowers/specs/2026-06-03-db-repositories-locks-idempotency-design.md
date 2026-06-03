# Repository Layer, Advisory Locks & Idempotency Ledger — Design

**Issue:** #7 (M0) · **Depends on:** #6 (schema + migration runner, merged) ·
**Decisions:** [ADR-0016](../../adr/0016-repository-layer-locks-idempotency.md) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)

## Goal

The data-access layer for the M0 walking skeleton: typed async CRUD over each
durable object, per-Allocation/per-System serialization via Postgres advisory
locks, and idempotent step execution. Three new modules under `src/kdive/db/`:

- `repositories.py` — async `insert` / `get` / `update_state` per object, returning
  `kdive.domain.models` instances; every state change is guarded by
  `kdive.domain.state.can_transition`.
- `locks.py` — `async with advisory_xact_lock(conn, scope, key)`, wrapping
  `pg_advisory_xact_lock` (transaction-scoped, pooler-safe per ADR-0005).
- `idempotency.py` — `run_step(conn, run_id, step, fn)`: return the stored result
  if the `(run_id, step)` ledger row exists, else run `fn`, store, return.

This layer sits between the domain models (#5) / schema (#6) below it and the MCP
handlers, worker, and reconciler (later issues) above it. It owns *how state is
read and written*; it does not own *when* (that is the handlers' policy).

## Non-goals

- No handler, worker, reconciler, or MCP wiring — those consume this layer later.
- No `audit_log` writer — auditing lands with the handler issue that emits
  transitions; `audit_log` is append-only and has no lifecycle to manage here.
- No `PROJECT_BUDGET` lock scope — admission control is ADR-0007's issue; shipping
  the scope now would be a speculative, unused value.
- No `*Create` input models — see "Insert contract" below.

## Components

### `repositories.py` — typed async CRUD

Two generic classes, instantiated once per table at module scope. The CRUD body is
otherwise written eight times (the CLAUDE.md "rule of three" is met and exceeded).
A base `Repository[M]` provides `insert` / `get`; a `StatefulRepository[M, S]`
subclass adds `update_state` and binds the object's state enum `S`. Write-once
`Artifact` uses the base class, so `ARTIFACTS.update_state` is a *compile* error (the
method does not exist) rather than an unspecified runtime path, and each stateful
repo's `update_state` accepts only its own enum — a wrong-enum call cannot be
written.

```python
M = TypeVar("M", bound=DomainModel)
S = TypeVar("S", bound=StrEnum)

class Repository(Generic[M]):
    def __init__(
        self,
        model: type[M],
        table: str,
        *,
        json_columns: frozenset[str] = frozenset(),
    ) -> None: ...

    async def insert(self, conn: AsyncConnection, obj: M) -> M: ...
    async def get(self, conn: AsyncConnection, obj_id: UUID) -> M | None: ...

class StatefulRepository(Repository[M], Generic[M, S]):
    def __init__(
        self,
        model: type[M],
        table: str,
        state_enum: type[S],
        *,
        state_column: str = "state",
        json_columns: frozenset[str] = frozenset(),
    ) -> None: ...

    async def update_state(
        self, conn: AsyncConnection, obj_id: UUID, new_state: S
    ) -> M: ...
```

Module-level instances (the eight durable objects):

| instance | class | model | table | state column |
|----------|-------|-------|-------|--------------|
| `RESOURCES` | `StatefulRepository` | `Resource` | `resources` | `status` (`ResourceStatus`) |
| `ALLOCATIONS` | `StatefulRepository` | `Allocation` | `allocations` | `state` (`AllocationState`) |
| `SYSTEMS` | `StatefulRepository` | `System` | `systems` | `state` (`SystemState`) |
| `INVESTIGATIONS` | `StatefulRepository` | `Investigation` | `investigations` | `state` (`InvestigationState`) |
| `RUNS` | `StatefulRepository` | `Run` | `runs` | `state` (`RunState`) |
| `DEBUG_SESSIONS` | `StatefulRepository` | `DebugSession` | `debug_sessions` | `state` (`DebugSessionState`) |
| `JOBS` | `StatefulRepository` | `Job` | `jobs` | `state` (`JobState`) |
| `ARTIFACTS` | `Repository` | `Artifact` | `artifacts` | — (write-once) |

**Column mapping.** The model's field names (`tuple(model.model_fields)`) match the
SQL columns one-for-one (verified against `0001_init.sql`). The **read/validate**
path uses the full field set: rows are read with psycopg's `dict_row` factory and
re-validated through `model.model_validate`. The **insert** path uses a narrower
column set — see below.

**Insert contract.** `insert`'s `INSERT` column/value lists are `model_fields` minus
`{created_at, updated_at}` (caller-minted `id` is retained). The two timestamp
columns are deliberately omitted so they take their database defaults; the inserted
row is read back with `RETURNING *` and validated into the full model, so the
**database is the authority for timestamps**. `id` is caller-minted (the model
already requires it; minting it client-side avoids a pre-insert round-trip). The
model's `created_at` / `updated_at` are therefore advisory on insert (whatever the
caller sets is ignored) — documented on the method. This keeps a future move to
server-generated-id + `*Create` models purely additive: no code can come to depend
on caller-supplied timestamps. jsonb columns (`json_columns`) are wrapped in
psycopg's `Jsonb` adapter; all other values adapt natively (`UUID`→uuid,
`datetime`→timestamptz, `StrEnum`→text since `StrEnum` is a `str`).

**`update_state`** (on `StatefulRepository`, so `new_state` is typed to the repo's
own enum `S` — a wrong-enum call is a compile error). Atomic read-check-write inside
the method's own transaction:

```
async with conn.transaction():
    row = SELECT <state_column> FROM <table> WHERE id = %s FOR UPDATE
    if row is None: raise ObjectNotFound
    current = state_enum(row[state_column])
    ensure_transition(current, new_state)            # raises IllegalTransition
    return UPDATE <table> SET <state_column> = %s WHERE id = %s RETURNING *
```

`FOR UPDATE` serializes concurrent updaters on the same row, so the guard check and
the write cannot interleave. `conn.transaction()` works whether or not the caller
holds an outer transaction (it opens a real transaction or a nested savepoint), so
the row lock is held across both statements regardless of the connection's
autocommit setting. `update_state` composes beneath an `advisory_xact_lock` (which
serializes the broader operation) but does not require one.

**Errors.** `get` returns `None` on a miss (a lookup may legitimately miss).
`update_state` raises `ObjectNotFound` (a `RuntimeError` subclass) on a missing
row and `IllegalTransition` (already in `domain.state`) on a disallowed edge — both
programming/consistency errors, distinct from `CategorizedError` (reserved for
operational failures a handler turns into a client response).

### `locks.py` — advisory transaction locks

```python
class LockScope(StrEnum):
    ALLOCATION = "allocation"
    SYSTEM = "system"

@asynccontextmanager
async def advisory_xact_lock(
    conn: AsyncConnection, scope: LockScope, key: UUID
) -> AsyncIterator[None]:
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope, key),))
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError(
            "advisory_xact_lock must run inside an open transaction; the lock "
            "auto-released because no transaction is in progress (ADR-0005). "
            "Wrap the call in `async with conn.transaction()` or use a "
            "non-autocommit connection."
        )
    yield
```

The guard checks `transaction_status`, not `conn.autocommit`: after the lock
`SELECT`, the connection is `INTRANS` exactly when a transaction is open to hold the
lock — true for a non-autocommit connection *and* for an autocommit connection
inside `conn.transaction()` (which issues a real `BEGIN`), and false only when the
lock just auto-released (autocommit, no transaction). `conn.autocommit` is the wrong
signal: psycopg leaves it `True` while `conn.transaction()` holds an open
transaction, so checking it would reject a valid acquire.

**Lock key.** `_lock_key(scope, key)` derives a deterministic signed 64-bit integer:
`blake2b(digest_size=8)` over `scope` bytes, a `0x00` separator, and `str(key)`
bytes, read back with `int.from_bytes(..., "big", signed=True)`. The
**single-bigint** `pg_advisory_xact_lock(bigint)` is a lock space disjoint from
migrate.py's two-int `(class, objid)` migration lock, so application and migration
locks never contend (ADR-0015 already documents this reservation). Hashing folds an
unbounded `(scope, UUID)` key space onto 64 bits; a collision causes two unrelated
keys to over-serialize (safe — never under-serialize). The `0x00` separator removes
`scope`/`key` boundary ambiguity for the NUL-free identifiers used here.

**Release.** `pg_advisory_xact_lock` has no manual unlock; it releases at
transaction end (COMMIT/ROLLBACK). The context manager therefore only acquires —
the surrounding transaction's end is the release. The `transaction_status` guard
turns the silent-no-op case (lock acquired then immediately auto-released) into a
loud error.

### `idempotency.py` — step ledger

```python
JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

async def run_step(
    conn: AsyncConnection,
    run_id: UUID,
    step: str,
    fn: Callable[[], Awaitable[JsonValue]],
) -> JsonValue: ...
```

Logic:

1. `SELECT result FROM run_steps WHERE run_id = %s AND step = %s`. If the **row**
   exists, return its `result` (a stored JSON `null` returns `None`, distinct from
   "no row" — existence is tested on the row, not the value).
2. Else `result = await fn()`.
3. `INSERT INTO run_steps (run_id, step, state, result) VALUES (%s, %s, 'succeeded',
   %s) ON CONFLICT (run_id, step) DO NOTHING RETURNING result`. If a row came back,
   return **the `RETURNING result`** — the stored, JSON-round-tripped value.
4. Otherwise a concurrent caller inserted first; re-`SELECT` and return theirs.

**Return consistency.** Every path returns the value as read back from jsonb (steps
1, 3, 4), never the raw in-memory `fn()` result, so the first call and every replay
return the same stored form. For a type-conformant `JsonValue` the jsonb round-trip
is already identity under Python `==` (dict equality ignores key order; `list` stays
`list`), so this guarantee is robustness against an `Any`-typed or non-conformant
value that the round-trip would normalize. The regression test therefore uses a
value that genuinely differs under `==` after the round-trip — a tuple, which jsonb
returns as a `list` — and asserts the first call and the replay are equal (both the
`list`); it deliberately steps outside `JsonValue` to exercise the stored-form path.

**Failure semantics.** If `fn` raises, nothing is inserted, so the step is not
recorded and a later call retries — a failed step never poisons the ledger.
`run_step` records only *successful* step results; the Run's own `failed`
transition is the handler's responsibility, not this function's.

**Concurrency.** Re-runs (sequential) never re-execute `fn` (the acceptance
criterion). A true concurrent *first* call may run `fn` on both racers, but the
unique `(run_id, step)` key makes the first commit win and both callers return that
stored result; the second `fn`'s result is discarded. Single-execution under
concurrency is provided one layer up by the operation's `advisory_xact_lock` and
the job `dedup_key` at admission (per the m0 spec's layered guarantee).

**Isolation level.** Step 4's "re-`SELECT` and return theirs" assumes the caller's
transaction runs at **READ COMMITTED** (psycopg's default, the server default): the
winner's row was committed by another transaction and a fresh `SELECT` must see it.
Under REPEATABLE READ / SERIALIZABLE the loser's snapshot predates that commit, so
the `INSERT` would instead raise a serialization failure — `run_step` is not
designed for those isolation levels in M0, where the per-operation
`advisory_xact_lock` is the actual concurrent-execution guard and the
concurrent-first-call race is a degenerate fallback. This precondition is documented
on the function.

`run_steps.state` is free `text` (no CHECK in the schema) — a ledger-bookkeeping
field, set to `'succeeded'` here.

## Data flow (illustrative, a future handler)

```
handler (later issue), inside one pooled connection + transaction:
  async with conn.transaction():
    async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
      result = await run_step(conn, run_id, "build", do_build)   # idempotent
      await SYSTEMS.update_state(conn, system_id, SystemState.READY)
  # transaction commit releases the advisory lock and persists the row writes
```

## Error handling summary

| Condition | Raised | Kind |
|-----------|--------|------|
| `KDIVE_DATABASE_URL` unset (existing) | `CategorizedError(CONFIGURATION_ERROR)` | operational |
| `update_state` on missing row / lost CAS | `ObjectNotFound(RuntimeError)` | consistency |
| disallowed transition | `IllegalTransition(ValueError)` | programming |
| `advisory_xact_lock` with no open transaction | `RuntimeError` | programming |
| `fn` raises in `run_step` | propagates; nothing recorded | caller's |

## Testing strategy

Disposable Postgres via the existing `testcontainers` fixtures
(`tests/db/conftest.py`); async code is driven with `asyncio.run(...)` (the
established pattern in `test_pool.py` — no `pytest-asyncio` dependency). A new
`migrated_url` fixture applies migrations to the clean per-test schema and yields
the conninfo for async connections.

- **repositories** — round-trip insert→get for every object (jsonb columns
  included); `get` miss returns `None`; legal `update_state` returns the new state
  with a DB-bumped `updated_at`; illegal transition raises `IllegalTransition`;
  `update_state` on a missing id raises `ObjectNotFound`; concurrent CAS on the same
  row — one wins, the loser raises; timestamps come from the DB, not the input
  (insert a model with a deliberately wrong `created_at` and assert the returned
  value is the DB's). A drift guard introspects `information_schema.columns` for
  `data_type = 'jsonb'` per table and asserts each repo's `json_columns` matches —
  tying model / SQL / `json_columns` together the way the existing enum↔CHECK test
  does.
- **locks** (the headline acceptance) — two real connections: A holds the lock in a
  transaction; B's acquisition is started as a task. The test proves B is genuinely
  *waiting on the lock* (polls `pg_locks` for a `locktype = 'advisory'`,
  `granted = false` row for B's backend) before asserting the task is not `done()`;
  A commits; B proceeds (`asyncio.wait_for` resolves). Plus: different key does not
  block; different scope does not block; a lock acquired on an autocommit connection
  with no `conn.transaction()` raises (no open transaction); `_lock_key` is
  deterministic and scope-sensitive.
- **idempotency** (the headline acceptance) — `run_step` runs `fn` once across two
  calls (`call count == 1`), both return the same result; `None`/`list`/`dict`
  results round-trip; a result that differs under `==` after the jsonb round-trip (a
  tuple, returned as a `list`) gives *equal* values on first call and replay, proving
  every path returns the stored form rather than the in-memory `fn()` result; distinct
  steps are independent; `fn` raising leaves no row and the next call re-executes;
  concurrent first-call race resolves to one stored result.

The env-gated libvirt/gdb/drgn integration tests are untouched and stay gated.

## Files

- Create `src/kdive/db/repositories.py`, `src/kdive/db/locks.py`,
  `src/kdive/db/idempotency.py`.
- Create `tests/db/test_repositories.py`, `tests/db/test_locks.py`,
  `tests/db/test_idempotency.py`; extend `tests/db/conftest.py` with `migrated_url`.
- Create `docs/adr/0016-repository-layer-locks-idempotency.md`; add it to
  `docs/adr/README.md`.
