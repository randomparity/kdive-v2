# Live-stack end-to-end functional test (MCP protocol, real libvirt) — Epic design (M1.2)

**Parent spec:** [`docs/specs/m1.2-live-stack-e2e.md`](../../specs/m1.2-live-stack-e2e.md)
(the M1.2 integration contract) · **Decisions:**
[ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) (the cross-cutting decisions this epic
realizes; supersedes the gated full-path tier of
[ADR-0035](../../adr/0035-walking-skeleton-e2e-harness.md)) · **Status:** Proposed ·
**Date:** 2026-06-04

This is an **umbrella spec**: it scopes the epic and decomposes it into sub-issues A–F. The
detailed spec and (where noted) ADR for each sub-issue are authored when that issue is
scheduled — each sub-issue gets its own spec → plan → implementation cycle. GitHub issues
are cut from this document after it is reviewed.

## Goal

Stand up the first executable end-to-end functional test for kdive: drive the full spine —
allocate a VM, provision it on real libvirt, build/install/boot a kernel, attach a debugger,
crash the guest, capture and introspect the vmcore, release — **over the live MCP HTTP
protocol**, against a real backing-service stack, under three distinct OIDC role tokens
(`viewer`/`operator`/`admin`), ending in an accounting report. This closes two gaps at once:
no executable VM end-to-end test exists today (the M0 proof is a `NotImplementedError`
stub), and nothing exercises the MCP tools over their real transport + auth path.

## Non-goals

- **No CI integration in M1.2.** The test is operator-run on a KVM host. Wiring a
  self-hosted CI job is out of scope (ADR-0042 Consequences).
- **No containerized kdive server in M1.2.** `server`/`worker`/`reconciler` run on the host
  against containerized backends; containerizing them is the containerized-service follow-on (sub-issue F).
- **No new product code in this epic.** The driver exercises shipped handlers unchanged; the
  one new tool it needs, `accounting.report`, is built in the platform-RBAC epic (P2) and
  consumed here.
- **No new fixtures.** Reuses ADR-0035's `scripts/live-vm/*` (pinned kernel tree + guest
  image) and the preflight-skip idiom.

## What already exists vs what this epic builds

| Concern | Status today | This epic |
|---|---|---|
| Postgres / MinIO (S3) / mock-OIDC | in `docker-compose.yml` | reused unchanged |
| Object store (`ObjectStore`, boto3, `KDIVE_S3_*`) | S3-only, working | reused |
| RBAC viewer/operator/admin + per-project JWT claims | working (`security/rbac.py`, `mcp/auth.py`) | reused |
| Accounting model + `accounting.usage`/`estimate`/`set_budget`/`set_quota` | working | reused; `accounting.report` comes from the platform-RBAC epic (P2) |
| Server entrypoint `python -m kdive server` (HTTP) + `worker`/`reconciler` | working | run on host against the stack (C) |
| MCP **client** in tests | none — all tests call handlers in-process | new wire harness (A) |
| OIDC token issuance for tests | in-process `mint` + injected verifier only | real issuer tokens (A) |
| Full-path VM test | `NotImplementedError` stub | phase-structured wire driver (D), replacing the stub |

## Topology (M1.2)

```
  containers (docker-compose, unchanged)        host processes (real local_libvirt)
  ┌───────────────────────────────┐             ┌──────────────────────────────┐
  │ postgres:17                    │◄────────────│ python -m kdive server  :8000│◄── fastmcp.Client
  │ minio + minio-init (bucket)    │◄────────────│ python -m kdive worker       │      (test driver)
  │ mock-oauth2-server  :8090      │──tokens────►│ python -m kdive reconciler   │
  └───────────────────────────────┘             └──────────────┬───────────────┘
                                                                │ libvirt (qemu:///system)
                                                                ▼  real VM: build→boot→crash→vmcore
```

The worker + reconciler are mandatory: `provision`/`build`/`install`/`boot`/`capture_vmcore`
are async job kinds, so a driver that calls `provision_system` then `start_session` blocks
until the worker drains the queue (ADR-0035 §1 queue-drive contract, now via the shipping
processes per ADR-0042 §1).

## The spine and its phases

The driver advances through named phases and records per-phase pass/fail (ADR-0042 §4). Each
phase is one or more tool calls over the wire under a specific **caller** role. The async
job-kind phases (`build`, `install`, `boot`, `capture`) are **enqueued** by the caller's tool
call and then **executed by the worker**; the driver polls the corresponding read tool
(`runs.get` / `systems.get`) until the step commits `succeeded` before the next dependent
phase (the queue-drive contract, ADR-0042 §1). Each phase threads its output id into the
phases that follow.

