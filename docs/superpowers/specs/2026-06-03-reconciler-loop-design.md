# Reconciler Loop (M0 subset) — Design

**Issue:** #12 (M0) · **Depends on:** #7 (repository layer, advisory locks,
idempotency — merged), #9 (job queue & worker — merged) · **Decisions:**
[ADR-0021](../../adr/0021-reconciler-loop-drift-repair.md), refining
[ADR-0008](../../adr/0008-async-worker-tier-job-queue.md) /
[ADR-0009](../../adr/0009-capability-provider-dispatch.md) /
[ADR-0018](../../adr/0018-job-queue-worker-execution.md) · **Parent spec:**
[`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("Reconciler (M0 subset)", "Plane interfaces → DiscoveryPlane")

## Goal

A periodic core loop that repairs drift between Postgres and libvirt for the four
M0 failure cases, plus the lease-expiry policy. One new module under
`src/kdive/reconciler/`:

- `loop.py` — the `InfraReaper` port the reconciler consumes, a `reconcile_once`
  that runs all four repairs once and returns a count report, and a `Reconciler`
  that owns a pool and loops `reconcile_once`.

Plus a `reconciler` subcommand in `src/kdive/__main__.py` (the parser already
reserves the slot, per its docstring).

This layer sits **above** the repository/queue layers (#7, #9) and **beside** the
worker — both are durable background loops over the same Postgres state. It owns
*detecting and repairing drift the happy path leaves behind*; it does not own *what
a provider op does* (the handler, #15+) or *admitting/claiming jobs* (the queue, #9).

The four repairs (parent spec):

| Drift | Detection | Repair |
|-------|-----------|--------|
| **Orphaned System** | a `systems` row, not terminal, whose `Allocation` is `released`/`failed` | enqueue an idempotent `(system_id, teardown)` job |
| **Abandoned (zombie) job** | `running`, `lease_expires_at < now()`, `attempt >= max_attempts` | dead-letter `→ failed`/`lease_expired`; compensate the owning Run |
| **Dead DebugSession** | `live`, `worker_heartbeat_at` non-NULL and older than `debug_session_stale_after` | `→ detached` |
| **Leaked libvirt domain** | a tagged domain whose `system_id` row is absent/`torn_down` and has no in-flight provision/teardown job | `reaper.destroy(name)` |

## Non-goals

- **No provider implementation.** The reconciler consumes the narrow `InfraReaper`
  port (`list_owned` + `destroy`); the local-libvirt implementation lands with the
  provider (#15). Tested here against a fake. The full `DiscoveryPlane`
  (`list_resources`, capability registration) is **not** built here (ADR-0021 alt 1).
- **No reclaim of lapsed-lease jobs with attempts remaining.** `queue.dequeue` (#9)
  already reclaims those; the reconciler owns **only** the `attempt >= max_attempts`
  zombie that `dequeue`'s `attempt < max_attempts` predicate can never claim
  (ADR-0021 alt 3). The reconciler never requeues.
- **No System state write on the orphan path.** The reconciler enqueues the teardown
  job and stops; the teardown handler (#15) drives `releasing → torn_down` when the
  domain is actually destroyed. Marking the System `torn_down` while its domain still
  runs would *manufacture* the leaked-domain drift (ADR-0021 alt 5).
- **No NULL-heartbeat DebugSession sweep.** A `live` session with a NULL
  `worker_heartbeat_at` may be freshly attached and not yet beating; only a *stale*
  (non-NULL, old) heartbeat is unambiguous drift (ADR-0021).
- **No time-based provision grace window in M0.** Row-first provisioning ordering
  (ADR-0009: `systems` row `provisioning` written before the domain is defined) plus
  the in-flight-job guard already protect a mid-create domain; a domain-age grace
  window waits for the provider to expose a `provisioned_at` tag (#15).
- **No deeper reconciliation.** Idle-Investigation sweep and mid-job
  secret-resolution failure are M1.5 (parent spec); not here.
- **No schema change.** Every column the reconciler reads/writes — `systems.state`,
  `allocations.state`, `jobs.{state,attempt,max_attempts,lease_expires_at,payload,
  error_category}`, `debug_sessions.{state,worker_heartbeat_at}`,
  `runs.{state,failure_category}` — already exists (#6, merged). The
  `running → failed`, `live → detached`, and `created|running → failed` edges are all
  legal in `domain.state` (#5).

## Components

### `InfraReaper` — the provider port (in `loop.py`)

```python
@runtime_checkable
class OwnedDomain(Protocol):
    name: str                # libvirt domain name (the destroy handle)
    system_id: UUID | None   # parsed from the libvirt metadata tag; None = untagged

@runtime_checkable
class InfraReaper(Protocol):
    async def list_owned(self) -> list[OwnedDomain]: ...
    async def destroy(self, name: str) -> None: ...   # idempotent: absent domain → no-op
```

A strict subset of the parent spec's `DiscoveryPlane`. The reconciler holds an
`InfraReaper`; the libvirt provider implements it later. `destroy` is specified
idempotent so a domain already gone (reaped, or torn down between `list_owned` and
`destroy`) is not an error.

### `reconcile_once` — one pass, four repairs

```python
@dataclass(frozen=True, slots=True)
class ReconcileReport:
    orphaned_systems: int      # teardown jobs enqueued
    abandoned_jobs: int        # zombies dead-lettered
    dead_sessions: int         # sessions detached
    leaked_domains: int        # domains destroyed

async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    debug_session_stale_after: timedelta = timedelta(minutes=2),
) -> ReconcileReport: ...
```

Runs the four repairs in this order on connections drawn from `pool`, returns the
counts. Each repair is a module-level `async def _repair_*(conn, …) -> int` so it is
unit-testable in isolation; `reconcile_once` is the thin composition the acceptance
tests drive. All time comparisons use Postgres `now()` (never a Python clock).

**`_repair_orphaned_systems(conn) -> int`.** One query finds candidates:

```sql
SELECT s.id, s.allocation_id, s.principal, s.agent_session, s.project
FROM systems s
JOIN allocations a ON a.id = s.allocation_id
WHERE s.state NOT IN ('torn_down', 'failed')
  AND a.state IN ('released', 'failed')
