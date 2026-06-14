# ADR 0042 — Live-stack end-to-end functional test over MCP HTTP (M1.2)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Supersedes:** the **gated full-path tier** of
  [ADR-0035](0035-walking-skeleton-e2e-harness.md) (§1, gated portion) and amends its §3
  for the live tier. ADR-0035's **non-gated** criterion tier (#3 redaction, #4 idempotency,
  #6 gate refusal), its teardown-via-`Discovery.list_owned` assertion (#5), and its pinned
  `scripts/live-vm/*` fixtures + preflight-skip idiom **remain in force**.
- **Depends on:** the merged M0/M1 planes whose handlers this exercises unchanged
  ([ADR-0023](0023-discovery-allocation-admission.md) `allocations.*`,
  [ADR-0025](0025-provisioning-plane-libvirt.md) `systems.*`,
  [ADR-0029](0029-build-plane-local-make.md)/[ADR-0030](0030-install-boot-plane.md)
  `runs.*`, [ADR-0032](0032-connect-plane-gdbstub-debugsession.md)/[ADR-0034](0034-debug-plane-gdbmi-tier.md)
  `debug.*`, [ADR-0028](0028-control-plane-power-force-crash.md) `control.force_crash`,
  [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) `vmcore.*`,
  [ADR-0033](0033-drgn-introspection-from-vmcore.md) `introspect.from_vmcore`,
  [ADR-0036](0036-reservation-lease-semantics.md)/[ADR-0040](0040-admission-lifecycle-concurrency.md)
  accounting), plus the server transport ([ADR-0002](0002-multi-user-mcp-http.md)/[ADR-0010](0010-fastmcp-framework-auth.md))
  and OIDC/RBAC ([ADR-0006](0006-oidc-rbac-attribution.md)/[ADR-0020](0020-rbac-audit-gate-implementation.md)/[ADR-0037](0037-rbac-hardening-role-separation.md)).