**Setup prerequisite for the `crash` phase.** `control.force_crash_system` is behind the
three-check destructive gate (ADR-0028/ADR-0035, `mcp/tools/control.py`): it requires the
`admin` role, a destructive **capability scope** on the request context, **and** the
provisioning profile to opt in by listing `force_crash` in
`provider.local_libvirt.destructive_ops`. So the `provision` phase must use such a profile
and the run must execute under a context carrying the destructive scope — otherwise `crash`
returns `authorization_denied` and the spine dies before `capture`. This is setup the driver
establishes up front, not something the crash phase discovers.

| Phase | Caller (role) | Tool(s) | Output → / asserts |
|---|---|---|---|
| allocate | operator | `allocations.request_allocation` | → `allocation_id`; ledger `reserved` row |
| provision | operator | `systems.provision_system` (profile **opts into `force_crash`**, see above) → worker; poll `systems.get` | → `system_id`; real domain exists |
| open-investigation | operator | `investigations.open` | → `investigation_id` |
| create-run | operator | `runs.create(investigation_id, system_id, build_profile)` | → `run_id` |
| build | operator → worker | `runs.build(run_id)` (enqueue); poll `runs.get` to `succeeded` | kernel built from `KDIVE_KERNEL_SRC`; `build_id` recorded |
| install | operator → worker | `runs.install(run_id)` (enqueue); poll `runs.get` | kernel installed to domain |
| boot | operator → worker | `runs.boot(run_id)` (enqueue); poll `runs.get` | guest boots |
| attach | operator | `debug.start_session(run_id, "gdbstub")` + `debug.gdb_mi_command` probe | → `session_id`; MI responds |
| crash | admin | `control.force_crash_system(system_id)` | 3-check gate passes (admin role + capability scope + profile opt-in); guest panics → kdump |
| capture | operator → worker | `vmcore.fetch(system_id)` (enqueue); poll | **redacted** vmcore artifact in MinIO |
| introspect | operator | `introspect.from_vmcore(run_id)` | redacted report returned |
| release | operator | `allocations.release_allocation(allocation_id)` | ledger `reconciled`; teardown |
| report | platform_auditor (`report`) · viewer (`usage`) | `accounting.report` (platform-RBAC P2) + `accounting.usage(investigation_id=…)` (viewer) | the all-projects form reachable over the wire under a `platform_auditor` token and denied to a project-only token; returns this run's spend; artifact written. (Multi-project rollup correctness and the granted-set form are platform-RBAC P2's in-process tests, not D — the spine runs one project.) |

## Exit criteria

Reuses ADR-0035 #1/#2/#5, adds wire/RBAC/accounting:

- **Protocol:** every step returns a well-formed MCP envelope **over HTTP** (the new
  coverage); tokens validate through the real JWKS/`JWTVerifier` path.
- **#1** a fetchable redacted vmcore artifact exists in MinIO at the end.
- **#2** an `audit_log` row for every transition + the `force_crash`, under the request's
  `(principal, agent_session, project)` tuple.
- **#5** after release the System is `torn_down` **and** `Discovery.list_owned()` returns no
  `OwnedInfra` for the released `system_id` (ADR-0035 §2 mechanism, retained).
- **RBAC negatives:** a `viewer` token is denied operator/admin operations; `force_crash` is
  denied without the `admin` role — asserted over the wire.
- **#3 redaction:** a planted secret does not leak through the wire path (transcript +
  artifact-sensitivity, ADR-0035 §1(a)/(b)).
- **Accounting:** the ledger carries `reserved` + `reconciled`; `budget_remaining` reflects
  spend; `accounting.report`'s **all-projects** form (`platform_auditor`) is reachable over the
  wire, returns this run's spend, and is denied to a project-only `viewer`/`operator`/`admin`
  token. The spine runs a single project, so D does **not** validate the multi-project rollup
  or the membership-gated granted-set form — that correctness is covered by platform-RBAC P2's
  in-process tests (≥2 projects); D proves only wire reachability and the platform-vs-project
  authorization boundary.

## Decomposition (sub-issues)

Build order follows the dependency arrows. A and C carry no VM dependency and are CI-able on
their own; D, E require the KVM host. (Sub-issue B — `accounting.report` — was relocated to
the platform-RBAC epic; see below.)

