# Phase-structured spine driver — sub-issue D design (M1.2, #100)

**Parent (umbrella) spec:** [`2026-06-04-live-stack-e2e-design.md`](2026-06-04-live-stack-e2e-design.md)
(sub-issue D) · **Decisions:** [ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) §1/§4/§5
(the cross-cutting decisions this driver realizes) + [ADR-0045](../../adr/0045-spine-driver-capability-grant-phase-naming.md)
(the two driver-local decisions: out-of-band capability grant and the phase-failure naming
contract) · **Depends on:** A ([#98](https://github.com/randomparity/kdive/issues/98), merged —
`tests/integration/live_stack/harness.py`), C ([#99](https://github.com/randomparity/kdive/issues/99),
merged — `just stack-up`/`just test-live-stack`, the `live_stack` marker, the runbook) ·
**Status:** Proposed · **Date:** 2026-06-04

## Goal

Build `tests/integration/test_live_stack.py`: a single phase-structured test that drives the
full kdive spine **over the live MCP HTTP transport** via A's `LiveStackClient`/`mint_token`,
each phase a tool call under a specific OIDC role token, the async job kinds drained by the
real host `worker` + `reconciler`. It asserts the protocol/#1/#2/#3/#5/RBAC exit criteria and,
on any failure, **names the phase that failed**. It replaces (deletes) the unimplemented
`test_walking_skeleton_full_path` stub. It carries the `live_stack` marker and a preflight that
skips cleanly when the stack/fixtures are absent, so it is safe in CI and on any host.

This is a **test-only** change: no `src/` product code moves. The driver exercises shipped
handlers unchanged.

## Non-goals

- **No `report` phase accounting assertions.** The umbrella spec splits the `report` phase's
  `accounting.report` ledger assertions + report artifact into sub-issue E
  ([#101](https://github.com/randomparity/kdive/issues/101)) for bisectability. This driver
  proves only the **RBAC reachability boundary** the issue lists: `accounting.report`'s
  all-projects form is reachable over the wire under a `platform_auditor` token and **denied**
  to a project-only token. The ledger-variance assertions and the emitted report artifact are E.
- **No CI execution.** Operator-run on a KVM host with the stack up (ADR-0042 Consequences).
  The marked suite skips cleanly everywhere else.
- **No new product code, no new fixtures.** Reuses the ADR-0035 `scripts/live-vm/*` fixtures
  and the existing handlers/tools.

## The spine and its phases

The driver advances through the phases below in order, each phase one or more tool calls over
HTTP under the named caller role; each phase records pass/fail and threads its output id into
the phases that follow. The async job-kind phases (`provision`/`build`/`install`/`boot`/
`capture`) are **enqueued** by the caller's tool call (which returns a *job handle* envelope,
status `queued`) and **executed by the real host worker**; the driver polls `jobs.wait` (then
the read tool `runs.get`/`systems.get`) until the step commits before the next dependent phase
— the queue-drive contract (ADR-0042 §1).

| # | Phase | Caller (role) | Tool(s) over the wire | Output → / asserts |
|---|---|---|---|---|
| 1 | allocate | operator | `allocations.request` | → `allocation_id`; envelope `status=granted` |
| — | *(grant capability scope)* | *(platform setup)* | *out-of-band DB update* | `allocations.capability_scope.destructive_ops=["force_crash"]` (ADR-0045 §1) |
| 2 | provision | operator | `systems.provision(allocation_id, profile)` — profile **opts `force_crash` in** via `provider.local-libvirt.destructive_ops` → worker; poll `systems.get` to `ready` | → `system_id` (from the provision envelope's `data["system_id"]`, **not** `object_id`, which is the job id) |
| 3 | open-investigation | operator | `investigations.open(project, title)` | → `investigation_id` |
| 4 | create-run | operator | `runs.create(investigation_id, system_id, build_profile)` | → `run_id` |
| 5 | build | operator → worker | `runs.build(run_id)` (enqueue); poll `jobs.wait` to `succeeded` | `run` reaches `succeeded`; `build_id` recorded |
| 6 | install | operator → worker | `runs.install(run_id)` (enqueue); poll `jobs.wait` | kernel installed |
| 7 | boot | operator → worker | `runs.boot(run_id)` (enqueue); poll `jobs.wait` | guest boots |
| 8 | attach | operator | `debug.start_session(run_id, "gdbstub")` then `debug.read_registers` probe | → `session_id`; the gdb-MI probe returns a non-error envelope |
| 9 | crash | admin | `control.force_crash(system_id)` (enqueue); poll `systems.get` to `crashed` | 3-check gate passes (admin role ∧ capability scope ∧ profile opt-in) |
| 10 | capture | operator → worker | `vmcore.fetch(system_id)` (enqueue); poll `jobs.wait` to `succeeded` | **redacted** vmcore artifact in MinIO (#1) |
| 11 | introspect | operator | `introspect.from_vmcore(run_id)` | redacted report returned in `data.report` |
| 12 | release | operator | `allocations.release(allocation_id)` | ledger `reconciled`; teardown enqueued |
| — | *(await teardown)* | operator → worker | poll `systems.get` to `torn_down` | System `torn_down`; `Discovery.list_owned()` empty for `system_id` (#5) |
| 13 | report (RBAC boundary only) | platform_auditor + viewer | `accounting.report(scope="all-projects")` under a `platform_auditor` token (reachable) **and** under a project-only token (denied); `accounting.usage(investigation_id=…)` under viewer | the platform-vs-project authorization boundary holds over the wire (E owns the ledger assertions) |

The `attach` probe uses `debug.read_registers` (a gdb-MI command) rather than the umbrella
spec's placeholder `debug.gdb_mi_command`; the registered MI tools are
`debug.read_registers`/`debug.read_memory`/`debug.continue`/`debug.interrupt`/breakpoints.

**Id threading.** The async tools (`systems.provision`, `runs.build`/`install`/`boot`,
`vmcore.fetch`, `control.force_crash`) return a **job-handle** envelope whose `object_id` is the
**job id**; the System/Run id is carried in `data` (`data["system_id"]` / `data["run_id"]`). So
the provision phase threads `system_id` from `data["system_id"]`, and the `jobs.wait`-polled
phases use `object_id` as the job id. The synchronous tools (`allocations.request`,
`investigations.open`, `runs.create`) carry the created object's id in `object_id` directly.

### Setup prerequisite for `crash` (the capability scope) — ADR-0045 §1

`control.force_crash` is behind the three-check destructive gate
(`kdive.security.gate.assert_destructive_allowed`): admin role **and** the controlling
allocation's `capability_scope.destructive_ops` granting `force_crash` **and** the provisioning
profile opting `force_crash` in. The wire `allocations.request` tool always grants an **empty**
capability scope (`allocation_admission._grant` hardcodes `capability_scope={}`), and no shipped
tool sets it — granting a destructive capability is a privileged platform action outside the
per-project operator surface. So the driver establishes the scope **up front, out of band**: a
single privileged DB `UPDATE allocations SET capability_scope = … WHERE id = <allocation_id>`
against the same Postgres the stack uses (`KDIVE_DATABASE_URL`), mirroring exactly what
`seed_granted_allocation(capability_scope=…)` does in the in-process gate tests. This is setup
the driver performs deterministically before `crash`, not something the crash phase discovers
(ADR-0045 §1 records the rationale and the rejected alternatives).

The profile opt-in (the third gate factor) is carried in the provision phase's `profile` dict:
`provider.local-libvirt.destructive_ops = ["force_crash"]`, validated by `LibvirtProfile`.

## Phase-failure naming contract (ADR-0042 §4, mechanism in ADR-0045 §2)

Each phase runs inside a `phase(name)` context manager that, on any exception, re-raises a
`SpinePhaseError(phase=name)` chaining the original (`raise … from exc`). The test body is a
linear sequence of `async with phase("provision"): …` blocks, so a failure's message and the
chained traceback both name the failing phase — `provision`, not "something in the VM path"
(ADR-0042 §4). A tool envelope returned with `status in {"error","failed"}` inside a phase is
converted to a raised `SpinePhaseError` carrying the envelope's `error_category`, so an
authorization/infrastructure denial is reported under its phase too, not silently passed over.

## Async drain (worker + reconciler)

The real host `worker` drains `provision`/`build`/`install`/`boot`/`capture_vmcore`/
`force_crash` jobs; the `reconciler` repairs drift and reaps released infra. The driver never
runs a job inline. Two poll mechanisms, used deliberately for different signals:

- **`jobs.wait` poll (run-step signal): `build`/`install`/`boot`/`capture`.** These phases carry
  a job id (the enqueue envelope's `object_id`) whose terminal state is the step's outcome. The
  driver polls `jobs.wait(job_id, timeout_s)` and must distinguish its **three** outcomes
  (`wait_job` returns `ToolResponse.from_job(job)` when the job is terminal — `succeeded`,
  `failed`, or `canceled` — **or** when the clamped `MAX_WAIT_S=300` server deadline elapses, in
  which case it returns a still-`queued`/`running` envelope):
  1. `status == "succeeded"` → the step committed; proceed to assert the read tool
     (`runs.get` `succeeded`).
  2. `status in {"failed","canceled"}` → raise `SpinePhaseError(phase, error_category)` carrying
     the job envelope's `error_category`. **A failed job is never treated as not-yet-done.**
  3. a non-terminal return (`queued`/`running`) → the server's 300s cap elapsed with the worker
     still draining (a stall). Re-issue `jobs.wait` until the module-level `_DRAIN_DEADLINE_S`
     budget expires, then raise a timeout `SpinePhaseError(phase, reason="drain_timeout")`. The
     loop **never** spins forever.
- **`systems.get` poll (system-state signal): `provision`/`crash`/teardown.** These phases move
  *System* state, not run-step state. `provision` drives `provisioning → ready`, `crash`
  (`force_crash`) drives `ready → crashed`, and `release`'s teardown drives `→ torn_down`. The
  driver polls `systems.get(system_id)` and reads `status` (the System state) until it reaches
  the target state, bounded by the same `_DRAIN_DEADLINE_S`; a `failed`/`error` envelope or the
  deadline raises the phase's `SpinePhaseError`. `crash` polls `systems.get` (not `jobs.wait`)
  because the System-state transition is the observable signal and because the `force_crash` job
  is authorized under the **admin** token while the polling driver may hold an operator context
  — see the single-project invariant below.

`_DRAIN_DEADLINE_S` is set comfortably above the 300s `MAX_WAIT_S` server cap (e.g. a small
multiple) so a single `jobs.wait` returning a non-terminal envelope is one tick of the retry
loop, not the whole budget; a real worker stall exhausts the budget and fails the phase by name.

### Single-project invariant (cross-role job polling)

All spine role tokens — `operator` and `admin` — carry the **same** project string. `jobs.wait`
/`jobs.get` are project-scoped: `_in_scope` (`jobs.py`) gates a job read on the job's
`authorizing.project` being in the **caller's** `ctx.projects`. So a job enqueued under one role
is `jobs.wait`-readable under another **only** when both tokens share the project. The driver
keeps this invariant: every spine phase runs in one project, and the `crash` phase is polled via
`systems.get` (no cross-role `jobs.wait` on the admin-authorized `force_crash` job). A future
multi-project variant must revisit this.

## Acceptance assertions

All assertions run over the wire / against the stack's Postgres + MinIO:

- **Protocol.** Every phase's tool call returns a well-formed `ToolResponse` envelope parsed by
  `LiveStackClient.call_tool` (the structured-content path). Tokens are minted by `mint_token`
  from the real issuer and validated through the server's `JWTVerifier`/JWKS — proven by the
  calls succeeding under role tokens (an invalid token is rejected at the transport, never
  reaching a handler).
- **#1 redacted vmcore in MinIO.** After `capture`, `vmcore.list(system_id)` /
  `artifacts.list(system_id)` over the wire return a fetchable **redacted** artifact, and
  `artifacts.get` on it succeeds; the raw `sensitive` key is never returned (the artifact-
  sensitivity guard, mirrors the in-process `test_raw_vmcore_is_sensitive_and_unreachable`).
- **#2 audit per transition + force_crash.** The stack Postgres `audit_log` carries a row for
  every transition the spine drove — `->granted`, provision/run transitions, `ready->crashed`
  (the `force_crash`), release — each under the request's `(principal, agent_session, project)`
  tuple. Asserted by querying `audit_log` for the `force_crash` row and the release rows under
  the driver's principal/session/project.
- **#3 redaction.** The introspect report (`introspect.from_vmcore`) and any returned crash
  transcript do not leak a planted secret over the wire; `[REDACTED]` appears where the secret
  was. The fixture guest plants the secret (operator fixture concern); the driver asserts the
  wire path does not surface it.
- **#5 torn_down + no OwnedInfra.** After `release` and teardown drain, `systems.get(system_id)`
  reports `torn_down` **and** a `LocalLibvirtDiscovery(host_uri="qemu:///system", …).list_owned()`
  returns no `OwnedInfra` for the released `system_id` (the ADR-0035 §2 mechanism).
- **RBAC negatives over the wire — two distinct mechanisms, asserted differently.** The two
  negatives the issue lists surface through **different** wire paths and must be asserted
  separately (a single "raised/error envelope" assertion is wrong for one of them):
  - **(a) Raised path — `viewer` denied operator/admin ops.** `require_role`
    (`security/rbac.py`) **raises** `AuthorizationError` for a viewer calling
    `allocations.request`/`systems.provision` (ADR-0020: authz denials raise, there is **no**
    authz `ErrorCategory`). The server has no `ToolResponse` to return; fastmcp surfaces the
    raised exception as a **tool error** (`CallToolResult.is_error == True`), not a structured
    envelope. The merged `LiveStackClient.call_tool` reads `result.structured_content`, which is
    `None` on a raised error, so the harness must be **extended** (see below) to surface tool
    errors as an assertable typed outcome rather than the current opaque
    `RuntimeError("...returned no structured content")`. The driver asserts this case via the
    harness's tool-error surface (a raised `LiveStackToolError` / a returned `is_error`), **not**
    by inspecting `error_category`.
  - **(b) Envelope path — `operator` denied `control.force_crash`.** `force_crash`'s gate
    **catches** `DestructiveOpDenied`, audits the denial, and **returns**
    `ToolResponse.failure(system_id, AUTHORIZATION_DENIED)` (`control.py`). This is a structured
    envelope, so the driver asserts `status == "error"` **and**
    `error_category == "authorization_denied"`. (Force_crash returns an envelope rather than
    raising because the gate must audit the denied attempt before responding.)

  These negatives run against the standing stack and do **not** require a KVM host beyond the
  spine itself (they can be asserted before/independent of the VM path).

  **Harness extension (additive, must not break existing callers).** `LiveStackClient.call_tool`
  is extended to detect a tool error (`CallToolResult.is_error`) and raise a typed
  `LiveStackToolError` (carrying the tool name + the error text) **before** the
  structured-content parse. The existing success path — `structured_content` → `ToolResponse`
  (scalar) or `{"result":[...]}` → `list[ToolResponse]` — is unchanged, so the wire smoke test
  (`test_wire_harness.py`) and every other caller that relies on envelope parsing keep working.
  The `RuntimeError("returned no structured content")` branch remains only for the genuinely
  malformed (non-error, no-structured-content) case it was written for.
- **report RBAC boundary.** `accounting.report(scope="all-projects")` is reachable under a
  `platform_auditor` token and denied under a project-only `viewer`/`operator`/`admin` token
  (sub-issue E owns the ledger-variance + artifact assertions).

## Preflight / skip semantics

A module-level `_spine_preflight()` (the ADR-0035 §4 idiom, extended) skips with an actionable
reason unless **all** of: `KDIVE_GUEST_IMAGE` + `KDIVE_KERNEL_SRC` present (the VM fixtures);
`KDIVE_STACK_BASE_URL` set + the server reachable; the OIDC issuer reachable
(`require_issuer()`); and `KDIVE_DATABASE_URL` set (the out-of-band capability grant + the
audit/teardown assertions read it). Each missing prerequisite produces a distinct
`pytest.skip` naming the exact fix (the script to run / the env to set / `just stack-up`). The
suite carries the `live_stack` marker, so `just test` (`-m "not live_vm and not live_stack"`)
never collects it and `just test-live-stack` runs it (skipping cleanly when the preflight
fails).

## Files

- **Create** `tests/integration/test_live_stack.py` — the phase-structured driver + `phase`
  context manager + `SpinePhaseError` + `_spine_preflight` + the assertions above. Marked
  `live_stack`.
- **Modify** `tests/integration/live_stack/harness.py` — extend `LiveStackClient.call_tool` to
  raise a typed `LiveStackToolError` on `CallToolResult.is_error` (the raised-RBAC path),
  additively, leaving the existing structured-content envelope parsing unchanged.
- **Modify** `tests/integration/test_walking_skeleton.py` — **delete**
  `test_walking_skeleton_full_path` and its now-unused `live_vm` preflight branch
  (`_live_vm_preflight` is retained only if still used by a remaining test; the non-gated
  exit-criterion tests stay untouched). The `live_vm` marker on the per-plane smoke tests is
  untouched (`live_stack` is additive, ADR-0042 §5).
- **Create** `docs/adr/0045-spine-driver-capability-grant-phase-naming.md` + add it to
  `docs/adr/README.md`.

## Risks

- **libvirt path/permission seams** surface here first (the reason for host-first staging,
  ADR-0042 §2); a provision/boot/teardown failure names its phase.
- **Async-job timing** — bounded per-phase deadlines make a worker stall a named-phase timeout,
  not a hang.
- **Capability-scope coupling** — the out-of-band grant is a test-side privileged action; if a
  future release adds a real `allocations.grant_capability` tool, the driver should switch to it
  (ADR-0045 §1 Consequences).
