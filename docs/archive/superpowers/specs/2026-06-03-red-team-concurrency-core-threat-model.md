# Red-team threat model — concurrency / auth / supply-chain core (M0)

**Date:** 2026-06-03 · **Scope:** the M0 concurrency core (provisioning/admission,
job queue, reconciler), the auth boundary (JWT claims → context → RBAC → destructive
gate), and supply-chain posture. **Method:** for each stated invariant, write a
property-based (`hypothesis`) or adversarial (real concurrent connections against a
testcontainers Postgres) test that *tries to falsify* it; corroborated invariants
become regression tests, falsified ones drive a TDD fix.

This document is the durable artifact of the campaign. The tests live under
`tests/adversarial/`.

## Surfaces and invariants attacked

| # | Surface | Invariant under test | Test | Result |
|---|---------|----------------------|------|--------|
| A | `db/locks.py::_lock_key` | Key-folding is deterministic, stays in signed 64-bit range, and separates scopes/keys (a collision only ever *over*-serializes) | `test_lock_key_properties.py` | ✅ corroborated |
| B | `db/idempotency.py::run_step` | "fn at most once per (run_id, step)" | `test_idempotency_concurrency.py` | ⚠️ **over-claim** — see Finding 1 |
| C | `jobs/queue.py` | Mutual exclusion under concurrent `dequeue`; attempt-charging caps total claims at `max_attempts` across worker death; the `worker_id`+`running` fence blocks a reclaimed worker; `enqueue` dedup under a race | `test_queue_concurrency.py` | ✅ corroborated |
| D | `reconciler/loop.py::_repair_abandoned_jobs` | Zombie job dead-lettered *and* its non-terminal run failed atomically; terminal run untouched; live lease never reaped | `test_reconciler_atomicity.py` | ✅ corroborated |
| E | `domain/allocation_admission.py::admit` | Per-resource lock prevents cap overshoot under N concurrent admits on distinct connections | `test_admission_concurrency.py` | ✅ corroborated |
| F | `mcp/auth.py`, `security/rbac.py` | `roles_from_claims`/`context_from_claims` fail closed on malformed claims; `require_role` is rank-monotone; membership and role are both required | `test_auth_properties.py` | ⚠️ **gap** — see Finding 2 |
| G | `pyproject.toml`, `uv.lock` | Exact pinning; lockfile integrity; XML parsing routes through `defusedxml` | audit (below) | ✅ / note |

## Findings

### Finding 1 — `run_step`'s idempotency docstring over-claimed (hardening, doc)

**Claim:** the module docstring said `run_step` "runs a step's function at most once
per (run_id, step)."

**Reality (proven):** `test_bare_run_step_double_executes_fn_under_concurrency` forces
two callers on distinct connections past their `SELECT`-miss with an `asyncio.Barrier`
before either `INSERT` commits — `fn` runs **twice** (`state["calls"] == 2`) while the
unique `(run_id, step)` row still de-dupes the stored *result*. So the bare function
guarantees **result-once, not fn-at-most-once**.

**Why it isn't an active bug:** the sole production caller,
`mcp/tools/runs.py::_run_step_locked`, already holds `LockScope.RUN` around the whole
`run_step` and documents exactly this — `test_run_step_under_run_lock_executes_fn_exactly_once`
proves the lock makes `fn` run once. The defect was the *isolated module's* contract,
which a future caller could trust without holding the lock.

**Fix:** tightened `db/idempotency.py`'s module- and function-docstrings to state the
real contract (result-once; serialize under an external per-scope lock if `fn`'s side
effect must not repeat) and point at the proving tests. No behavior change.

### Finding 2 — `projects` claim parsing swallowed falsy malformed values (hardening)

**Reality (found by hypothesis):** `context_from_claims` used
`raw_projects = claims.get("projects") or ()`. A falsy non-list claim — `0`, `""`,
`False`, `{}` — was silently coerced to "no projects granted" instead of being
rejected, while a truthy non-list (`5`, `"abc"`) raised, and the sibling
`agent_session` check *did* strictly reject `0`. An inconsistency with both the
function's documented contract ("raises on a malformed projects claim") and the
sibling field.

**Severity:** low — it fails **closed** (empty projects = no access; no escalation).
But a misconfigured IdP sending `projects: 0` would yield a silently access-less
session rather than a clear auth error.

**Fix:** `auth.py` now treats absent/`None` as `()` and raises `AuthError` on any other
non-list, matching `agent_session`'s strictness. Driven by
`test_context_from_claims_rejects_non_list_projects`. Existing `tests/mcp/test_auth.py`
stays green.

### Surfaced (not fixed) — `jobs.get`/`jobs.list`/`jobs.cancel` are unscoped in M0

`mcp/tools/jobs.py::get_job` performs **no project-membership check**, unlike its
siblings `get_allocation`/`get_run`/`get_system` (which return not-found for a row
outside the caller's `projects`). Any authenticated principal can read any job by id
(`payload`, `result_ref` object-store key, `authorizing` tuple) and cancel it.

This is a **documented, accepted M0 risk**, not an oversight:
`docs/superpowers/specs/2026-06-03-mcp-skeleton-auth-jobs-design.md` §"M0 isolation
posture" states the exposure is accepted for M0's trusted-operator deployment and that
**#11 must close it before any broader/untrusted multi-principal use**. The original
blocker — no stable key on the `authorizing` jsonb — no longer holds: the planes now
pin `authorizing = {principal, agent_session, project}`
(`systems.py::_authorizing`, `_ctx_from_job`), so the same `project not in ctx.projects`
guard the other getters use is now implementable.

**Recommendation (needs maintainer decision, not unilaterally changed here):** close the
jobs.* read/cancel scoping under #11 now that the key exists. Filed as a surfaced risk
rather than a fix because it contradicts a recorded design decision.

## Supply-chain audit (G)

- **Pinning:** every runtime and dev dependency is `==`-pinned in `pyproject.toml`;
  `uv.lock` carries hashes. `hypothesis==6.142.4` added to the dev group, pinned.
- **XML attack surface:** `defusedxml` is a dependency; provider libvirt-XML parsing
  should route through it. Verify at each `fromstring`/`parse` site as the libvirt
  planes land (follow-up — out of this pass's concurrency-core scope).
- **CVE scan:** run `uv`-ecosystem `pip-audit` against the pinned set before release
  (not run here; flagged as a release-gate step).

## Residual risk / follow-up passes

- **Auth depth:** the destructive-op gate (`security/gate.py`) three-check
  deny-by-default is covered by existing unit tests; a property-based pass over
  `(scope, role, profile_opt_in)` combinations would harden it further.
- **Reconciler `_repair_leaked_domains`:** the `destroy` runs unlocked after the guards
  release the lock (documented TOCTOU mitigated by idempotent `destroy`). Not exercised
  in M0 because `NullReaper` destroys nothing; revisit when the real reaper (#15) lands.
- **Provider planes** (libvirt build/install/control/provision): excluded this pass;
  next red-team target.

## Outcome

No exploitable concurrency or privilege-escalation defect was reproduced: the advisory
locks, queue fencing, admission cap, and reconciler compensation all hold under real
contention. Two fail-closed hardening items were fixed via TDD, one accepted-risk was
surfaced for a maintainer decision, and a 34-test adversarial/property suite now guards
these invariants against regression.