### A — MCP-over-HTTP test harness + OIDC token issuance  *(spec + light ADR)*
A reusable `fastmcp.Client` wrapper and a helper that obtains `viewer`/`operator`/`admin`
tokens (per-project `roles`/`projects` claims) **and a `platform_auditor` token** (the
`platform_roles` claim, for the `report` phase) from the mock-oauth2-server, plus a thin
**wire smoke test** (connect → `list_tools` → one read-only call per role) that runs against
a server + Postgres + issuer with no VM. **Depends on:** nothing. **ADR:** light — records
the token-issuance approach and the wire-harness boundary. **Acceptance:** the issuer mints
the **nested-object `roles` claim** (`{<project>: <role>}`) `roles_from_claims` expects, the
**`platform_roles` array claim** `platform_roles_from_claims` expects, and all tokens
validate through the server's real verifier (this is the gate that confirms
the open assumption in ADR-0042 §3); the smoke test passes against a locally-run stack; the
harness is importable by D. **If the issuer cannot mint that claim shape, A redesigns token
acquisition before D is scheduled.**

### B — `accounting.report` ledger-audit tool  *(relocated to the platform-RBAC epic)*
**This sub-issue moved.** A cross-project report needs an actor the per-project RBAC model
cannot express (a per-project `admin` cannot legitimately span projects — see
[ADR-0043](../../adr/0043-platform-scoped-rbac-tier.md) and its spec
`2026-06-04-platform-rbac-tier-design.md`). `accounting.report` is therefore built as
**P2 of the platform-RBAC epic**, gated `platform_auditor` (satisfied by `platform_admin`),
on the new `platform_roles` seam (P1). The live-stack driver (sub-issue D) **depends on
P1+P2** and drives the tool with a `platform_auditor` token. No `accounting.report` work
lands in this epic.

### C — Stack orchestration + runbook  *(spec section only, no ADR)*
`just stack-up` / `just test-live-stack` recipes and the host `server`+`worker`+`reconciler`
env wiring against the existing compose; a runbook in `docs/` + an `AGENTS.md` pointer.
**Depends on:** nothing (ops/docs only). **Acceptance:** a documented one-command bring-up;
`just test-live-stack` runs the `live_stack` suite (skipping cleanly when fixtures/stack
absent).

### D — Phase-structured spine driver (replaces the stub)  *(spec; anchored by ADR-0042)*
`tests/integration/test_live_stack.py` driving the full spine over the wire via A's harness;
new `live_stack` marker + preflight; **deletes** `test_walking_skeleton_full_path`. Asserts
the protocol/#1/#2/#5/RBAC/#3 exit criteria. **Depends on:** A, C. **Acceptance:** on a KVM
host with fixtures + stack, the full spine reaches `release` and every exit criterion holds;
a failure names its phase.

### E — Accounting assertions + report artifact  *(separate sub-issue — decided)*
The `report` phase: drive the `accounting.report` tool (platform-RBAC **P2**) with a
`platform_auditor` token, assert ledger `reserved`/`reconciled`/variance, emit the report
artifact. **Kept a separate sub-issue/commit from D** (not folded in) for bisectability — an
accounting-assertion regression bisects to E, not to the libvirt driver. **Depends on:**
platform-RBAC P1+P2, D. **Acceptance:** the report reflects the run's real spend and is
written as an artifact.

### F — Containerize `server`/`worker`/`reconciler` + libvirt mount  *(deferred follow-on)*
Move the three processes into compose with `/var/run/libvirt` mounted and host qemu/kernel
paths made resolvable. A deferred follow-on phase of M1.2 (not a separate founding milestone;
could be promoted to ~M1.6 if it warrants one). **Depends on:** D green. Scoped here, **not
built in M1.2's first pass**.

## Dependency graph

```
  A ─┐
     ├─► D ─► E
  C ─┘        ▲
  platform-RBAC P1 ─► P2 ─┘   (external epic; accounting.report)
              D (green) ─► F   (the containerized-service follow-on, deferred)
```

## Risks

- **libvirt path/permission seams** (the reason for host-first staging) — surface in D, not
  hidden behind a container until the containerized-service follow-on.
- **OIDC claim shape** — the issuer must mint the exact `roles`/`projects` claims **and the
  `platform_roles` array claim** the server expects; de-risked early in A's smoke test before
  D needs it.
- **Async-job timing** — the driver must drive jobs to `succeeded` via the real worker before
  dependent calls; phase structure makes a stall localizable.