```

For each, under `advisory_xact_lock(SYSTEM, system_id)` in one transaction,
re-read the System state (it may have changed while unlocked), and if still
non-terminal, `queue.enqueue(conn, JobKind.TEARDOWN, payload={"system_id": str(id)},
authorizing={principal, agent_session, project}, dedup_key=f"{system_id}:teardown")`.
The lock serializes against a concurrent live teardown; the `dedup_key` makes a
re-enqueue across passes return the same job. The pass counts only a *genuinely new*
enqueue (see Open question O1) so `ReconcileReport.orphaned_systems` measures work
done, not candidates seen. Emit a log line per enqueue.

**`_repair_abandoned_jobs(conn) -> int`.** One query claims zombies (fenced so two
reconcilers do not both compensate):

```sql
UPDATE jobs SET state = 'failed', error_category = 'lease_expired'
WHERE state = 'running'
  AND lease_expires_at < now()
  AND attempt >= max_attempts
RETURNING id, payload
```

For each returned row, **compensation**: `run_id = payload.get("run_id")`; if present
and the Run is non-terminal, `RUNS.update_state` is insufficient (it sets only
`state`, not `failure_category`), so compensation runs a single guarded `UPDATE`:

```sql
UPDATE runs SET state = 'failed', failure_category = 'lease_expired'
WHERE id = %s AND state IN ('created', 'running')
```

(The `created|running → failed` edges are legal; the `WHERE` makes a Run already
terminal a no-op.) A job whose `payload` has no `run_id` (a system-scoped job) is
dead-lettered with no Run compensation. The `UPDATE … RETURNING` over `state =
'running'` is the fence: a worker that finalized the job first leaves no row to
sweep. Emit a log line per dead-lettered job (and per compensated Run).

**`_repair_dead_sessions(conn) -> int`.** One fenced `UPDATE`:

```sql
UPDATE debug_sessions SET state = 'detached'
WHERE state = 'live'
  AND worker_heartbeat_at IS NOT NULL
  AND worker_heartbeat_at < now() - %s          -- debug_session_stale_after
RETURNING id
```

`live → detached` is legal; NULL heartbeats are excluded by the `IS NOT NULL`
predicate. Emit a log line per detached session.

**`_repair_leaked_domains(conn, reaper) -> int`.** `domains = await
reaper.list_owned()`. For each `d` with `d.system_id is not None`, decide *reap*
under one read (in a transaction holding `advisory_xact_lock(SYSTEM, d.system_id)`):

```sql
-- live row present and not torn_down → not a leak
SELECT 1 FROM systems WHERE id = %s AND state <> 'torn_down';
-- an in-flight provision/teardown job for this system → mid-op, do not reap
SELECT 1 FROM jobs
WHERE state IN ('queued', 'running')
  AND kind IN ('provision', 'teardown')
  AND payload->>'system_id' = %s;
