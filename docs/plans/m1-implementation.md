# M1 — Allocation/Accounting Depth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement each sub-issue. Each
> sub-issue below is sized for a single PR; its bite-sized TDD steps are authored at
> execution time. Steps within an issue use checkbox (`- [ ]`) tracking.

**Goal:** Make the allocation plane real from [`../specs/m1-allocation-accounting.md`](../specs/m1-allocation-accounting.md) — cost model + metering ledger, enforced budgets/quotas (fail-closed), real lease/reservation semantics, and operator/admin RBAC separation — plus two swept-in M0 deferrals (System reprovision-in-place, live drgn introspection over SSH). Still local-libvirt only; **no new resource kind**.

**Architecture:** Unchanged from M0 — a thin async core over Postgres + S3, a Postgres-backed job queue + worker tier, providers behind capability dispatch. M1 *activates* dormant seams (`allocations.lease_expiry`, `capability_scope`, `resources.cost_class`) and *composes* a per-project admission gate onto M0's per-host capacity check; it does not restructure the core. The "new transport/op = provider change only" hypothesis ([0009](../adr/0009-capability-provider-dispatch.md)) is tested by reprovision and SSH landing entirely behind existing `Protocol`s.

**Tech Stack:** As M0 — Python 3.13 · `uv` · FastMCP · `psycopg` 3 (async) · `boto3` · `libvirt-python` · `drgn` · Pydantic v2 · `ruff`/`ty`/`pytest` · `prek`. No new runtime dependency (SSH via the ported v1 backend).

---

## GitHub mechanics

