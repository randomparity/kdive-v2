# ADR 0021 — Reconciler loop: drift-repair seam, leaked-domain reaping, lease-expiry compensation (M0 subset)

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-03
- **Deciders:** kdive maintainers
- **Refines:** [ADR-0008](0008-async-worker-tier-job-queue.md) (worker tier / job queue),
  [ADR-0009](0009-capability-provider-dispatch.md) (provider seam),
  [ADR-0018](0018-job-queue-worker-execution.md) (job execution contract)

## Context

The [m0 spec](../design/m0-walking-skeleton.md) "Reconciler (M0 subset)" requires a
periodic core loop that repairs four drift cases between Postgres and libvirt:
orphaned System, abandoned (zombie) job, dead DebugSession, and leaked libvirt
domain — plus the lease-expiry policy (an abandoned run-scoped job fails its owning
Run with `lease_expired`, distinct from a `canceled` Run). Issue #12 owns
`src/kdive/reconciler/loop.py`, the `reconciler` subcommand, and `tests/reconciler/`.

Three forces bound the implementation shapes, which the spec leaves open:

1. **The provider does not exist yet.** The leaked-domain case needs the provider's
   `list_owned()` surface ([ADR-0009](0009-capability-provider-dispatch.md)'s
   `DiscoveryPlane`), but the local-libvirt provider is a later issue (#15). The
   reconciler must consume an *abstraction*, not the libvirt impl.
2. **The worker already reclaims lapsed leases.** `queue.dequeue` (#9) reclaims a
   `running` job whose lease lapsed — but only while `attempt < max_attempts` and only
   while a worker is polling. A job that lapses at `attempt == max_attempts` is
   excluded by that predicate and is stranded in `running` forever. The reconciler's
   abandoned-job duty must be scoped to exactly the gap the worker cannot close, not
   duplicate the worker.
3. **Job `payload` has no committed schema yet.** Its interior is owned by the plane
   issues (#15+), so the reconciler cannot assume a typed shape when it computes
   lease-expiry compensation.

## Decision

1. **A narrow reconciler-owned `InfraReaper` Protocol, not the full
   `DiscoveryPlane`.** The reconciler depends on a minimal port it can hold in one
   file:

   ```python
   class OwnedDomain(Protocol):
       name: str
       system_id: UUID | None   # parsed from the libvirt metadata tag; None = untagged

   class InfraReaper(Protocol):
       async def list_owned(self) -> list[OwnedDomain]: ...
       async def destroy(self, name: str) -> None: ...   # idempotent: absent domain is a no-op
   ```

   Tests inject a fake; the local-libvirt provider implements `InfraReaper` when it
   lands (#15). `list_resources` and the rest of `DiscoveryPlane` are not built here —
   the reconciler needs only "what domains do you own" and "destroy this one".

2. **Two reaping mechanisms, split by whether a live row exists.**
   - **Orphaned System** (a `systems` row, not terminal, whose Allocation is
     `released`/`failed`) → the reconciler **enqueues an idempotent
     `(system_id, "teardown")` job** (`queue.enqueue`, `JobKind.TEARDOWN`). It does
     **not** change the System state; the teardown handler (#15) drives
     `→ torn_down`. Re-running a pass returns the same job (`dedup_key` admission
     idempotency), so the repair never duplicates work even while no teardown handler
     is registered. **The job carries a system-principal attribution**
     (`authorizing.principal = "system:reconciler"`), not the owning user's tuple:
     `teardown` is a gated destructive op, but that gate
     (`assert_destructive_allowed`, ADR-0020) is a **tool-boundary** check over an
     interactive `RequestContext` the reconciler does not have. A reconciler teardown
     is system-initiated GC of a System whose Allocation is already gone — a platform
     invariant ("a System never outlives its Allocation"), not a user privilege — so it
     bypasses the interactive gate **by design**, made explicit and auditable by the
     reserved principal. The teardown handler (#15) must treat a `system:reconciler`
     job as pre-authorized GC (skip the gate) while still writing its audit row. This
     reserved principal is a provisional contract for #15.
   - **Leaked domain** (a libvirt domain whose tag points at *no live row*) → the
     reconciler calls **`reaper.destroy(name)` directly**, inline in the pass. A truly
     leaked domain has no live `systems` row to key a job on and no teardown handler
     exists at M0, so a job would only sit queued; a direct destroy is observable and
     testable now.

3. **The reconciler's abandoned-job duty is *only* to dead-letter the zombie the
   worker cannot reclaim, with run-scoped compensation.** A job is swept iff it is
   `running` **and** `lease_expires_at < now()` **and** `attempt >= max_attempts`. The
   reconciler moves it `running → failed` with `error_category = lease_expired`
   (fenced on `state = 'running'` so it no-ops if a worker finalized it first), then
   applies **compensation**: if the job's `payload` carries a `run_id` whose Run is
   non-terminal (`created`/`running`), transition that Run `→ failed` with
   `failure_category = lease_expired`. The `payload` is read **defensively**
   (`payload.get("run_id")`); a job without a `run_id` (a system-scoped job) is
   dead-lettered with no Run compensation. The dead-letter and its Run compensation
   **commit in one transaction, per zombie** — never a batched dead-letter followed by
   separate compensation — so a crash between them cannot leave a `failed` job whose
   Run is still `running` (which the next pass could not re-find, since the job is no
   longer `running`, stranding the Run forever). Reclaim-with-attempts-remaining stays
   `dequeue`'s job; the reconciler does not requeue.

4. **A leaked domain is reaped only under a three-part predicate; row-first ordering
   protects mid-create.** For each owned domain `d` with `d.system_id is not None`,
   reap iff **all** hold: (a) the `systems` row for `d.system_id` is **absent or
   `torn_down`**; (b) **no `teardown` job** for that `system_id` is in a non-terminal
   state (`queued`/`running`) — the don't-race-a-live-teardown guard; (c) `d.system_id`
   is tagged (untagged domains are never reaped — they are not kdive-owned). Because
   provisioning writes the `systems` row (`provisioning`) **before** defining the
   domain (ADR-0009 ordering), any mid-create domain already has a non-terminal row, so
   predicate (a) fails and it is never mistaken for a leak — that, not a job check, is
   what protects "a domain mid-provision is not reaped." Guard (b) checks **only
   `teardown`**, not `provision`: a `provision` job is enqueued from
   `systems.provision(allocation_id, …)` before the System exists (the handler mints
   the `system_id`), so it is keyed on `allocation_id` and its `payload` carries no
   `system_id` to match — and it needs no match, since guard (a) covers mid-provision.
   A `teardown` job is keyed `(system_id, "teardown")` and runs while the row is
   `torn_down`/`releasing` — exactly the window guard (a) would permit a reap — so guard
   (b) is the one that stops a double-destroy.

5. **`reconcile_once` is pure and testable; `run` wraps it; the DB clock is
   authoritative.** `reconcile_once(pool, reaper, *, thresholds…) -> ReconcileReport`
   runs the four repairs once and returns per-category counts; `run(stop, interval)`
   loops it, sleeping `interval` between passes and surviving a transient
   per-iteration error (the Worker's `run`/`run_once` split, #9). All time predicates
   use Postgres `now()` (consistent with the queue), so no reconciler/worker clocks
   need to agree. The orphan and leak repairs take the per-System
   `advisory_xact_lock(SYSTEM, system_id)` (#7/ADR-0016) so a pass cannot race a live
   provision/teardown.

6. **Each repair emits one structured log line** via `kdive.log` (#3) — object kind +
   id + action taken — inside `bind_context(object_id=…, transition=…)`, so drift
   events are observable in the JSON log.

## Consequences

- The reconciler compiles and is fully testable against the `InfraReaper` fake before
  any provider exists; #15 implements `InfraReaper` on the libvirt provider and the
  reconciler is wired to it with no reconciler change.
- The orphaned-System repair is a **no-op-until-#15** in production (the teardown job
  waits queued with no handler), but it is correct and idempotent today and its test
  asserts the enqueue, not the teardown. This is the intended seam, not dead code.
- **Dead DebugSession** sweep: a `live` session whose `worker_heartbeat_at` is
  **non-NULL and older than `debug_session_stale_after`** moves `→ detached`. A
  `live` session with a **NULL** heartbeat is never swept (it may be a just-attached
  session that has not beaten yet); sweeping NULL is deferred to the debug plane that
  owns the heartbeat cadence.
- A finer **time-based provision grace window** (skip a leaked-looking domain younger
  than N seconds) is **deferred to the provider (#15)**: libvirt does not expose a
  domain define-time cheaply, and row-first ordering alone already satisfies the M0
  acceptance criterion (a mid-provision domain has a `provisioning` row, so it is never
  reaped). When #15 tags a `provisioned_at` into the domain metadata, `OwnedDomain` can
  carry it and the grace window becomes a one-line age check — an additive change to
  this ADR, not a rewrite.
- The **`debug_session_stale_after` threshold is a provisional contract**, not a
  derived value: no M0 code writes `worker_heartbeat_at` (the debug plane, #16, owns
  the heartbeat cadence). The reconciler pins the contract — a `live` session must beat
  at most every `debug_session_stale_after / 3` — and makes the threshold injectable
  with a deliberately-long default, because the failure it guards against is detaching a
  *healthy, actively-debugging* session (an irreversible `detached` at M0). #16 sets the
  production default to honor the contract or updates this ADR.
- `lease_expired` now has two producers with one meaning: the worker's `fail` path
  (a handler that raised it) and the reconciler's zombie sweep. Both are the existing
  `ErrorCategory.LEASE_EXPIRED`; no new string.
- The reconciler reads `jobs.payload["run_id"]` as a **provisional contract**: the
  plane issues that enqueue run-scoped jobs (#15+) must place `run_id` in the payload
  (consistent with the `(run_id, step, kind)` `dedup_key` from #9) for compensation to
  fire. Documented here so it is honored, not discovered.
- The reconciler is a third entrypoint (`python -m kdive reconciler`) alongside
  `server`/`worker`; like them it `configure_logging` first and owns its pool.

## Alternatives considered

- **Stand up the full `DiscoveryPlane` provider seam now.** Rejected: the reconciler
  needs only `list_owned` + `destroy`; building `list_resources` and a provider
  registry here adds interface surface no M0 consumer uses ("no premature
  abstraction") and entangles #12 with the provider-dispatch work that owns those
  shapes. The narrow port is a strict subset the provider satisfies later.

- **Route leaked-domain reaping through a teardown job too (uniform mechanism).**
  Rejected: a truly leaked domain has no live `systems` row, so there is no object to
  attribute a `(system_id, teardown)` job to and no handler to run it at M0 — the job
  would dead-letter or sit queued, leaking the domain indefinitely. Direct
  `reaper.destroy` is the only mechanism that actually reaps at M0, and it is
  observable through the fake. The orphaned-System case **does** have a live row and
  keeps the job path (per the issue's "→ teardown job").

- **Have the reconciler also requeue lapsed-lease jobs with attempts remaining.**
  Rejected as redundant and race-prone: `queue.dequeue` already reclaims those
  opportunistically. Two writers (reconciler `UPDATE … SET state='queued'` and a
  worker's reclaiming `dequeue`) racing the same row buys nothing — the worker reclaim
  is sufficient whenever a worker runs, and when none runs the job is simply not
  urgent. The reconciler owns only the `attempt >= max_attempts` zombie, which
  `dequeue` provably cannot touch.

- **Sweep `live` DebugSessions with a NULL heartbeat as dead.** Rejected: a session
  legitimately sits `live` with no heartbeat in the window between attach and the
  first beat; sweeping it would detach a healthy just-attached session. The staleness
  rule keys on a *stale* (non-NULL, old) heartbeat, which is unambiguous drift.

- **Transition the orphaned System straight to `torn_down` in the reconciler (skip the
  job).** Rejected: it would mark the System torn down while the libvirt domain is
  still running (the teardown handler is what actually destroys it), producing the
  exact `torn_down`-row-with-live-domain state the leaked-domain rule then has to
  clean up — drift the reconciler would be *creating*. Enqueuing the durable teardown
  job keeps state honest: the System is torn down only when the domain actually is.

- **Use a wall-clock (`datetime.now`) for the lease/heartbeat predicates.** Rejected:
  multiple reconciler/worker processes with skewed clocks would disagree on whether a
  lease lapsed. Postgres `now()` is the single clock the queue already trusts; the
  reconciler reuses it.