```

Reap (`await reaper.destroy(d.name)`) iff **both** selects return no row. The lock +
the in-flight-job guard mean a teardown actively destroying the domain is not raced;
row-first ordering means a mid-provision domain has a `provisioning` row and so is
never reaped. Untagged domains (`system_id is None`) are skipped. Emit a log line per
reaped domain.

> **`destroy` runs outside the advisory-lock transaction.** The lock is released when
> the read transaction commits; `reaper.destroy` (an out-of-process libvirt call) then
> runs unlocked, so a slow/blocked provider call never holds a Postgres lock. The
> idempotent-`destroy` contract covers the small window where a concurrent teardown
> destroys the same domain first. (See Open question O2.)

### `Reconciler` — the loop

```python
class Reconciler:
    def __init__(
        self, pool, reaper, *,
        interval: timedelta = timedelta(seconds=30),
        debug_session_stale_after: timedelta = timedelta(minutes=2),
    ) -> None: ...
    async def run_once(self) -> ReconcileReport: ...     # one pass
    async def run(self, stop: asyncio.Event) -> None: ...  # loop; sleep interval; survive a transient error
```

`run` mirrors `Worker.run` (#9): `while not stop.is_set(): try run_once except
Exception: log + sleep; sleep interval`. A transient per-pass error (e.g. a brief DB
outage) is logged and the loop continues — a durable reconciler must not die on one
bad pass. The `run_once`/`run` split keeps the pass body testable with no sleeping.

### `__main__.py` — the `reconciler` subcommand

Add `sub.add_parser("reconciler", …)` and an `_run_reconciler()` that mirrors
`_run_worker`: open a pool (`min_size=1`), install SIGINT/SIGTERM → `stop.set`,
construct the `Reconciler` over the production `InfraReaper`, `await
reconciler.run(stop)`, close the pool in `finally`. **Until the libvirt provider
(#15) ships an `InfraReaper`, the subcommand needs a concrete reaper.** M0 ships a
`NullReaper` (in `loop.py`): `list_owned` returns `[]`, `destroy` is a no-op — so the
three Postgres-only repairs run in production today and leaked-domain reaping
activates when #15 injects the real reaper. The `NullReaper` is the honest M0 default,
not a stub to delete (it is what "no provider yet" *means* operationally).

## Concurrency & correctness

- **Fenced writes, like the queue.** Every state-changing repair is a single
  `UPDATE … WHERE <precondition> RETURNING` (abandoned jobs, dead sessions) or runs
  under the per-System advisory lock with a re-read (orphans, leaks). Two reconcilers,
  or a reconciler and a worker, cannot double-apply: the loser's `UPDATE` matches zero
  rows. This is the same exactly-one-writer discipline ADR-0018 uses for job finalize.
- **The DB clock is the only clock.** `now()` in every time predicate (lease lapse,
  heartbeat staleness) means skewed reconciler/worker process clocks never disagree
  about drift — the property the queue already relies on (#9).
- **Reconciler vs. worker on the same zombie.** The worker's `dequeue` cannot claim a
  job at `attempt == max_attempts` (its predicate is `attempt < max_attempts`), so the
  reconciler's `attempt >= max_attempts` sweep and the worker's reclaim are
  **disjoint** by construction — no overlap to race. If a worker is mid-handler on a
  job whose lease just lapsed at the last attempt, the reconciler's dead-letter fences
  on `state = 'running'` and the worker's eventual `complete`/`fail` fences likewise;
  whichever writes first wins and the other no-ops (ADR-0018's fencing, unchanged).
- **Advisory-lock scope.** Orphan and leak repairs lock `(SYSTEM, system_id)` — the
  same scope the provision/teardown handlers will take (ADR-0016) — so the reconciler
  serializes against live System ops. Abandoned-job and dead-session sweeps take no
  advisory lock (a single fenced `UPDATE` is atomic on its own).
- **Isolation level.** READ COMMITTED (psycopg default), consistent with `queue` and
  `idempotency` — the reconciler reads committed drift and writes fenced repairs.

## Error handling summary

| Condition | Outcome |
|-----------|---------|
| Orphaned System, Allocation `released`/`failed` | idempotent teardown job enqueued; System state untouched (handler tears down) |
| Orphaned System already terminal (`torn_down`/`failed`) | skipped (query excludes it; re-read under lock re-checks) |
| Zombie job (`running`, lapsed, `attempt >= max_attempts`) | `→ failed`/`lease_expired`, fenced on `running` |
| Zombie job carrying a non-terminal `run_id` | owning Run `→ failed`/`lease_expired` (compensation) |
| Zombie job with no `run_id` / a terminal Run | dead-lettered only; Run untouched |
| `live` session, stale (non-NULL, old) heartbeat | `→ detached` |
| `live` session, NULL heartbeat | not swept (may be freshly attached) |
| Domain tagged, no live row, no in-flight job | reaped via `reaper.destroy` |
| Domain tagged, mid-provision (`provisioning` row or in-flight job) | not reaped |
| Domain untagged (`system_id is None`) | skipped (not kdive-owned) |
| `reaper.destroy` on an already-gone domain | no-op (idempotent contract) |
| Transient error in a pass | logged; `run` sleeps `interval` and continues |

## Testing strategy

Disposable Postgres via the existing `tests/db/conftest.py` fixtures, reused from a
new `tests/reconciler/conftest.py` (import `migrated_url` / `postgres_url`); async
code driven with `asyncio.run(...)` (the established pattern — no `pytest-asyncio`).
`reconcile_once` and the `_repair_*` helpers are tested directly with seeded rows and
an injected **`FakeReaper`** (records `destroy` calls; returns scripted
`OwnedDomain`s). The acceptance requirement — *seeded drift rows are repaired on one
loop pass, one test per case* — maps to one test per repair plus the
mid-provision-not-reaped guard. Env-gated libvirt/gdb/drgn integration tests are
untouched and stay gated.

A small seeding helper builds a minimal valid graph (resource → allocation → system →
investigation → run → debug_session/job) since the FK chain is required; it lives in
`tests/reconciler/conftest.py`.

- **Orphaned System** — seed a `ready` System on a `released` Allocation; one pass
  enqueues exactly one `(system_id, teardown)` job and leaves the System `ready`; a
  **second** pass enqueues no new job (assert one `jobs` row — idempotent); a System
  on an `active` Allocation is **not** touched; a `torn_down` System on a `failed`
  Allocation is **not** touched.
- **Abandoned job** — seed a `running` job with `lease_expires_at` in the past and
  `attempt == max_attempts`; one pass moves it `→ failed` with `lease_expired`. With a
  `payload.run_id` pointing at a `running` Run, that Run becomes `failed`/`lease_expired`;
  with no `run_id`, no Run changes. A `running` job with a **future** lease, or
  `attempt < max_attempts`, is **not** swept (that is `dequeue`'s job).
- **Dead DebugSession** — seed a `live` session with `worker_heartbeat_at` older than
  the threshold; one pass moves it `→ detached`. A `live` session with a **recent**
  heartbeat, and one with a **NULL** heartbeat, are **not** touched.
- **Leaked domain** — `FakeReaper.list_owned` returns a domain tagged with a
  `system_id` that has **no** `systems` row; one pass calls `destroy(name)` once. A
  domain whose `system_id` has a `ready` row is **not** destroyed; an **untagged**
  domain (`system_id is None`) is **not** destroyed; a `torn_down` row **with** a
  lingering domain **is** destroyed.
- **Mid-provision not reaped (headline guard)** — a domain tagged `system_id` S with a
  `provisioning` System row **and** a `running` `provision` job for S; one pass does
  **not** call `destroy` (both the row-state and in-flight-job guards hold). Removing
  the in-flight job but keeping the `provisioning` row still does not reap (row guard);
  this is the acceptance criterion's falsifiable test.
- **`reconcile_once` report** — a pass over a mix of the above returns the correct
  per-category counts.
- **`Reconciler.run`** — a `run_once` that raises once (monkeypatched) is logged and
  the loop continues to a clean pass, then `stop.set()` ends it (the durable-loop
  property); driven with a sub-second `interval`.
- **`__main__` `reconciler` subcommand** — `build_parser().parse_args(["reconciler"])`
  selects it; `_run_reconciler` constructs a `Reconciler` with the `NullReaper` and a
  pool (mirroring the existing `test_main` worker-subcommand test, monkeypatching
  `Reconciler.run` / pool open).

## Files

- Create `src/kdive/reconciler/loop.py`.
- Edit `src/kdive/__main__.py` — add the `reconciler` subcommand + `_run_reconciler`.
- Create `tests/reconciler/__init__.py`, `tests/reconciler/conftest.py`,
  `tests/reconciler/test_loop.py`, `tests/reconciler/test_main.py` (or extend
  `tests/mcp/test_main.py` if the subcommand test fits there — decided in the plan).
- Create `docs/adr/0021-reconciler-loop-drift-repair.md`; add it to
  `docs/adr/README.md`. (done)

## Open questions (resolve in review / plan)

- **O1 — orphan re-enqueue counting.** Should `_repair_orphaned_systems` count a pass
  where `enqueue` returned a *pre-existing* job (no new work) as a repair? Proposed:
  count only when the returned job's `created_at` equals this pass (a genuinely new
  enqueue), so `ReconcileReport.orphaned_systems` measures work done, not candidates
  seen. The idempotency test asserts "second pass enqueues no new job" regardless.
- **O2 — `destroy` failure handling.** If `reaper.destroy` raises (libvirt
  unreachable), should the pass abort or continue to the next domain? Proposed:
  catch per-domain, log, and continue (a `CategorizedError`/any exception on one
  domain must not strand the others); the domain is retried next pass. The count
  reflects only domains actually destroyed.
