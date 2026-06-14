# Plan — M1 end-to-end integration test (#71)

Derived from the hardened M1 spec
([`m1-allocation-accounting.md`](../../specs/m1-allocation-accounting.md) §"Exit criteria",
the eight falsifiable signals) and the M1 plan's issue ⑨
([`m1-implementation.md`](../../plans/m1-implementation.md)). This is a `type:test` issue:
it adds an integration test, extends the shared seed/preflight helpers, and patches the M0
walking-skeleton harness to seed the now-mandatory budget/quota rows. It changes **no**
`#63-#70` handler behavior. Guardrails (`ruff check`, `ruff format`, `ty check src`,
`pytest -m "not live_vm"`) stay green at every commit.

## Testing contract (ADR-0019)

Handlers are the unit of testing: every assertion calls a plain async handler directly with
an injected `pool` + `RequestContext` (and an injected provider/introspector where the
handler takes one), never through the MCP transport. The disposable-Postgres `migrated_url`
fixture (ADR-0015) gives each test a freshly-migrated schema. The real-libvirt / SSH / drgn
paths stay behind the existing `live_vm` marker (deselected in CI via `-m "not live_vm"`);
they are never un-gated.

## Reused building blocks (already on main)

- `tests.mcp.roles.make_role_fixture`, `PROJECT_A`, `PROJECT_B` — separated
  `viewer`/`operator`/`admin` principals across two projects; each `Principal` carries
  `.ctx` (the handler-unit contract).
- `tests.integration.conftest.open_pool` / `request_context` and the disposable-Postgres
  fixtures.
- Handlers: `accounting.estimate/usage/set_budget/set_quota`, `allocations.request/
  release/renew`, `systems.provision/reprovision/teardown` (+ their job handlers),
  `control.power_system/force_crash_system`, `debug.start_session`, `introspect.run`,
  and `reconciler.loop.reconcile_once` / `_sweep_expired_allocations`.
- The `FakeLibvirtConn` host advertises `vcpus=8`, `memory_mb=16384`,
  `concurrent_allocation_cap=2` — the size ceiling and host cap the criteria assert
  against.

## Deliverables

1. `tests/integration/_seed.py` — add `seed_project_limits(pool, project, *, limit_kcu,
   max_allocations, max_systems)` (insert the `budgets` + `quotas` rows the M1 admission
   gate now requires) and a `register_resource(pool, *, cost_class="local",
   concurrent_allocation_cap=N)` helper returning the registered Resource id. These are the
   only new seeding primitives; the test composes the rest from existing handlers.
2. `tests/integration/test_m1_allocation_accounting.py` — one test function per exit
   criterion (criteria 1 and 6 split into a small number of focused functions where the
   criterion bundles several independent assertions), all non-gated except the single
   `live_vm`-gated criterion-8 function.
3. Patch `tests/integration/test_walking_skeleton.py`'s `_live_vm_preflight` to also require
   an SSH-reachability fixture env var, and seed a budget+quota in the M0 harness's setup so
   the M0 project can still allocate under the M1 explicit-limits regime.
4. `scripts/live-vm/check-ssh-reachable.sh` — a `set -euo pipefail`, shellcheck/shfmt-clean
   helper the live preflight names in its skip message (mirrors the existing two scripts).

## Phase 1 — seed helpers (prerequisite for every assertion)

`seed_project_limits` and `register_resource` land first; the existing
`seed_granted_allocation` already registers a Resource, so `register_resource` factors that
out so a test can register a host with an explicit `concurrent_allocation_cap` (criterion 2
needs cap=1 to hit the host limit; criterion 1's over-caps check needs the default fake
ceiling). Verify: a throwaway test that seeds limits + a resource and reads them back.

## Phase 2 — admission criteria (1, 2) — non-gated

**Criterion 1 (budget denial + input validation + idempotency).** Seed a resource +
quota (generous) and:
- *within-budget grant*: `set_budget` enough for one grant; `request_allocation` →
  `granted`; assert exactly one `reserved` ledger row for the allocation, one `->granted`
  audit row, and `budgets.spent_kcu == estimate`.
- *budget denial*: a fresh project whose budget is below the estimate → `allocation_denied`;
  assert **no** allocation row, **no** ledger row, **no** audit row for it.
