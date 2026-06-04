# Architecture Decision Records

This directory records the load-bearing architecture decisions for the KDIVE
production rewrite. The top-level design (`../specs/top-level-design.md`) lists
nine core decisions and states that each "should become an ADR before
implementation"; those ADRs live here.

## Process

- One decision per file, named `NNNN-kebab-title.md` with a zero-padded,
  monotonic number (`0001`, `0002`, …). Numbers are never reused.
- Copy `0000-template.md` to start a new ADR.
- Open it as **Proposed**, move it to **Accepted** once ratified, and to
  **Superseded by NNNN** when a later ADR replaces it (never edit an accepted
  decision in place — write a new ADR that supersedes it).

## Status lifecycle

```
Proposed → Accepted → Superseded by NNNN
                   ↘ Rejected
```

## Style

The project doc-style guard applies here too: use **Milestone**, not "Sprint",
and keep prose plain and factual (no "critical", "robust", "comprehensive").

## Index

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](0001-greenfield-rewrite.md) | Greenfield rewrite, Python | Proposed |
| [0002](0002-multi-user-mcp-http.md) | Multi-user service; MCP over streamable HTTP | Proposed |
| [0003](0003-six-durable-objects.md) | Six durable objects replace the run-centric model | Proposed |
| [0004](0004-first-slice-local-libvirt.md) | First slice targets local libvirt/QEMU | Proposed |
| [0005](0005-postgres-object-store-state.md) | Postgres + object store for state; advisory locks | Proposed |
| [0006](0006-oidc-rbac-attribution.md) | OIDC/SSO + RBAC with (principal, agent_session) | Proposed |
| [0007](0007-metering-budgets-admission.md) | Metering + budgets/quotas with admission control | Proposed |
| [0008](0008-async-worker-tier-job-queue.md) | Async worker tier + durable job queue | Proposed |
| [0009](0009-capability-provider-dispatch.md) | Capability-based provider dispatch | Proposed |
| [0010](0010-fastmcp-framework-auth.md) | FastMCP server framework + streamable-HTTP auth | Proposed |
| [0011](0011-provisioning-profile-schema.md) | Provisioning-profile schema | Proposed |
| [0012](0012-secret-backend.md) | Secret backend (file-ref for M0) | Proposed |
| [0013](0013-object-store-layout-retention.md) | Object-store layout & retention | Proposed |
| [0014](0014-structured-logging.md) | Structured logging via stdlib `logging` + `contextvars` | Proposed |
| [0015](0015-sql-migration-runner.md) | Forward-only SQL migration runner | Proposed |
| [0016](0016-repository-layer-locks-idempotency.md) | Repository layer, advisory locks, idempotency ledger | Proposed |
| [0017](0017-object-store-client-interface.md) | Object-store client interface & failure contract | Proposed |
| [0018](0018-job-queue-worker-execution.md) | Job-queue enqueue/dequeue + worker execution contract | Proposed |
| [0019](0019-tool-response-envelope.md) | Uniform tool-response envelope | Proposed |
| [0020](0020-rbac-audit-gate-implementation.md) | RBAC roles, audit record, destructive-op gate (M0 shapes) | Proposed |
| [0021](0021-reconciler-loop-drift-repair.md) | Reconciler loop: drift-repair seam, leaked-domain reaping, lease-expiry compensation | Proposed |
| [0022](0022-capability-registry-dispatch-impl.md) | Capability registry & dispatch implementation shapes (refines 0009) | Proposed |
| [0023](0023-discovery-allocation-admission.md) | Discovery registration & per-host allocation admission (M0) | Proposed |
| [0024](0024-provisioning-profile-model-shape.md) | Provisioning-profile model shape (M0, refines 0011) | Proposed |
| [0025](0025-provisioning-plane-libvirt.md) | Provisioning plane: System creation & teardown on local libvirt (M0) | Proposed |
| [0026](0026-investigation-run-lifecycle.md) | Investigation + Run lifecycle & tools (M0) | Proposed |
| [0027](0027-safety-modules-secret-backend-impl.md) | Safety modules & file-ref secret backend (impl, refines 0012) | Proposed |
| [0028](0028-control-plane-power-force-crash.md) | Control plane: power + force_crash on local libvirt (M0) | Proposed |
| [0029](0029-build-plane-local-make.md) | Build plane (local make): runs.build, BuildProfile, build handler (M0) | Proposed |
| [0030](0030-install-boot-plane.md) | Install + boot plane (local libvirt): runs.install, runs.boot, install/boot handlers (M0) | Proposed |
| [0031](0031-retrieve-plane-vmcore-postmortem.md) | Retrieve plane: vmcore capture/fetch + crash postmortem (M0) | Proposed |
| [0032](0032-connect-plane-gdbstub-debugsession.md) | Connect plane (gdbstub) + DebugSession lifecycle (M0) | Proposed |
| [0034](0034-debug-plane-gdbmi-tier.md) | Debug plane: gdb-MI tier (breakpoints, read_memory cap, read_registers, continue/interrupt) (M0) | Proposed |
