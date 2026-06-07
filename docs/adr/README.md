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
| [0033](0033-drgn-introspection-from-vmcore.md) | Debug plane: drgn introspection from vmcore (offline) (M0) | Proposed |
| [0034](0034-debug-plane-gdbmi-tier.md) | Debug plane: gdb-MI tier (breakpoints, read_memory cap, read_registers, continue/interrupt) (M0) | Proposed |
| [0035](0035-walking-skeleton-e2e-harness.md) | Walking-skeleton end-to-end integration-test harness (M0) | Proposed |
| [0036](0036-reservation-lease-semantics.md) | Reservation / lease semantics (M1) | Proposed |
| [0037](0037-rbac-hardening-role-separation.md) | RBAC hardening: operator/admin separation (M1) | Proposed |
| [0038](0038-system-reprovision-in-place.md) | System reprovision-in-place (M1) | Proposed |
| [0039](0039-ssh-transport-live-introspection.md) | SSH transport + live drgn introspection (M1) | Proposed |
| [0040](0040-admission-lifecycle-concurrency.md) | M1 admission & lifecycle concurrency: lock hierarchy, request idempotency, atomic reconciliation | Proposed |
| [0041](0041-versioning-release-process.md) | Versioning policy (SemVer, milestone→minor) & tag-driven release process | Proposed |
| [0042](0042-live-stack-e2e-mcp-http.md) | Live-stack end-to-end functional test over MCP HTTP; supersedes the gated tier of 0035 (M1.2) | Proposed |
| [0043](0043-platform-scoped-rbac-tier.md) | Platform-scoped RBAC tier (`platform_roles`); cross-project auditor surface (extends 0006) | Proposed |
| [0044](0044-mcp-wire-harness-oidc-token-issuance.md) | MCP-over-HTTP wire harness + OIDC token issuance; closes ADR-0042 §3's claim-shape gate (M1.2) | Proposed |
| [0045](0045-spine-driver-capability-grant-phase-naming.md) | Spine driver: out-of-band destructive-capability grant + phase-failure naming contract (M1.2, refines 0042 §4) | Proposed |
| [0046](0046-spine-report-phase-accounting-assertions-artifact.md) | Spine `report` phase: accounting assertions + report artifact (M1.2, refines 0042 §6) | Proposed |
| [0047](0047-agent-facing-tool-guide-generation.md) | Agent-facing tool-guide generation | Proposed |
| [0048](0048-external-build-artifact-ingestion.md) | External-build artifact ingestion: agent uploads, no server-side make (amended by #111) | Proposed |
| [0049](0049-crash-capture-tiers.md) | Crash-capture tiers: provider-agnostic method, local-libvirt realizations (M0) | Proposed |
| [0050](0050-vmcore-method-aware-storage.md) | Method-aware vmcore storage: first-method-wins per System (refines 0049/0031, closes #118) | Proposed |
| [0051](0051-install-method-conditional-crashkernel.md) | Install-time capture-method resolution + method-conditional crashkernel gate (refines 0049/0030, closes #116) | Proposed |
| [0052](0052-bootable-rootfs-image-builder.md) | Bootable kdive-ready rootfs builder: whole-disk-ext4 layout + managed SSH key (G3, closes #124) | Proposed |
| [0053](0053-build-checkout-seam.md) | Build checkout seam: warm-tree rsync + local config/patch refs (G1, closes #125) | Proposed |
| [0054](0054-object-store-unconditional-read.md) | Object-store unconditional read for system-produced keys (G2, closes #126) | Proposed |
| [0055](0055-install-readiness-kdump-seam.md) | Install readiness console classifier + host initrd-presence kdump gate (G4, closes #127) | Proposed |
| [0056](0056-live-demo-cmdline-wiring-dcache-driver.md) | Live demo cmdline wiring (build-ledger source, both lanes) + dcache A/B `live_vm` driver (G5, closes #128) | Proposed |
| [0062](0062-platform-operations.md) | Platform operations (M1.3): operator infra/control-plane tools, break-glass override, auditor reads, `require_role` denial-audit (builds on 0043) | Proposed |