- *malformed input* (parametrized): `vcpus=0`, `memory_gb=-1`, `vcpus=99` (over the fake
  host's 8-vcpu ceiling), `window=0` → each `configuration_error` with no allocation/
  ledger/audit row.
- *replayed idempotency key*: two `request_allocation` calls with the same
  `idempotency_key` → same `allocation_id`, exactly one `reserved` row, `spent_kcu`
  unchanged after the replay.
- *same key, two principals*: the same key string under two distinct principals (from the
  role fixture) → two distinct allocations (no cross-principal resolve), each with its own
  `reserved` row.

**Criterion 2 (quota denial).** Two independent assertions:
- *alloc quota*: `set_quota(max_allocations=1, ...)`, grant one, then a second
  `request_allocation` → `quota_exceeded`; assert the project's non-terminal allocation
  count stays 1 and no second ledger row was written.
- *system quota*: `set_quota(max_systems=1, ...)`, then provision one System for a granted
  allocation and call `provision_system` against a **second, distinct** granted allocation
  in the same project → `quota_exceeded`. The two distinct allocations are required: a
  second `provision_system` on the *same* allocation short-circuits to the existing
  System's job and never reaches the new-System quota branch (`systems.py:_within_system_quota`
  counts non-terminal Systems; the first insert as `provisioning` already occupies the slot).
  Assert exactly one System row and no provision job for the denied request.

## Phase 3 — ledger reconciliation + rollup (3) — non-gated

**Criterion 3 (ledger reconciliation).** Drive grant → active → release through the real
handlers as far as the wiring allows, then assert the three independent facts the spec
separates:
- *estimate == reserved*: `accounting.estimate(selector, window)` equals the `reserved`
  row's `kcu_delta` (both `rate × window`).
- *reconciliation nets to actual*: after release, `reserved + reconciled` rows sum to
  `rate × active_hours` (the **actual**), computed from `active_started_at` /
  `active_ended_at`, and `accounting.usage(project).spent_kcu` equals that sum.
