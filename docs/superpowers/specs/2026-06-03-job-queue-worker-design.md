# Job Queue & Worker Tier — Design

**Issue:** #9 (M0) · **Depends on:** #7 (repository layer, locks, idempotency —
merged) · **Decisions:** [ADR-0018](../../adr/0018-job-queue-worker-execution.md),
refining [ADR-0008](../../adr/0008-async-worker-tier-job-queue.md) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("Job queue & worker tier", "Failure & retry")

## Goal

The durable-job execution layer for the M0 walking skeleton: a Postgres-backed
queue with at-least-once delivery, lease/heartbeat, bounded retries, and admission
idempotency, plus a worker that claims jobs and dispatches them to handlers
registered per kind. Three new modules under `src/kdive/jobs/`:

- `models.py` — the `JobHandler` callable type and a `HandlerRegistry` keyed by
  `JobKind`.
- `queue.py` — connection-scoped `enqueue` / `dequeue` / `heartbeat` / `complete` /
  `fail` operating on the existing `jobs` table.
- `worker.py` — a `Worker` that owns a pool and runs the claim → heartbeat →
  dispatch → finalize loop.

This layer sits above the repository/idempotency layer (#7) and below the MCP
`jobs.*` tools (#10) and the plane handlers (#11+) that register `JobHandler`s. It
owns *how a job is admitted, claimed, kept alive, and finalized*; it does not own
*what a job does* (the handler) or *when a tool enqueues one* (the handler issues).

## Non-goals

- **No MCP wiring.** `jobs.get/.wait/.cancel/.list` are #10's tools; `jobs.cancel`'s
  state write (`queued|running → canceled`) is reachable today via
  `JOBS.update_state`, and the worker's `worker_id`/`state='running'` fencing already
  makes a concurrently-canceled job a no-op for `complete`/`fail` (cooperative
  cancellation). No `cancel` primitive ships here.
- **No reconciler sweep.** Dead-lettering a job *abandoned after exhausting attempts*
  (`running`, lapsed lease, `attempt >= max_attempts`) belongs to the reconciler
  (#12). This layer only guarantees such a job is never re-dequeued.
- **No provider handlers.** M0's `provision`/`build`/`install`/`boot`/`capture_vmcore`
  handlers land with their plane issues; this layer ships the registry they fill and
  is tested against fakes.
- **No per-pool / per-tenant scheduling.** One pool, one tenant at M0;
  `ORDER BY created_at` FIFO satisfies the ADR-0008 fairness rule trivially. A
  `kinds` filter and fair scheduler arrive with M1's second pool.
- **No schema change.** The `jobs` table (`dedup_key NOT NULL UNIQUE`, `attempt`,
  `max_attempts`, `worker_id`, `lease_expires_at`, `heartbeat_at`, `result_ref`,
  `error_category`) and the `JobState` `running → queued` requeue edge already exist
  (#6, #5, merged).

## Components

### `models.py` — handler type + registry

```python
type JobHandler = Callable[[AsyncConnection, Job], Awaitable[str | None]]
# returns a result_ref (object-store key) or None; raises to fail the job.

class DuplicateHandler(RuntimeError): ...

class HandlerRegistry:
    def register(self, kind: JobKind, handler: JobHandler) -> None: ...  # raises DuplicateHandler on a second register for a kind
    def get(self, kind: JobKind) -> JobHandler | None: ...
```

A handler receives an injected `AsyncConnection` (so it runs its effects and its
`run_step` ledger writes in the worker-provided transaction) and the claimed `Job`
(its `payload` and `authorizing` tuple). It returns the `result_ref` to store on
success, or raises — a `CategorizedError` to choose the failure category, any other
exception to map to `INFRASTRUCTURE_FAILURE`. `register` rejects a duplicate kind so
two issues cannot silently both claim `provision`.

### `queue.py` — connection-scoped queue operations

All functions take an injected `AsyncConnection` and are transaction-agnostic (they
compose under a caller's `conn.transaction()` and are unit-tested directly). Rows are
read with `dict_row` and validated through `Job.model_validate`, matching
`repositories.py`. `payload`/`authorizing` are wrapped in `Jsonb`.

```python
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE = timedelta(minutes=5)

async def enqueue(
    conn, kind, payload, authorizing, dedup_key, *, max_attempts=DEFAULT_MAX_ATTEMPTS
) -> Job: ...

async def dequeue(conn, worker_id, *, lease=DEFAULT_LEASE) -> Job | None: ...

async def heartbeat(conn, job_id, worker_id, *, lease=DEFAULT_LEASE) -> bool: ...

async def complete(conn, job_id, worker_id, result_ref) -> Job | None: ...

async def fail(conn, job, error_category) -> Job: ...
```

**`enqueue` — admission idempotency (upsert-then-fetch).** In one transaction:

```sql
INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key)
VALUES (%s, %s, 'queued', %s, %s, %s)
ON CONFLICT (dedup_key) DO NOTHING;
SELECT * FROM jobs WHERE dedup_key = %s;
```

The `SELECT` returns the pre-existing row on conflict and the freshly inserted row
otherwise, so a re-issued tool gets the **same** `job_id` and never enqueues a
duplicate. `DO NOTHING RETURNING` is deliberately avoided (it returns no row on
conflict). `enqueue` wraps the two statements in its own `conn.transaction()` only
when the connection is not already in one, so it is atomic standalone yet composes
beneath a caller's transaction. (The `INSERT`+`SELECT` must be one transaction so a
concurrent committer's row is visible to the `SELECT` under READ COMMITTED.)

**`dequeue` — claim with reclaim, bounded.** One statement claims the oldest eligible
job and charges an attempt:

```sql
UPDATE jobs SET
    state = 'running', worker_id = %s, attempt = attempt + 1,
    lease_expires_at = now() + %s, heartbeat_at = now()
WHERE id = (
    SELECT id FROM jobs
    WHERE (state = 'queued'
           OR (state = 'running' AND lease_expires_at < now()))
      AND attempt < max_attempts
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

Returns the claimed `Job`, or `None` when nothing is eligible. The disjunction
reclaims a lapsed-lease (`running`) job; `attempt < max_attempts` bounds reclaim and
leaves a terminally-abandoned job for the reconciler; `SKIP LOCKED` lets parallel
workers claim disjoint rows without blocking; `attempt = attempt + 1` charges the
claim (ADR-0018 decision 1). `now()` is the database clock throughout, so lease
arithmetic needs no synchronized worker clocks.

**`heartbeat` — fenced lease renewal.**

```sql
UPDATE jobs SET heartbeat_at = now(), lease_expires_at = now() + %s
WHERE id = %s AND worker_id = %s AND state = 'running'
RETURNING id;
```

Returns `True` when a row matched, `False` when the job is no longer this worker's
running job (reclaimed, completed, failed, or canceled) — the signal a cooperative
handler uses to abort.

**`complete` — fenced success.**
`UPDATE … SET state = 'succeeded', result_ref = %s WHERE id = %s AND worker_id = %s
AND state = 'running' RETURNING *`. Returns the updated `Job`, or `None` if the
fence did not match (the worker lost the job; it logs and drops the result).

**`fail` — requeue-or-dead-letter, fenced.** Decides from the claimed job's
already-incremented `attempt`:

```
if job.attempt >= job.max_attempts:
    UPDATE … SET state = 'failed', error_category = %s
        WHERE id AND worker_id AND state = 'running' RETURNING *   # dead-letter
else:
    UPDATE … SET state = 'queued', worker_id = NULL,
        lease_expires_at = NULL, heartbeat_at = NULL
        WHERE id AND worker_id AND state = 'running' RETURNING *   # requeue
```

Both writes use the `running → {failed,queued}` edges already legal in `JobState`.
The fence makes a `fail` against a reclaimed job a no-op (`RETURNING` yields no row);
`fail` returns the job's post-write state, or the unchanged input job when the fence
missed. `error_category` is ignored on the requeue branch (the job will retry).

### `worker.py` — the claim/dispatch loop

```python
class Worker:
    def __init__(
        self, pool, registry, *, worker_id,
        lease=DEFAULT_LEASE,
        heartbeat_interval=timedelta(seconds=30),
        poll_interval=timedelta(seconds=1),
    ) -> None: ...

    async def run_once(self) -> Job | None: ...      # claim+dispatch one job; None if queue empty
    async def run(self, stop: asyncio.Event) -> None: ...  # loop run_once; sleep poll_interval when idle; exit on stop
```

`run_once`:

1. Acquire a pooled connection, `job = await dequeue(conn, worker_id, lease=…)`,
   commit/release. Return `None` if no job.
2. `handler = registry.get(job.kind)`. If `None`: `await fail(conn, job,
   NOT_IMPLEMENTED)` on a fresh connection and return the job.
3. Start a background heartbeat task (its own pooled connection) that loops
   `await heartbeat(...)` every `heartbeat_interval`, stopping when `heartbeat`
   returns `False` or it is cancelled.
4. Acquire a dispatch connection inside a transaction; `result_ref = await
   handler(conn, job)`; `await complete(conn, job.id, worker_id, result_ref)`. The
   handler's effects and its `complete` commit together.
5. On a handler exception: map to `error_category` (decision 5), `await fail(conn,
   job, category)` (rolling back the handler's aborted transaction first), log the
   category + ids (never the exception text).
6. Cancel and await the heartbeat task in a `finally`; return the job.

`run` is `while not stop.is_set(): job = await run_once(); if job is None: await
asyncio.sleep(poll_interval)`. The split keeps the loop body (`run_once`) testable
with no sleeping and no event plumbing.

`worker_id` is supplied by the caller (`__main__.py`'s `worker` subcommand, #10);
the spec's worker authorization (service-scoped internal grant, no fresh per-job
auth) is satisfied because the worker performs no authorization — it runs whatever
the authorizing tuple on the job already admitted.

## Concurrency & correctness

- **Bounded retries under worker death** — attempt charged at claim, claim predicate
  `attempt < max_attempts`. A worker that dies mid-run leaves the job `running` with
  a lapsing lease and a spent attempt; reclaim resumes only while attempts remain.
- **Exactly-one finalizer via fencing** — every post-claim write requires
  `worker_id = <me> AND state = 'running'`. Once a lapsed-lease job is reclaimed by
  another worker (new `worker_id`), the original worker's `heartbeat`/`complete`/
  `fail` match zero rows. No double-finalize, no zombie completion.
- **Effect-idempotency beneath admission-idempotency** — `enqueue`'s `dedup_key`
  stops a *duplicate job*; the handler's own `run_step` (#7) stops a *duplicate
  effect* when the same job is retried. The two layers are independent and both
  required (a retried job re-runs the handler).
- **Isolation level** — `enqueue`'s `INSERT`-then-`SELECT` and `dequeue`'s reclaim
  read committed state, so they assume READ COMMITTED (psycopg's default), consistent
  with `idempotency.run_step`.

## Error handling summary

| Condition | Outcome |
|-----------|---------|
| Re-`enqueue` with an existing `dedup_key` | returns the existing `Job` (no duplicate) |
| Handler raises `CategorizedError` | `fail` with `error.category`; requeue if attempts remain, else dead-letter |
| Handler raises any other exception | `fail` with `INFRASTRUCTURE_FAILURE` |
| No handler registered for `job.kind` | `fail` with `NOT_IMPLEMENTED` |
| Handler succeeds but job was reclaimed | `complete` fence misses → `None`; worker logs and drops |
| `heartbeat` on a reclaimed/finished job | returns `False` (handler may abort) |
| Job abandoned, `attempt < max_attempts` | next `dequeue` reclaims it |
| Job abandoned, `attempt >= max_attempts` | left for the reconciler (#12); never re-dequeued |

## Testing strategy

Disposable Postgres via the existing `tests/db/conftest.py` fixtures, reused from a
new `tests/jobs/conftest.py` (import the `migrated_url` / `postgres_url` fixtures);
async code driven with `asyncio.run(...)` (the established pattern — no
`pytest-asyncio`). Handlers are tested as plain callables with injected fakes
(CLAUDE.md: handlers are the unit of testing). Env-gated libvirt/gdb/drgn integration
tests are untouched and stay gated.

- **`queue.enqueue`** — first call inserts and returns a `queued` job; a second call
  with the **same** `dedup_key` returns the **same** `job_id` with no second row
  (assert one row in `jobs`); a different `dedup_key` makes a distinct job; `payload`
  and `authorizing` round-trip through `jsonb`.
- **`queue.dequeue`** — claims the oldest `queued` job, setting `running`,
  `worker_id`, `attempt = 1`, a future `lease_expires_at`; returns `None` on an empty
  queue; two concurrent dequeues on two connections claim **different** jobs (SKIP
  LOCKED, no error, no double-claim); a `running` job with a **future** lease is not
  claimed; a `running` job with a **past** lease (simulated lapsed lease) **is**
  reclaimed and its attempt increments; a job at `attempt == max_attempts` is **not**
  claimed.
- **`queue.heartbeat`** — extends `lease_expires_at`/`heartbeat_at` for the owning
  worker (`True`); a wrong `worker_id` or a non-`running` job returns `False` and
  changes nothing.
- **`queue.complete` / `queue.fail`** — `complete` moves `running → succeeded` and
  stores `result_ref` for the owner, `None` for a non-owner; `fail` with
  `attempt < max_attempts` requeues (`running → queued`, `worker_id` cleared) and
  with `attempt >= max_attempts` dead-letters (`running → failed`,
  `error_category` set); a `fail`/`complete` whose fence misses returns the unchanged
  job / `None`.
- **`HandlerRegistry`** — `get` returns a registered handler and `None` for an
  unregistered kind; a second `register` for a kind raises `DuplicateHandler`.
- **`Worker.run_once` (the headline acceptance)** — with a fake pool over
  `migrated_url`:
  - *happy path*: enqueue → `run_once` dispatches the handler, stores its
    `result_ref`, job ends `succeeded`; a second `run_once` returns `None`.
  - *dedup*: two `enqueue`s with one `dedup_key` then one `run_once` runs the handler
    **once** (assert call count == 1) and the second handle equals the first.
  - *dead-letter*: a handler that always raises, driven by repeated `run_once`,
    reaches `failed` after exactly `max_attempts` dispatches with the mapped
    `error_category`; the handler ran `max_attempts` times.
  - *lapsed lease*: claim a job, force its `lease_expires_at` into the past, and show
    the **next** `run_once` reclaims and runs it (attempt incremented), proving a
    lapsed lease returns the job to the queue.
  - *unknown kind*: a job whose kind has no handler ends `failed` with
    `NOT_IMPLEMENTED` without invoking any handler.

## Files

- Create `src/kdive/jobs/models.py`, `src/kdive/jobs/queue.py`,
  `src/kdive/jobs/worker.py`.
- Create `tests/jobs/__init__.py`, `tests/jobs/conftest.py`,
  `tests/jobs/test_queue.py`, `tests/jobs/test_registry.py`,
  `tests/jobs/test_worker.py`.
- Create `docs/adr/0018-job-queue-worker-execution.md`; add it to
  `docs/adr/README.md`. (done)