- **Epic:** one parent issue (`type:epic`, milestone `M1 — Allocation/accounting depth`), with each task below attached as a **native GitHub sub-issue**.
- **Labels:** reuse the taxonomy (`type:*`, `area:*`, `provider:*`); the relevant `area:*` for M1 are `area:allocation`, `area:core-platform`, `area:security`, `area:provisioning`, `area:debug`.
- **Milestone:** `M1 — Allocation/accounting depth` (exists, #2).
- Each sub-issue carries its `area:*` (and `provider:local-libvirt` where it touches the provider), a `Depends on:` line, and acceptance criteria copied into the issue body.

## Package layout (M1 additions)

```
src/kdive/
  db/schema/0002_*.sql                    # ledger, budgets, quotas, coefficients; CHECK widenings; alloc size cols
  db/locks.py                             # + LockScope.PROJECT
  db/repositories.py                      # + LEDGER, BUDGETS, QUOTAS, COST_CLASS_COEFFICIENTS repos
  domain/models.py                        # + LedgerEntry, Budget, Quota, CostClassCoefficient
  domain/state.py                         # + allocation granted/active→expired; system ready↔reprovisioning,→failed
  domain/cost.py                          # NEW — kcu rate/cost, W_CPU/W_MEM, coeff resolution
  domain/accounting.py                    # NEW — ledger emit (reserved/reconciled), rollup, budget_remaining
  domain/allocation_admission.py          # extend admit(): PROJECT lock + quota + budget + reserve + lease window
  mcp/tools/accounting.py                 # NEW — estimate, usage, set_budget, set_quota
  mcp/tools/allocations.py                # + renew
  mcp/tools/systems.py                    # + reprovision; + system-quota check on provision
  mcp/tools/debug.py                      # + transport="ssh"; + introspect.run (live)
  providers/local_libvirt/provisioning.py # + reprovision op + handler
  providers/local_libvirt/connect.py      # + ssh transport backend + capability
  providers/local_libvirt/introspect_drgn.py # + live introspect (drgn over ssh)
  reconciler/loop.py                      # + →expired sweep (feeds existing teardown)
tests/                                    # mirrors src/; tests/integration/ for the live_vm path
```

## Dependency graph (issue order)

```
gating spine:  ①schema/models → ②cost-model → ③ledger/usage → ④admission-gate → ⑤lease/renew/expiry
forks:                                                          ④ → ⑥RBAC-hardening
               ① → ⑦reprovision         (+ M0 #16 provisioning)
               ⑧ssh/live-introspect     (off M0 #18 connect, #20 drgn, #23 secrets — no M1 dep)
join:          ①…⑧ → ⑨integration
```

- `①` is the only hard gate for the accounting chain; everything in `②③④⑤` is sequential because each composes the prior (rate → ledger → debit-in-admission → expiry-credit).
- `⑥` forks off `④` (the admin surface it gates exists there).
- `⑦` needs only `①`'s `reprovisioning` state + the M0 provisioning plane.
- `⑧` is fully independent of the accounting work (it extends M0 debug planes), so it parallelizes from the start.
- `⑨` joins everything.

## Orchestration waves (subagent dispatch)

Each wave's issues are independent and dispatch in parallel; the next wave waits on the prior. Each subagent works in its **own external worktree** (sibling of the repo, never nested) and opens one PR per issue.

| Wave | Issues (parallel) | Unlocks |
|------|-------------------|---------|
| 1 | **①** schema/models · **⑧** ssh/live-introspect | the spine + the independent debug work |
| 2 | **②** cost-model · **⑦** reprovision | — |
| 3 | **③** ledger/usage | — |
| 4 | **④** admission-gate | — |
| 5 | **⑤** lease/renew/expiry · **⑥** RBAC-hardening | — |
| 6 | **⑨** integration test | M1 exit |

## Test environments

- **Unit + service tests** run anywhere on the M0 harness (disposable Postgres + MinIO + mock OIDC). M1's mock OIDC issuer now mints **separated** principals (`viewer`/`operator`/`admin` per project) — issue ⑥ extends the fixture; ⑦⑧⑨ reuse it.
- **`live_vm` tests** (⑦ reprovision, ⑧ SSH/live-introspect, ⑨ integration) require the M0 KVM/nested-virt host + kdump-enabled guest image, and additionally a **guest reachable over SSH** with a key/password resolvable through the file-ref secret backend (a fixture credential, `ssh_credential_ref`). The `live_vm` preflight gains an SSH-reachability check.
- **No new from-scratch cost**: reprovision reuses the warm-tree build/provision fixtures; SSH uses the already-booted guest.

---

## Phase A — Accounting foundation (gating spine)

### Issue ① — Migration 0002, accounting models, state edges, PROJECT lock
- **Labels:** `area:core-platform` · `area:allocation`
- **Depends on:** — (M0 complete)
- **Goal:** The M1 data layer: new tables, widened state machines, the project lock — no behavior.
- **Files:** Create `src/kdive/db/schema/0002_*.sql`; extend `db/locks.py`, `db/repositories.py`, `domain/models.py`, `domain/state.py`; tests under `tests/db/`, `tests/domain/`.
- **Scope:**
  - `0002_*.sql`: `cost_class_coefficients` (seed `('local', 1.0)`), `budgets`, `quotas`, `ledger` (signed `kcu_delta`, `event_type CHECK IN ('reserved','reconciled')`, indexes on `project`/`allocation_id`); `ALTER allocations ADD requested_vcpus int, requested_memory_gb int, active_started_at timestamptz, active_ended_at timestamptz` (the `active_hours` billing interval — not `updated_at`); widen `allocations` CHECK with `expired` and `systems` CHECK with `reprovisioning`.
  - `domain/models.py`: `LedgerEntry`, `Budget`, `Quota`, `CostClassCoefficient` Pydantic models.
  - `domain/state.py`: edges `AllocationState.GRANTED/ACTIVE → EXPIRED`; `SystemState.READY → REPROVISIONING`, `REPROVISIONING → READY`, `REPROVISIONING → FAILED`; matching guard-table tests.
  - `db/locks.py`: `LockScope.PROJECT`; `db/repositories.py`: typed repos for the four new tables.
- **Acceptance:** `migrate.py` applies `0002` on the M0 schema and is a no-op on re-run; new `state.py` edges pass and a representative illegal one raises; `LockScope.PROJECT` serializes two connections on the same project key (proven with two connections); seed coefficient row present.

### Issue ② — Cost model + accounting.estimate
- **Labels:** `area:allocation`
- **Depends on:** ①
- **Goal:** The kcu rate/cost computation and a pre-commit estimate tool.
- **Files:** Create `src/kdive/domain/cost.py`, `src/kdive/mcp/tools/accounting.py` (estimate only); tests.
- **Scope:**
  - `cost.py`: `W_CPU=1.0`, `W_MEM=0.25` constants; `rate(coeff, vcpus, memory_gb)` and `cost(rate, hours)`; `resolve_coeff(conn, cost_class)` reading `cost_class_coefficients`, failing closed (`configuration_error`) on a missing row.
  - `validate_selector(selector, resource)` / `validate_window(window)`: reject `vcpus < 1`, `memory_gb < 0`, a selector over the resource's advertised caps, or `window ≤ 0` (`configuration_error`), so `rate`/`estimate` are always `≥ 0` (the budget-minting guard, ADR-0007 decision 2). Reused by `estimate` and by admission (④).
  - `accounting.estimate({selector, window})` → `{estimate_kcu, rate_kcu_per_hr, breakdown}`; `viewer` role; validates inputs first.
- **Acceptance:** `rate` matches the size-weighted formula for sample sizes; a `cost_class` with no coefficient row raises `configuration_error`; a negative `memory_gb`, a `vcpus < 1`, an over-caps selector, and a `window ≤ 0` each raise `configuration_error` (never a negative estimate); `accounting.estimate` returns the rate×window product with a vcpu/memory breakdown; `viewer` may call it.

### Issue ③ — Metering ledger emit + accounting.usage + release reconciliation
- **Labels:** `area:allocation`
- **Depends on:** ②
- **Goal:** Append-only ledger writers, the release-time reconciliation, and the usage rollup.
- **Files:** Create `src/kdive/domain/accounting.py`; extend `mcp/tools/accounting.py` (usage), `mcp/tools/allocations.py` (wire reconcile into `release`); tests.
- **Scope:**
  - `accounting.py`: `reserve(conn, allocation, estimate)` writes a `reserved` row; `reconcile(conn, allocation)` computes `actual = rate × active_hours` (`active_hours = active_ended_at − active_started_at`, read from the allocation row, never from `updated_at`) and writes `kcu_delta = actual − Σ reserved`; `usage(conn, project)` / `usage_for_investigation(conn, id)` return `{spent_kcu, budget_remaining, by_cost_class}` where `budget_remaining = limit_kcu − Σ kcu_delta`.
  - Wire `reconcile` into `allocations.release`: **under `LockScope.PROJECT → ALLOCATION`**, stamp `active_ended_at` on the `active → releasing` transition and write the `reconciled` credit in one transaction, so release and the `→expired` sweep (⑤) — which takes the same per-Allocation lock — can never both reconcile one allocation (release on a terminal allocation → `stale_handle`). Release from `granted` leaves `active_started_at` null → `active_hours = 0` → full credit.
  - `accounting.usage(project | investigation_id)` tool; `viewer` role **scoped to the target project** — the `investigation_id` form resolves the investigation's owning project and enforces `require_project` + `require_role(viewer)` on it (no cross-project read bypass; ADR-0007 decision 6 / ADR-0037).
- **Acceptance:** a `reserve` then `reconcile` for a known active duration net to `rate × active_hours`; `usage` rolls up signed deltas and computes `budget_remaining`; release from `granted` (never active) reconciles to a full credit; a release racing an expired-lease sweep yields exactly **one** `reconciled` row (two-connection test); investigation rollup sums across two allocations; a `viewer` in project A is refused `usage(investigation_id)` for a B-owned investigation.

### Issue ④ — Budget/quota admission gate + set_budget/set_quota
- **Labels:** `area:allocation` · `area:security`
- **Depends on:** ③
- **Goal:** Fail-closed admission: per-project quota + budget check-then-debit, composed with M0's host cap, plus the admin set tools and the lease window at grant.
- **Files:** Extend `src/kdive/domain/allocation_admission.py`, `mcp/tools/allocations.py` (request selector/window), `mcp/tools/systems.py` (system-quota check), `mcp/tools/accounting.py` (set_budget/set_quota); tests.
- **Scope:**
  - Extend `admit()`: **validate selector + window first** (②'s `validate_selector`/`validate_window` → `configuration_error`, so `estimate ≥ 0`); then acquire `LockScope.PROJECT(project)` **then** `LockScope.RESOURCE` (the global order `PROJECT < RESOURCE < ALLOCATION < SYSTEM`); under the project lock check `max_concurrent_allocations` (→ `quota_exceeded`) and `budget_remaining ≥ estimate` (→ `allocation_denied`); under the resource lock the unchanged M0 host-cap check; in one transaction insert `granted` Allocation (with `lease_expiry = now()+window`, `requested_vcpus/memory_gb`; `active_started_at` null until provisioned), the `reserved` ledger row (call ③'s `reserve`), and the audit row.
  - Stamp `active_started_at` on the `granted → active` transition (the System reaches `ready` in `systems.provision`) so the billing interval opens when work actually starts.
  - `systems.provision`: under `LockScope.PROJECT`, check `max_concurrent_systems` (non-terminal System count) → `quota_exceeded`.
  - `accounting.set_budget(project, limit_kcu)` / `set_quota(project, max_allocations, max_systems)` — `admin` role. **No silent default:** a project with no budget row is denied (`allocation_denied`, `limit_kcu = 0`) and no quota row is denied (`quota_exceeded`) — allocation requires both rows to exist. A deployment seeds them explicitly.
- **Acceptance:** over-budget request → `allocation_denied`, **no** allocation/ledger/audit row; a malformed request (`vcpus < 1`, `memory_gb < 0`, over-caps selector, `window ≤ 0`) → `configuration_error`, no row, no negative `reserved`; at alloc cap → `quota_exceeded`; at system cap `systems.provision` → `quota_exceeded`; a granted request writes exactly one `reserved` row and one audit row atomically; two concurrent same-project requests serialize and cannot overshoot budget or quota (two-connection test); `set_budget`/`set_quota` refused for `operator`.

### Issue ⑤ — Lease window, renew, reconciler →expired sweep
- **Labels:** `area:allocation` · `area:core-platform`
- **Depends on:** ④
- **Goal:** Renewal and automatic reclamation of expired allocations.
- **Files:** Extend `mcp/tools/allocations.py` (renew), `reconciler/loop.py` (expiry sweep); `__main__.py` unchanged; tests.
- **Scope:**
  - `allocations.renew(allocation_id, extend)`: under `LockScope.PROJECT`, clamp to `KDIVE_LEASE_MAX` from now, re-check budget for the **added** window, write an incremental `reserved` delta, extend `lease_expiry`; over budget → `allocation_denied` (window unchanged); terminal allocation → `stale_handle`; `operator` role.
  - Reconciler `→expired` pass: select non-terminal allocations with `lease_expiry < now()`; **per allocation, under `LockScope.PROJECT → ALLOCATION`**, transition `→ expired` (audited), stamp `active_ended_at`, and write the `reconciled` credit in one transaction — the same per-Allocation lock `allocations.release` (③) takes, so the two can never double-reconcile one allocation; then hand the System to the **existing** orphaned-System teardown, which honors the M0 in-flight-job grace window (drain → force-kill) so the `→expired` flip never bypasses the drain. Idempotent; one structured-log line per reclaim. **Scope:** M1 gates the *idle* expiry path (no in-flight job) + grace-window drain of a cleanly completing job; the expiry-mid-job race is M1.5.
- **Acceptance:** `renew` extends the window and writes an incremental `reserved`; renew over budget denies and leaves the window unchanged; renew on a terminal allocation → `stale_handle`; a seeded **idle** expired allocation is moved to `expired` on one reconciler pass, its System torn down, its Run `failed(lease_expired)`, `active_ended_at` stamped, and a `reconciled` credit written; the sweep is a no-op on a second pass.

## Phase B — RBAC hardening (forks off ④)

### Issue ⑥ — Operator/admin separation + separated-role test fixtures
- **Labels:** `area:security`
- **Depends on:** ④
- **Goal:** Make the role boundary real and tested; provide the separated-principal fixtures later issues reuse.
- **Files:** Extend `security/rbac.py` call sites across `mcp/tools/*`; extend the mock-OIDC test fixture (separated principals); tests under `tests/security/`, `tests/mcp/`.
- **Scope:**
  - Pin each tool to its **lowest** sufficient role (table in [0037](../adr/0037-rbac-hardening-role-separation.md)): `accounting.set_budget`/`set_quota` → `admin`; the destructive gate's role factor → `admin` (force_crash, power off/cycle/reset, teardown); read/usage → `viewer`; lifecycle → `operator`. Every check binds to the **target object's project**, resolved per-object — `accounting.usage(investigation_id)` resolves the investigation's project before `require_role(viewer)` (no cross-project bypass).
  - Mock-OIDC fixture mints distinct `viewer`/`operator`/`admin` tokens per test project (≥ two projects, so cross-project access can be tested).
  - Negative tests: each privileged tool refuses the next-lower role (`AuthorizationError` → mapped `authorization_error`); plus a `viewer` in project A refused `usage(investigation_id)` for a B-owned investigation.
- **Acceptance:** `operator` is refused `set_budget`/`set_quota` and `force_crash` (three+ negative tests); `admin` succeeds; `viewer` may read its own project's usage but not request an allocation, and is refused a foreign project's `usage(investigation_id)`; the separated-principal fixture is importable by other test modules.

## Phase C — Swept-in M0 deferrals

### Issue ⑦ — System reprovision-in-place
- **Labels:** `area:provisioning` · `provider:local-libvirt`
- **Depends on:** ① (state edge), M0 #16 (provisioning plane)
- **Goal:** Reprovision a System in place under the same Allocation.
- **Files:** Extend `providers/local_libvirt/provisioning.py` (reprovision op + handler), `mcp/tools/systems.py` (reprovision); tests.
- **Scope:**
  - `systems.reprovision(system_id, provisioning_profile)` → `reprovision` job, `dedup_key=(system_id,"reprovision",profile_digest)`; drives `ready → reprovisioning → ready`, updating `provisioning_profile`/`target_fingerprint` on the **same** row and re-defining the domain re-tagged with the same `system_id`.
  - Contract: idempotent (profile digest), destructive, cleanup `best-effort` (interrupted → `failed`). Gate: capability scope ∧ profile opt-in ∧ **`operator`** role ([0038](../adr/0038-system-reprovision-in-place.md)). Refuse if a non-terminal Run exists (`stale_handle`).
- **Acceptance:** `systems.reprovision` cycles `ready → reprovisioning → ready` on the same `system_id` with no new System/Allocation row; re-issue with the same profile returns the existing job (dedup), a different profile is a new job; reprovision under a live Run → `stale_handle`; (live_vm) the libvirt domain is re-defined and boots the new install; `operator` may invoke it, `viewer` may not.

### Issue ⑧ — SSH Connect transport + live introspect.run
- **Labels:** `area:debug` · `provider:local-libvirt`
- **Depends on:** M0 #18 (Connect/DebugSession), #20 (offline drgn), #23 (secret backend) — no M1 accounting dep
- **Goal:** A second Connect transport (SSH) and live drgn introspection over it.
- **Files:** Extend `providers/local_libvirt/connect.py` (ssh backend + capability), `providers/local_libvirt/introspect_drgn.py` (live path), `mcp/tools/debug.py` (`transport="ssh"`, `introspect.run`); port the v1 SSH backend; tests.
- **Scope:**
  - Register capability `(connect, open_transport, local-libvirt)` advertising `kind="ssh"`; SSH backend opens a connection to the booted guest.
  - Credential by `ssh_credential_ref` (in the provisioning profile) resolved through the file-ref backend **and registered into `PROCESS_SECRET_REGISTRY` before use**; pre-registration output quarantined.
  - `debug.start_session(run_id, transport="ssh")` → `attach → live` on the ssh transport (single-attach; second attach → `transport_conflict`); `introspect.run(session_id, helper)` runs the offline drgn helper set against the **live** kernel over SSH; output redacted before persistence/response.
- **Acceptance:** dispatch selects the ssh backend by capability; (live_vm) `start_session(transport="ssh")` attaches and `introspect.run` returns task/module/sysinfo from the live guest; a planted secret value is masked in the response and the raw transcript is `sensitive`; the credential is registered before transport use (ordering test); a second ssh attach → `transport_conflict`.

## Phase D — Integration

### Issue ⑨ — M1 end-to-end integration test
- **Labels:** `type:test` · `area:core-platform`
- **Depends on:** ①–⑧
- **Goal:** Prove the eight M1 exit criteria end-to-end.
- **Files:** Create `tests/integration/test_m1_allocation_accounting.py` (marker `live_vm` for the reprovision/SSH/expiry-on-real-host portions; the budget/quota/role assertions run on the service harness without a host); extend the `live_vm` preflight with SSH reachability.
- **Setup:** seed the test project's `budgets` + `quotas` rows (mandatory now that `0002` enforces explicit limits — no silent default); this also patches the M0 walking-skeleton harness, whose project previously needed no budget.
- **Scope:** Drive: budget denial + within-budget grant with `reserved` row; alloc-quota and system-quota denials; grant→active→release with ledger reconciliation (assert `reserved`+`reconciled` sum to `rate × active_hours` = the **actual**, and that estimate=`rate × window` is asserted separately); idle lease expiry → `expired` → teardown → Run `failed(lease_expired)` → credit; renew (success + over-budget denial); role separation (operator refused admin ops, reprovision allowed for operator); reprovision-in-place cycle; live `introspect.run` over SSH with redaction.
- **Acceptance:** the spec's eight exit criteria each have an assertion.

---

## Self-review (spec coverage)

- Cost model (kcu, size-weighted, coefficients) → ②; ADR-0007. ✓
- Metering ledger (reserve/reconcile, signed deltas, rollup) → ③. ✓
- Budget + quota admission gate (PROJECT lock, check-then-debit, host-cap compose, hard-deny) → ④; ADR-0007. ✓
- set_budget/set_quota (admin) → ④ (tools) + ⑥ (role enforcement/tests). ✓
- Lease window + renew + `expired` + reconciler sweep → ⑤; ADR-0036. ✓
- RBAC operator/admin separation + negative tests → ⑥; ADR-0037. ✓
- Reprovision-in-place (`reprovisioning`, same row/allocation, operator role) → ⑦; ADR-0038. ✓
- SSH transport + live introspect.run (secret-by-ref, redaction) → ⑧; ADR-0039. ✓
- Schema delta (0002), state edges, PROJECT lock → ①. ✓
- Error taxonomy first-use (`quota_exceeded`, `allocation_denied`, `lease_expired`) → ④,⑤. ✓
- Eight exit criteria → ⑨. ✓

No spec section is unmapped.
