# ADR 0018 — Job-queue enqueue/dequeue + worker execution contract

- **Status:** Proposed
- **Date:** 2026-06-03
- **Deciders:** core-platform
- **Implements:** issue #9; refines [0008](0008-async-worker-tier-job-queue.md)
  (async worker tier + durable queue) over the schema (#6), domain models (#5),
  and repository/idempotency layer (#7).

## Context

[0008](0008-async-worker-tier-job-queue.md) decided *that* long-running work runs
as durable jobs in a Postgres queue dequeued with `FOR UPDATE SKIP LOCKED`, carrying
a lease/heartbeat, bounded retries, and admission idempotency via `dedup_key`. The
`jobs` table, the `Job` model, the `JobState` lifecycle (including the
`running → queued` requeue edge), and the `JOBS` repository all already exist
(merged in #7). What [0008](0008-async-worker-tier-job-queue.md) left open is the
*execution contract*: how attempts are counted, who returns an abandoned job to the
queue versus who dead-letters it, how a long handler keeps its lease alive, and how a
handler failure maps onto the closed `ErrorCategory` set. Each of these has viable
alternatives and shapes the seam every plane issue (#11+) plugs handlers into, so
they are pinned here before code.

## Decision

**1. `attempt` counts *claims*, incremented atomically at dequeue.** `dequeue` sets
`state = 'running'`, `worker_id`, `lease_expires_at = now() + lease`, `heartbeat_at`,
**and** `attempt = attempt + 1` in the one `UPDATE … RETURNING` that claims the row. A
job is claimable when it is `queued`, *or* `running` with a lapsed lease (an abandoned
job), **and** `attempt < max_attempts`. Counting claims rather than failures makes
retries bounded across *worker death*: a worker that dies mid-run has already spent
the attempt, so reclaim cannot loop forever.

**2. Three owners dead-letter / requeue, with no overlap.**
- A handler that **raises in-process**: the worker requeues (`running → queued`,
  clearing `worker_id`/lease) when `attempt < max_attempts`, else dead-letters
  (`running → failed`, set `error_category`).
- A job **abandoned with attempts left** (worker died, lease lapsed): the next
  `dequeue` reclaims it (decision 1). No separate sweeper is needed for this case.
- A job **abandoned after exhausting attempts** (`running`, lapsed lease,
  `attempt >= max_attempts`): left as-is by the queue and the worker; the
  **reconciler** (#12) sweeps it to `failed` (`lease_expired`) and runs the op's
  compensation. This issue does not implement that sweep; it only guarantees such a
  job is *not* re-dequeued (the `attempt < max_attempts` claim predicate).

**3. `enqueue` is upsert-then-fetch, in one transaction.**
`INSERT … ON CONFLICT (dedup_key) DO NOTHING`, then
`SELECT * FROM jobs WHERE dedup_key = %s` in the same transaction, returning the
existing row on conflict and the new row otherwise. `DO NOTHING RETURNING` is **not**
used: it returns no row on conflict, so it cannot return the existing job — the whole
point of admission idempotency.

**4. The lease is renewed by a background heartbeat task, fenced by `worker_id`.**
While a handler runs, the worker runs a concurrent heartbeat coroutine on its **own**
pooled connection that periodically sets `heartbeat_at`/`lease_expires_at` for the
claimed job **only while `worker_id` still matches and the job is `running`**. The
same `worker_id AND state = 'running'` guard fences `heartbeat`, `complete`, and the
failure write: once another worker reclaims a lapsed-lease job, the original worker's
writes match zero rows and are no-ops, so a zombie worker cannot complete or fail a
job it no longer owns. `heartbeat` reports `False` on a zero-row match so a
cooperative handler can abort early.

**5. Handler failures map to the closed `ErrorCategory`.** A `CategorizedError`
contributes its `.category`; any other exception maps to `INFRASTRUCTURE_FAILURE`
(matching the object-store client's precedent). Handler exceptions are *retryable*
(requeue until `max_attempts`). A job kind with **no registered handler** is a
different, non-retryable failure: it maps to `NOT_IMPLEMENTED` and dead-letters at
once (the worker passes a `terminal` flag to `fail`), because no retry can make a
handler appear — and because `attempt` was already charged at claim, a plain
attempt-based `fail` would otherwise *requeue* the unrunnable job and spin it to
`max_attempts`. Only `result_ref` (an object-store reference) and
`error_category` (a taxonomy value) are ever written to the job row — never a
free-form exception message — so the "redact before persist" contract holds *by
construction*. The worker logs the category plus job/worker ids, not the exception
text (which could embed guest output).

**6. Queue functions take an injected `AsyncConnection`; the worker owns the pool.**
`enqueue`/`dequeue`/`heartbeat`/`complete`/`fail` are connection-scoped and
transaction-agnostic, so they are unit-tested directly and compose under a caller's
transaction (an MCP handler enqueues inside its own). The `Worker` holds the pool,
acquires short-lived connections per claim/dispatch/heartbeat, and exposes a
single-iteration `run_once` beneath the continuous `run` loop so the loop body is
testable without sleeping. Because a dispatched job holds two pool connections at
once (its handler's and the heartbeat's), the constructor rejects a pool with
`max_size < 2` — a fast, clear error instead of a per-dispatch stall.

**7. No transaction spans the handler.** A handler runs 30+ minutes, so the worker
holds no transaction across it: each `run_step` (#7) commits in its own short
transaction, and `dequeue`/`complete`/`fail` are each their own. This keeps the xmin
horizon from being pinned for the life of a long job and makes partial progress
durable, so a retry skips already-committed steps. A handler whose job never reaches
`complete` (worker death, or a fenced-out `complete` after a lapsed lease) leaves its
committed steps standing for the reclaiming worker and any object-store write as an
orphan the reconciler GCs. The worker does **not** cancel a running handler when its
lease lapses; sequential-retry safety is `run_step` and concurrent-overlap safety is
the handler's `advisory_xact_lock(SYSTEM, …)` (#7) — neither is the worker's job. The
`heartbeat_interval <= lease / 3` constructor guard keeps a sane configuration from
lapsing the lease mid-run in the first place.

## Consequences

- At-least-once delivery is bounded under both deterministic failure and worker
  death; the headline acceptance cases (happy path, dedup, dead-letter at
  `max_attempts`, lapsed-lease reclaim) follow from decisions 1–3.
- The queue and the reconciler share one rule — `attempt < max_attempts` gates
  re-dispatch — so the reconciler (#12) can recognize a terminally-abandoned job by
  exactly that predicate without coordinating with the worker.
- A handler is a thin async callable `(conn, job) -> result_ref | None`; plane issues
  register one per kind and never touch lease/attempt mechanics.
- Heartbeating uses a second connection per in-flight job; at M0's one-pool,
  low-concurrency scale this is comfortably within the pool. A higher-throughput
  broker can later replace the queue behind the same `enqueue`/`dequeue` seam
  ([0008](0008-async-worker-tier-job-queue.md)).
- Storing only references + categories keeps the queue free of sensitive payloads,
  but means post-hoc debugging of a failed job leans on the worker's structured logs
  and the op's own artifacts, not the job row.

## Alternatives considered

- **Increment `attempt` on failure, not on claim.** Rejected: a worker that dies
  before its failure handler runs never charges the attempt, so a lapsed-lease
  reclaim retries the job forever — unbounded retries, the exact failure
  [0008](0008-async-worker-tier-job-queue.md) forbids.
- **Dequeue inline-dead-letters an abandoned, exhausted job.** Rejected: it puts
  compensation (provider cleanup) on the hot dequeue path and duplicates the
  reconciler's documented role (#12, "abandoned job past `max_attempts` → failed +
  compensation"). The claim predicate cleanly leaves it for the reconciler.
- **`DO NOTHING RETURNING` for enqueue.** Rejected (and called out in the issue):
  returns no row on conflict, so a re-issue would get nothing instead of the existing
  job handle.
- **Single connection for dispatch + heartbeat.** Rejected: the handler's
  connection is busy running its own short step transactions, so a heartbeat `UPDATE`
  on the same connection cannot run concurrently. A separate pooled connection is the
  minimal correct mechanism.
- **One transaction spanning the whole handler (handler + `complete` commit
  together).** Rejected (decision 7): a 30+ minute open transaction pins the xmin
  horizon and blocks vacuum, and it defers every `run_step` commit to job end, so a
  crash near the end loses all partial progress and the retry redoes everything —
  defeating the ledger. Short per-step transactions make progress durable.
- **Worker cancels the in-flight handler when the lease lapses.** Rejected for M0:
  the lease only lapses under a real fault (DB unreachable past the lease) given the
  `heartbeat_interval <= lease / 3` guard, and concurrent-overlap safety already
  belongs to the handler's `advisory_xact_lock` (#7). Cancellation machinery the
  acceptance criteria do not need is deferred (YAGNI).
- **No heartbeat at M0 (a long fixed lease).** Rejected: it would make the
  lease/heartbeat that [0008](0008-async-worker-tier-job-queue.md) rests on a
  documented-but-absent feature, and any op exceeding the fixed lease would be falsely
  reclaimed mid-run. A real heartbeat is small and removes the guess.
- **Per-kind / per-pool dequeue filter and tenant-fair scheduling now.** Rejected for
  M0: one pool, one tenant — `ORDER BY created_at` FIFO trivially satisfies the
  [0008](0008-async-worker-tier-job-queue.md) fairness rule. A `kinds` filter and a
  fairness scheduler are added when M1 introduces a second pool (YAGNI).
- **Persisting the exception message on the job row for debuggability.** Rejected:
  provider exceptions may carry guest output or secrets; the row holds the taxonomy
  category only, and detail goes to redaction-aware logs/artifacts.