- *actual ≠ estimate when the lease did not run full window*: assert the actual is the
  active-interval cost, asserted **separately** from the estimate.

  NOTE (billing-interval gap — see Risks): no `#63-#70` handler stamps `active_started_at`
  on the `granted → active` edge, so the real provision path yields `active_hours = 0` and a
  full credit. To assert a non-zero `active_hours` reconciliation (the spec's intent), this
  test stamps the billing interval explicitly before release — exactly as the existing
  `tests/mcp/test_allocations_reconcile.py` does — and the gap is recorded in the final
  report. The full-credit release-from-`granted` (active_hours = 0) path is asserted through
  the real handler with no stamping.

**Criterion 3 (investigation rollup no-double-count).** Seed one allocation backing Systems
whose Runs belong to two distinct investigations (the reprovision-in-place reuse shape) and
a second allocation whose Runs are solely in investigation X. Assert:
`usage_for_investigation(X)` sums only the exclusive allocation; the shared allocation
appears in **neither** per-investigation rollup; `usage(project).shared_kcu` equals the
shared allocation's spend; and per-investigation sums never exceed the project total.

## Phase 4 — lease expiry, renewal (4, 5) — non-gated

**Criterion 4 (idle lease expiry).** Seed an active, sized, metered allocation with
`lease_expiry` in the past and a `ready` System on it (the `_seed_expired_alloc` shape).
Run `reconcile_once` (NullReaper). Assert the signals the **idle** sweep genuinely
produces: the allocation is `expired` (≠ `released`); a `teardown` job was enqueued for the
System (the orphaned-System repair runs in the same pass); the ledger shows `reserved` then
`reconciled` (the unused reservation credited back); and `active_ended_at` is stamped.

The Run `failed(lease_expired)` transition is **not** produced by the idle sweep: the
reconciler fails a Run with `lease_expired` only through `_repair_abandoned_jobs`
(`loop.py`), which acts on an **in-flight job** whose `run_id` is non-terminal. The idle
case the spec scopes to (criterion 4 — "with no in-flight job") has no such job, so the
Run-fail is the lease-expiry-mid-job path that is explicitly an M1.5 target. This test
therefore asserts the Run-fail-on-`lease_expired` contract on the path that owns it — drive
`_repair_abandoned_jobs` with a seeded `running` job referencing a non-terminal Run whose
lease lapsed and assert the Run reaches `failed` with `failure_category = lease_expired` —
keeping it distinct from the idle-sweep allocation reclaim above. The expiry-mid-job
**race** (worker death during the drain) remains out of scope (M1.5).

**Criterion 5 (renewal).** Through `renew_allocation`:
- *success*: a `+extend` renew on a metered allocation extends `lease_expiry` by the clamped
  window and writes a second `reserved` row; `spent_kcu` increases by the added-window cost.
- *over-budget denial*: a budget that just covers the grant; a renew whose added window
  exceeds the remaining budget → `allocation_denied`, `lease_expiry` unchanged, no second
  `reserved` row.

## Phase 5 — role separation, reprovision (6, 7) — non-gated

**Criterion 6 (role separation).** Using the two-project role fixture:
- *operator refused admin ops*: `set_budget`, `set_quota` raise `AuthorizationError` for an
  operator; `power_system(action="off")` and `teardown_system` raise `AuthorizationError`;
  `force_crash_system` (the three-check gate) returns the `authorization_denied` **envelope**
  (not a raise) for an operator missing the admin factor.
- *admin succeeds / operator succeeds on its surface*: an admin's `set_budget`/`set_quota`
  succeed; an operator's `reprovision_system` and `power_system(action="on")` succeed.
- *cross-project usage refusal*: a `PROJECT_A` viewer calling
  `accounting.usage(investigation_id=<a PROJECT_B investigation>)` is refused (the
  investigation-resolves-to-its-project authorization boundary) — `require_project` /
  `require_role` raises on the foreign owning project.

**Criterion 7 (reprovision-in-place).** Seed a scoped active allocation + a `ready` System
with the reprovision opt-in. `reprovision_system` with a changed profile → `queued`, System
→ `reprovisioning`; run `reprovision_handler` with an injected `Provisioner` fake →
`reprovisioning → ready`. Assert: the same `system_id`, one System row, one allocation row,
the new profile persisted, no new allocation, no new System.

## Phase 6 — live introspection (8) — `live_vm`-gated

**Criterion 8 (live introspection).** A single `@pytest.mark.live_vm` function. The body is
wired by the live runner (mirrors `test_walking_skeleton_full_path`); it calls the preflight
(now including SSH reachability) and asserts: `debug.start_session(transport="ssh")` then
`introspect.run` returns task/module/sysinfo from the live guest, a planted secret is
`[REDACTED]` in the response, and the transcript is marked `sensitive`. The non-gated
analogue (the handler with a fake live introspector + the redaction contract) is already
covered by `tests/mcp/test_introspect_tools.py`; this function asserts the same contract on
the **real** SSH/drgn path and SKIPs in CI.

## Phase 7 — harness patch + preflight

- Extend `_live_vm_preflight` with an SSH-reachability env var
  (`KDIVE_LIVE_SSH_TARGET`), skipping with the exact `scripts/live-vm/check-ssh-reachable.sh`
  to run when absent.
- Seed a budget + quota in the M0 walking-skeleton harness setup so its project still
  allocates under the M1 explicit-limits regime (the spec's stated consequence).
- Add `scripts/live-vm/check-ssh-reachable.sh` (`set -euo pipefail`, `--help`,
  shellcheck/shfmt clean).

## Verification at every commit

1. `uv run ruff check` + `uv run ruff format --check`
2. `uv run ty check src`
3. `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest -q -m "not live_vm"` (the disposable
   Postgres needs Docker; the live paths SKIP)
4. `shellcheck scripts/live-vm/check-ssh-reachable.sh && shfmt -d scripts/live-vm/`

## Rollback / cleanup

Pure-test change: reverting the commit removes the test, the seed helpers, and the script,
and restores the M0 harness setup. No migration, no schema, no handler change — nothing to
roll back beyond `git revert`. The seed helpers are additive (no existing test imports them
yet), so they cannot break a sibling suite.

## Risks

- **Billing-interval gap (product, #67):** `active_started_at` is never stamped by any
  handler on the `granted → active` edge (ADR-0007 §3 says it should be, "when the first
  System reaches `ready`"). The real provision path therefore reconciles every active
  allocation at `active_hours = 0`. Criterion 3's non-zero-actual assertion stamps the
  interval explicitly (as the existing reconcile tests do) and the gap is reported, not
  silently patched (scope boundary).
- **`live_vm` body not executable in CI:** by design — the criterion-8 function SKIPs
  without the operator fixtures; its non-gated handler-level redaction contract is already
  covered, so CI still has a real signal for the redaction invariant.
