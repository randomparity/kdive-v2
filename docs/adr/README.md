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
| [0009](0009-capability-provider-dispatch.md) | Capability-based provider dispatch | Superseded for runtime assembly by 0063 |
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
| [0022](0022-capability-registry-dispatch-impl.md) | Capability registry & dispatch implementation shapes (refines 0009) | Superseded for runtime assembly by 0063 |
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
| [0059](0059-first-run-host-registration.md) | First-run local-libvirt host registration at reconciler startup (live stack, PR #134) | Proposed |
| [0060](0060-per-system-rootfs-overlay.md) | Per-System rootfs overlay: a writable qcow2 layer over the shared base (live stack, PR #134) | Proposed |
| [0061](0061-boot-cmdline-composition.md) | Boot cmdline composition: platform-required base + appended debug args (supersedes 0056, PR #134) | Proposed |
| [0062](0062-platform-operations.md) | Platform operations (M1.3): operator infra/control-plane tools, break-glass override, auditor reads, `require_role` denial-audit (builds on 0043) | Proposed |
| [0063](0063-typed-provider-runtime.md) | Typed ProviderRuntime is the active M0/M1 provider seam | Accepted |
| [0064](0064-expected-boot-failures-artifact-search.md) | Expected boot failures + bounded redacted artifact search | Proposed |
| [0065](0065-provider-component-references.md) | Provider component references and profile requirements | Proposed |
| [0066](0066-remove-capability-registry-prototype-from-src.md) | Remove capability-registry prototype from production source | Accepted |
| [0067](0067-system-shapes-catalog.md) | System shapes catalog + selector unification (M1.4) | Proposed |
| [0068](0068-custom-config-pcie-modeling.md) | Custom config + PCIe capability modeling (M1.4) | Proposed |
| [0069](0069-reservation-pending-queue-scheduler.md) | Reservation / FIFO pending-queue scheduler (M1.4) | Proposed |
| [0070](0070-fleet-availability-system-reuse.md) | Fleet availability + system reuse (M1.4) | Proposed |
| [0071](0071-per-kind-provider-runtime-registry.md) | Per-kind ProviderRuntime registry — the selection seam (M1.5) | Proposed |
| [0072](0072-fault-injection-provider-seeded-engine.md) | Fault-injection provider + seeded decision-keyed fault engine (M1.5) | Proposed |
| [0073](0073-forced-secret-resolution-redaction.md) | Forced secret resolution + end-to-end redaction validation (M1.5) | Accepted |
| [0074](0074-fault-inject-engine-port-wiring.md) | Wiring the seeded fault engine into the fault-inject ports (M1.5) | Proposed |
| [0075](0075-objectstore-quarantine-pre-registration-writes.md) | Object-store quarantine for pre-registration writes (M1.5) | Proposed |
| [0076](0076-remote-libvirt-provider-package.md) | Independent remote-libvirt provider package + portability diff gate (M2) | Proposed |
| [0077](0077-qemu-tls-control-transport.md) | qemu+tls:// control transport + x509 client-cert secret-by-reference (M2) | Proposed |
| [0078](0078-object-store-in-target-install-seam.md) | Object-store + presigned-URL in-target install/retrieve seam (M2) | Proposed |
| [0079](0079-remote-live-debug-transport.md) | Remote live-debug transport reachability — direct-TCP gdbstub, in-guest drgn, worker-side vmcore postmortem (M2) | Proposed |
| [0080](0080-remote-provisioning-disk-image-profile.md) | Remote provisioning: disk-image base-OS profile, domain-XML gdbstub port registry, storage-pool overlay (M2) | Proposed |
| [0081](0081-remote-build-kernel-bundle.md) | Remote build publishes a single vmlinuz+modules install bundle as `kernel_ref` (M2) | Proposed |
| [0082](0082-remote-install-in-guest-kernel.md) | Remote install: in-guest kernel install via one allowlisted helper + boot-id readiness (M2) | Proposed |
| [0083](0083-remote-connect-debug-plane.md) | Remote connect/debug plane: shared gdb-MI/drgn infra + ACL'd direct-TCP gdbstub (M2) | Proposed |
| [0084](0084-remote-control-two-phase-vmcore-retrieve.md) | Remote control (power/force_crash over TLS) + two-phase vmcore retrieve (kdump→local, post-reboot presigned-PUT upload) (M2) | Proposed |
| [0085](0085-drgn-live-transport-generalization.md) | Generalize the live-drgn transport off the ssh model (`drgn-live` capability token + profile-derived credential) (M2) | Proposed |
| [0086](0086-dead-worker-gdbstub-reconciler-reset.md) | Dead-worker gdbstub reconciler reset — free the single-client port on stale-session detach (M2) | Proposed |
| [0087](0087-config-registry.md) | Central typed configuration registry — single source of truth for the `KDIVE_*` contract, startup validation, generated reference (M2.1) | Proposed |
| [0088](0088-deployment-packaging.md) | Deployment & packaging — one multi-process image (remote-libvirt target), compose + Helm reference, migrate one-shot, GHCR release publish (M2.1) | Proposed |
| [0089](0089-operator-cli-mcp-client.md) | Operator CLI (`kdivectl`) as an authenticated MCP client — read-only-by-policy passthrough, break-glass mutations, `(principal, operator-cli)` attribution (M2.2) | Proposed |
| [0090](0090-opentelemetry-adoption-service-health.md) | OpenTelemetry adoption — logs/metrics/traces spine, log-signal migration (amends ADR-0014) with stdout floor + opt-in OTLP, aux health endpoints (M2.3) | Proposed |
| [0091](0091-doctor-diagnostics-model.md) | `doctor` / diagnostics model — server-side authz-gated diagnostics tool, per-check vantage, ephemeral-probe-guest egress check (M2.3) | Proposed |
| [0092](0092-image-rootfs-lifecycle.md) | Image & rootfs lifecycle — `RootfsBuildPlane` Python build planes, `image_catalog` DB table as single source of truth, publish/register two-write + reconciler drift repair (M2.4) | Proposed |
| [0093](0093-private-image-uploads.md) | Private image uploads — owner-scoped `visibility='private'` rows, required TTL, reconciler auto-prune, ADR-0048 ingest reuse (M2.4) | Proposed |
| [0094](0094-remote-host-dump-via-coredump-volume.md) | Remote host_dump via `virDomainCoreDumpWithFormat` (ELF) + presigned-PUT stream download (M2.5) | Proposed |
| [0095](0095-reconciler-remote-console-collector.md) | Reconciler-supervised remote console collector (M2.5) | Proposed |
| [0096](0096-kdump-config-fragment-build-input.md) | Kdump kernel-config fragment as a seeded build-config catalog input — merge onto `make defconfig`, catalog ref with implicit default, inline agent download | Proposed |
| [0097](0097-not-found-conflict-error-categories.md) | `not_found` / `conflict` error categories — absent-but-valid object ids return `not_found` (exit 4) while parse failures stay `configuration_error`; ungranted-row stays identical to absent (no-leak); `conflict` defined-but-unemitted (closes #338) | Proposed |
| [0098](0098-membership-denial-envelope.md) | Envelope project-membership denials as `authorization_denied` (exit 3) — `require_project` raises a typed `ProjectMembershipDenied` caught at the dispatch boundary; supersedes ADR-0020 §4 "raise" for the named-scope surface; non-member denial stays unaudited; by-id `not_found` no-leak path (ADR-0097) untouched (closes #339) | Proposed |
| [0099](0099-remote-build-host-targets.md) | Remote build-host targets — `BuildTransport` seam (local/ssh) over the existing `BuildHostOrchestrator`, DB-backed `build_hosts` inventory + selection, fail-fast `capacity_exhausted`, git-clone provenance for the ssh builder, presigned-PUT artifact upload; ephemeral remote-libvirt build VM designed-for as a follow-up (#342) | Proposed |
| [0100](0100-ephemeral-libvirt-build-vm.md) | Ephemeral remote-libvirt build VM (`kind='ephemeral_libvirt'`, #355) — bare provider-managed `kdive-build-<run_id>` domain reaped by marker + BUILD-job liveness; `GuestExecBuildTransport` over the guest-agent exec channel (one `sh -c` hop, like ssh) via a shared `ShellBuildTransport` base; migration 0029 widens the kind CHECK + adds `base_image_volume`; reuses ADR-0099 selection/capacity/lease and the error taxonomy unchanged | Proposed |
| [0101](0101-local-libvirt-remote-build-host.md) | Local-libvirt builds on a remote build host (#356) — `LocalLibvirtBuild.over_transport` makes the local provider transport-capable (direct-kernel `bzImage`+`vmlinux`, no modules bundle); shared `ArtifactSource`/presigned-publish helper extracted to neutral `build_host`; BUILD-handler dispatch becomes capability-based (`TransportCapableBuilder`), so both `ssh`/`ephemeral_libvirt` accept both providers; no schema/selection/lease/taxonomy change | Proposed |
| [0102](0102-build-host-clone-dir-cleanup.md) | Clean up the per-run build workspace after a terminal build (#358) — `BuildHostOrchestrator` owns workspace destruction via an injected best-effort `cleanup` seam (`rmtree` worker-side, `BuildTransport.cleanup` over a transport); both providers wrap `build_workspace` in `try/finally` so success and failure both reclaim the clone/rsync tree; reconciler sweep of killed-worker leaks deferred | Proposed |
| [0103](0103-build-host-reachability-probe.md) | Reconciler reachability probe for SSH build hosts (#359) — optional `BuildHostProber` `\| None` port runs a bare `ssh … true` (no workspace cd) per `kind='ssh' AND enabled=true` host and CAS-flips `build_hosts.state` ready↔unreachable so selection skips a dead builder proactively; per-probe `SecretRegistry` scope released each pass; wired unconditionally (independent of remote-libvirt) | Proposed |
| [0104](0104-chunked-external-upload-reassembly.md) | Chunked external-build uploads >5 GiB (#112) — agent splits an artifact into ordered ≤5 GiB chunks uploaded via the existing single-PUT path; `complete_build` reassembles server-side (`CreateMultipartUpload`+`UploadPartCopy`, no bytes through the server); integrity moves to per-chunk SHA-256 pins (whole-object hash advisory); chunks in JSONB (no migration); reaper obligation generalized to "manifest past deadline" to reclaim leftover chunks; `KDIVE_MAX_UPLOAD_BYTES` 5→50 GiB | Proposed |
| [0105](0105-build-config-seed-actionable-error.md) | Actionable error when the kdump build-config catalog entry is unseeded (#373) — the missing-entry `configuration_error` carries a literal `remediation` command (`python -m kdive migrate`) in `details` (→ `failure_detail_remediation` in the job response), reusing one committed constant; the standard kdump build-config stays seeded by the existing S3-tolerant `migrate` step (no second bare-migration seed, which can't run S3-free) | Proposed |
| [0106](0106-build-rootfs-guest-image-wiring.md) | `build-rootfs` emits its `KDIVE_GUEST_IMAGE` wiring (#370) — on success the command prints exactly one eval-safe `export KDIVE_GUEST_IMAGE=<shlex-quoted --dest>` line to stdout (human summary + `sha256:` digest stay on the stderr logger, ADR-0014), so `eval "$(python -m kdive build-rootfs ...)"` exports the live-spine guest image with no runbook cross-reference; live-spine skip message reworded to name the wiring; no build/plane/schema/gate change (closes the F3 "GAP (fixture)") | Proposed |
| [0107](0107-cli-mutating-tool-call-opt-in.md) | `kdivectl tool call` reaches mutating/destructive tools by explicit opt-in (#368) — relaxes ADR-0089's read-only-only passthrough to a three-tier `classify_tool` (READ_ONLY/MUTATING/DESTRUCTIVE/UNKNOWN); `--allow-mutating` / `--allow-destructive` deny-by-default flags widen the admitted tier; destructive also needs typed TTY confirmation or `--yes`; UNKNOWN stays fail-closed; server authz/audit/annotations unchanged (client-side guard only) | Proposed |