- **Spec:** [`../superpowers/specs/2026-06-04-live-stack-e2e-design.md`](../archive/superpowers/specs/2026-06-04-live-stack-e2e-design.md)
  (the umbrella spec this ADR anchors; the epic's sub-issues are cut from it)

## Context

ADR-0035 wired the M0 end-to-end proof as a single gated test,
`tests/integration/test_walking_skeleton.py::test_walking_skeleton_full_path`, and left its
body to "the live_vm runner." That body was never written — it is a
`raise NotImplementedError("live_vm full-path harness wired by the live_vm runner")` behind
a fixture preflight. So today there is **no executable test that drives VM creation and
debugging end to end**, and nothing in the suite reaches the MCP tools over their actual
transport: every test (gated stub included, and all non-gated tiers) calls tool functions
in-process with a hand-built `RequestContext`. Tool registration, input/output schemas,
result-envelope serialization, the JWKS/`JWTVerifier` auth path, and the streamable-HTTP
transport are therefore unexercised end to end.

This ADR settles the cross-cutting decisions for a real, operator-run functional test that
closes both gaps: it drives the full spine **over the live MCP HTTP protocol** against a
real backing-service stack, under three distinct OIDC role tokens, on a host with real
libvirt. The work is M1.2-scale — a new wire-test harness, real token issuance, a new
read-only reporting tool, stack orchestration, and the libvirt-dependent driver — so it is
delivered as an **epic of sub-issues** (A–F in the umbrella spec); this ADR is their
convergence anchor. The per-plane behaviour is unchanged; no handler moves.

## Decision

### 1. The full-path proof runs over the live MCP HTTP protocol against a running server, not in-process handlers

The new test (`tests/integration/test_live_stack.py`) connects a real MCP client
(`fastmcp.Client`, streamable-HTTP) to a running `python -m kdive server` and drives every
step as a tool call over the wire. This is the coverage ADR-0035's in-process tier cannot
give: registration, schemas, envelope serialization, and transport framing are now on the
path. The async job kinds (`provision`, `build`, `install`, `boot`, `capture_vmcore`) are
drained by a **real** `python -m kdive worker` + `python -m kdive reconciler`, not an
inline `Worker.run_once()` — the production process topology, so the queue-drive contract
from ADR-0035 §1 is honoured by the shipping processes rather than simulated in-test.

### 2. M1.2 runs the kdive processes on the host against containerized backing services; containerizing the server is deferred to the containerized-service follow-on

The existing `docker-compose.yml` (Postgres, MinIO + `kdive-artifacts` bucket,
mock-oauth2-server) stands up the backends unchanged. `server`, `worker`, and `reconciler`
run **on the host**, pointed at those containers by env (`DATABASE_URL`, `KDIVE_S3_*`, the
OIDC issuer), using the real `local_libvirt` provider with `KDIVE_GUEST_IMAGE`/
`KDIVE_KERNEL_SRC` fixtures present. Running the processes on the host means qemu disk-image
and kernel-tree paths resolve where `libvirtd` runs, with no container↔host path
translation or socket-permission plumbing — the riskiest part of the integration is
deferred until the spine is proven green. Moving `server`/`worker`/`reconciler` into
containers with `/var/run/libvirt` mounted is **the containerized-service follow-on** (sub-issue F), specified in the
umbrella spec but not built here.

### 3. Role tokens are issued by the OIDC issuer, exercising the real JWKS/`JWTVerifier` path (amends ADR-0035 §3 for this tier only)

The driver obtains three bearer tokens — `viewer`, `operator`, `admin`, each carrying the
`roles: {<project>: <role>}` and `projects` claims `roles_from_claims` expects
(`security/authz/rbac.py`) — **from the mock-oauth2-server**, and the server validates them through
its configured `JWTVerifier` against the issuer's JWKS. This is the deliberate change from
ADR-0035 §3, which kept the mock issuer off every test path and built `RequestContext`
in-process: a wire-level test must prove the auth path the agent actually uses. ADR-0035 §3
still governs the **non-gated** criterion tier — those tests keep constructing
`RequestContext` directly and never touch the issuer.

**Open assumption, confirmed in sub-issue A before D depends on it:** that
`navikt/mock-oauth2-server` can mint the **nested-object** `roles` claim shape (not just flat
string/array claims) through its token flow. Sub-issue A's wire smoke test is the gate — it
must obtain all three tokens and have them validate through the real verifier. If the issuer
cannot produce that claim shape, A redesigns token acquisition (e.g. a claim-mapping config
or a thin token-exchange shim) before D is scheduled; this decision's host-first/real-JWKS
shape does not change, only A's mechanism.

### 4. The spine is phase-structured, so a failure names its phase

The driver advances through named phases — `allocate → provision → open-investigation →
create-run → build → install → boot → attach → crash → capture → introspect → release →
report` (the spine's full ordering, including the `investigations.open`/`runs.create`
prerequisites a `run_id` needs, is in the umbrella spec) — and records per-phase
pass/fail. A boot failure reports `boot`, not "something in the VM path." This replaces
ADR-0035's single all-or-nothing test, whose one assertion could only say the whole spine
failed.

### 5. Replace the stub; gate the new test behind a distinct `live_stack` marker

Per the project's replace-don't-deprecate rule, `test_walking_skeleton_full_path` and its
`live_vm` usage are **deleted**, not left as a placeholder. The new test carries a new
`live_stack` marker and a preflight that `pytest.skip`s (the ADR-0035 §4 idiom) unless the
VM fixtures **and** a reachable stack are present (`KDIVE_GUEST_IMAGE`, `KDIVE_KERNEL_SRC`,
`KDIVE_STACK_BASE_URL`, the OIDC issuer). A distinct marker is warranted because this tier
needs a running stack + issuer, not just a KVM host. The per-plane `live_vm` real-host
smoke tests (e.g. `test_build.py`, `test_introspect_drgn.py`) and M1's
`test_c8_live_introspect_over_ssh` keep the `live_vm` marker untouched — `live_stack` is
additive, not a rename.

### 6. The accounting report is a new server-side read-only tool, not a client-side assembly

The driver's reporting phase asserts against a new read-only `accounting.report`
(ledger-audit) MCP tool — per-allocation `reserved`/`reconciled`/variance plus a
cross-project rollup, complementing the O(1) `accounting.usage`. Putting it in the server
(rather than assembling the report in the test from `accounting.usage` calls) keeps
authorization and ledger access server-side and gives agents a durable reporting primitive.
**The tool's all-projects form is gated `platform_auditor`** (satisfied by `platform_admin`)
on the new `platform_roles` seam — a cross-project view cannot be expressed by the per-project
`admin` role without breaking tenant isolation, so it belongs to the platform-scoped tier of
[ADR-0043](0043-platform-scoped-rbac-tier.md) (which also defines a membership-gated
granted-set form this driver does not exercise). Consequently `accounting.report` is **built as
P2 of the platform-RBAC epic, not as a sub-issue of this one**; this epic's driver depends on
that work and exercises the tool with a `platform_auditor` token.

## Consequences

- A real, executable end-to-end functional test exists for the first time, and it is the
  only place the MCP HTTP transport + OIDC JWKS path are exercised end to end. The M0 exit
  proof (#1 fetchable redacted vmcore, #2 full audit attribution, #5 no orphaned domain)
  moves from "wired and skipped" to "wired and runnable on a KVM host."
- New operator obligation: bring up the stack (`docker compose up` + host `server`/`worker`/
  `reconciler`) and run the fixture scripts before `just test-live-stack`. Captured in a
  runbook (sub-issue C).
- The epic adds one product surface (`accounting.report`) and one reusable test harness
  (wire client + OIDC token helper); both are independently useful beyond this test.
- the containerized-service follow-on (server/worker/reconciler in containers + libvirt
  mount) is now a named, bounded item rather than implied scope.
- CI is unchanged: `live_stack` is deselected on `pull_request` exactly as `live_vm` is.
  Wiring this tier into a self-hosted KVM CI job is explicitly out of scope for M1.2.
- ADR-0035's status line is annotated to point its gated tier here; its non-gated tier,
  teardown assertion, and fixtures remain the governing decisions for those concerns.

## Alternatives considered

- **In-process / in-memory FastMCP client instead of a running server.** Exercises tool
  registration and schemas without a subprocess, and would run in CI. Rejected as the
  primary proof: it does not exercise transport framing, server startup, or the real JWKS
  path, and the stated goal is a production-shaped stack (real server process + Postgres +
  S3 + OIDC). The in-memory client remains available for the cheaper wire-smoke test in
  sub-issue A's CI-able tier.
- **Containerize `server`/`worker`/`reconciler` now (skip the host-first stage).** Matches
  "full container stack" literally, but forces libvirt-socket permissions and host↔container
  qemu path translation to be solved before the spine is ever green. Deferred to the containerized-service follow-on so the
  high-risk libvirt path is isolated from first proof.
- **Assemble the accounting report client-side from `accounting.usage`.** Smaller scope, no
  new tool. Rejected per the decision to keep RBAC and ledger access server-side and to give
  agents a reusable reporting primitive (decision 6).
- **Reuse the in-process `mint` helper + injected verifier for tokens.** What ADR-0035 §3
  does. Rejected for this tier because it bypasses the JWKS/`JWTVerifier` path a real agent
  uses — the exact seam a wire test exists to cover.
- **Amend ADR-0035 in place.** The ADR process forbids editing a ratified decision in place
  and prefers supersession; a new ADR keeps the decision history legible even though 0035 is
  still Proposed. Only 0035's gated tier is superseded, so 0035 is annotated, not retired.
- **Keep the `live_vm` marker and fill the stub.** Smallest change, but conflates two gates
  (a KVM host vs a running stack + issuer) and would leave the in-process-vs-wire distinction
  invisible. A distinct `live_stack` marker makes the heavier dependency explicit.
