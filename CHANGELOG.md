# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-05

### Added

- Add allocation expired + system reprovisioning state edges
- Add accounting domain models and allocation size/billing columns
- Add migration 0002 for the accounting/admission data layer
- Add LockScope.PROJECT advisory lock keyed by project string
- Add typed repositories for the accounting tables
- Add kcu cost model and accounting.estimate
- Add ssh transport backend + connect capability
- Add live drgn-over-ssh introspection port
- Ssh start_session + live introspect.run over ssh
- Parameterize the destructive-op gate's role factor
- Add reprovision job kind and provider op
- Add systems.reprovision tool, handler, and profile digest
- Add metering ledger reserve/reconcile and usage rollup
- Wire release reconciliation and add accounting.usage tool
- Add quota_exceeded category + migration 0004
- Add validate_against_resource and lease-window resolver
- Gate allocations on per-project budget and quota
- Wire request through the budget/quota gate
- Enforce per-project max_concurrent_systems at provision
- Add admin set_budget and set_quota tools
- Pin power off/cycle/reset and teardown to admin
- Add renewal extension clamp against KDIVE_LEASE_MAX
- Add renew tool extending the lease window
- Add lease ->expired sweep and idempotency-key GC
- Resolve version + commit SHA + release/dev flag
- Add --version and a startup version log line
- Bake commit SHA + release flag into artifacts
- Git-cliff config + generated CHANGELOG
- Set-version/release/lock-check recipes + CI lock gate

### Changed

- Make KeyedRepository key column explicit
- Remove unused _DEFAULT_SSH_PORT constant
- Drop dead test mock, reject empty baked commit

### Documentation

- M1 allocation/accounting spec, ADRs, and implementation plan
- Fix accounting defects found in adversarial review
- Close input-validation, authz-scoping, and reconcile-race gaps
- Add request idempotency, fix rollup attribution, O(1) budget read
- Scope idempotency key per principal, split input validation
- Extract ADR-0040 for admission/lifecycle concurrency
- Reconcile ADR-0038 cost-rollup claim with ADR-0007 §6
- Tighten ADR-0039 SSH transport implementation contracts
- Tighten ADR-0038 digest, lock scope, gate role factor
- Pin destructive power+teardown to admin in ADR-0037
- Address ADR-0037 challenge round 1
- Align M1 RBAC table+exit-criterion with ADR-0037
- Clarify raise-vs-envelope denial in ADR-0037+spec
- Add issue #71 e2e integration test plan
- Tighten #71 plan criteria 2 and 4 per challenge
- Pass 7 threat model for expire-sweep renew race
- Threat model for admit cross-kind idempotency fence
- Finding for the CI ty test-tree gap
- Add CLAUDE.md and AGENTS.md
- Design spec for the versioning policy and release process
- Harden versioning spec per adversarial review
- Address second-pass review of versioning spec
- Address third-pass review of versioning spec
- Address fourth-pass review of versioning spec
- ADR-0041 versioning policy & release process
- Address fifth-pass review (ADR/spec convergence)
- Implementation plan for versioning & release process
- RELEASING runbook + README/AGENTS pointers
- Point version-bump heading at just set-version
- Record -dev window, breaking marker, and release CI scope
- Enforce post-release bump merge ordering in the runbook

### Fixed

- Fail closed on too-large estimate, accept string window
- Preserve secret-backend error category; pin credential retention
- Restore detect-secrets baseline; drop kwargs type-ignore
- Map reconcile pricing failure to a typed release failure
- Fail closed on estimate overflow and cross-project key reuse
- Isolate each allocation in the ->expired sweep
- Stamp active_started_at when first System reaches ready
- Stamp active_ended_at under a row lock
- Satisfy Provisioner protocol in _FakeProvisioner
- Re-validate lease window in the ->expired sweep
- Scope request idempotency replay to the request kind
- Reject unexpected release arg in stamp script
- Add Breaking heading, repo URLs, correct config
- Reject leading-zero versions, clarify release guard message
- Classify security-scoped commits; document conventions
- Scope security parser to feat|fix so noise doesn't leak
- Pass build flag positionally (just build true)

### Style

- Split overlong _buildinfo import for ruff I001

## [0.1.0] - 2026-06-04

### Added

- Add M0 models, lifecycles, and error taxonomy
- Add M0 schema and forward-only migration runner
- Add async connection pool from KDIVE_DATABASE_URL
- Add transaction-scoped advisory lock helper
- Add typed async repositories with state-transition guard
- Add idempotent run_step ledger
- Object-store client skeleton with key validation
- Handler registry keyed by JobKind
- Enqueue with admission idempotency on dedup_key
- Dequeue with SKIP LOCKED claim, attempt charge, lease reclaim
- Fenced heartbeat, complete, and requeue-or-dead-letter fail
- Worker run_once dispatch with heartbeat and finalize
- Tool-response envelope with job-handle mapping (ADR-0019)
- JWT verifier + (principal, agent_session, project) context
- Recent_jobs read for the jobs.list tool
- Jobs.get/.wait/.cancel/.list handlers and register hook
- App assembly with tool + handler plane seams
- Server/worker entrypoints with structured logging
- InfraReaper port, NullReaper, ReconcileReport
- Orphaned-system repair enqueues GC teardown
- Dead-letter zombie jobs with atomic Run compensation
- Detach dead debug sessions on stale heartbeat
- Reap leaked libvirt domains via the InfraReaper port
- Reconcile_once composition with per-repair isolation and Reconciler loop
- Add the reconciler subcommand entrypoint
- Capability value types and plane/cleanup enums
- Eight plane Protocols and handle aliases
- Capability registry with atomic register and dispatch
- Allow granted->releasing allocation transition (#14)
- Add RESOURCE advisory-lock scope for admission (#14)
- ToolResponse.success/.failure envelope factories (#14)
- Per-host capacity admission with resource lock (#14)
- Local-libvirt discovery + resource registration (#14)
- Resources.list/.describe tools (#14)
- Allocations.request/.get/.release/.list tools (#14)
- Provisioning-profile schema with libvirt section (#15)
- Export provisioning-profile public names (#15)
- Add System provisioning->torn_down edge (#16)
- Tagged libvirt domain define/start and idempotent teardown (#16)
- Systems.* tools + provision/teardown handlers (#16)
- Add Provisioner port + full provision/teardown test coverage (#16)
- Register systems.* tools and provision/teardown handlers in app (#16)
- Add INVESTIGATION advisory-lock scope (#17)
- Add open + get tools (#17)
- Add close tool with idempotent + backstop paths (#17)
- Add link/unlink external-ref tools (#17)
- Add runs.get tool and run envelope (#17)
- Add runs.create with binding invariant + first-Run activation (#17)
- Register investigations.* and runs.* planes (#17)

### Documentation

- Initial commit
- Close review findings F1-F6 and restore dropped error categories
- Add ADR scaffolding (README, template, decision stubs)
- Close iteration-1 adversarial review findings
- Close iteration-2 adversarial review findings
- Resolve and harden M0 architecture decisions
- Add M0 walking-skeleton integration contract
- Close M0 walking-skeleton adversarial review findings
- Add domain ER and walking-skeleton sequence diagrams
- Drop PROJECT pseudo-entity from M0 ER diagram
- Fix mermaid sequence diagram parse error
- Clarify Investigation as cross-cutting root in ER diagram
- Add Investigation external_refs and generalize campaign framing
- Add M0 walking-skeleton implementation plan
- Close adversarial review findings on M0 plan
- Close second adversarial pass on M0 plan
- Close third adversarial pass on M0 plan
- Fix graph/deps mismatch and add observability
- Own live_vm fixtures and tighten dedup_key constraint
- Fix Protocol miscount in Issue 11 (eight, not nine)
- Record structured-logging seam (ADR-0014)
- Add ADR-0015 forward-only SQL migration runner
- Harden ADR-0015 lock-space, checksum, trigger, CI guard
- Add db schema & migration runner implementation plan
- Strengthen enum coverage, harness, and concurrency tests
- Spec + ADR-0016 for repository layer, locks, idempotency ledger
- Harden repo/locks/idempotency spec from adversarial review
- Reconcile insert column derivation with timestamp contract
- Correct update_state concurrency contract to match FOR UPDATE
- Implementation plan for repository layer, locks, idempotency
- Close plan verification gaps from adversarial review
- Spec + ADR for object-store client
- Tighten object-store failure contract and row binding
- Separate stored etag from If-Match header form
- Implementation plan for object-store client
- Fix plan import ordering and CI image-pull verification
- Pin the MinIO tag actually published to Docker Hub
- Spec + ADR-0018 for the job queue & worker tier
- Address spec review — txn granularity, heartbeat test, lease guard
- Terminal dead-letter for no-handler; finalize on fresh conns
- Reject max_attempts<1 at enqueue boundary
- TDD implementation plan for the job queue & worker tier
- Address plan review — drop broken helper, type handler, fix dict_row
- Spec + ADR-0019 for MCP skeleton, auth, jobs tools
- Address spec review — worker handler seam, claim passthrough, authz risk window
- Accurate long-poll timeout treatment, stable list ordering
- TDD implementation plan for issue #10
- Fix plan review findings — list_tools accessor, wait loop coverage, no placeholders
- Spec + ADR-0020 for RBAC, audit, destructive gate
- Address spec review — fix require_role algorithm, hashability, audit/gate threat model
- Audit denied destructive ops; bound args_digest input domain
- Implementation plan; document auth<->rbac cycle break
- Restructure plan for green self-contained commits; pin digest test
- Spec + ADR-0021 for the M0 reconciler loop
- Address challenge pass 1 on the spec
- Address challenge pass 2 on the spec
- Address challenge pass 3 — frame candidate reads in a transaction
- Address challenge pass 4 — isolate each repair in reconcile_once
- TDD implementation plan for the M0 reconciler loop
- Spec + ADR for capability registry & dispatch (#13)
- Harden capability spec/ADR per challenge review (#13)
- Enforce capability-key uniqueness, contract parity, dispatch logging (#13)
- Correct profile-alias provenance in capability spec (#13)
- Implementation plan for capability registry & dispatch (#13)
- Merge register+dispatch into one green-commit task (#13)
- Spec + ADR-0023 for discovery and per-host admission (#14)
- Address spec /challenge round 1 (release race, idempotency, list_owned, concurrency test, ANY array)
- Address spec /challenge round 2 (failed-allocation envelope, deterministic lock test, registration Jsonb)
- Implementation plan for discovery + admission (#14)
- Address plan /challenge round 1 (self-contained Task 5 commit, inlined insert, ty check scope)
- Add missing RESOURCES import to discovery block (plan /challenge round 2)
- 0024 provisioning-profile model shape (refines 0011, #15)
- Pin boot_method, non-empty strings, domain_xml_params type (#15)
- Clarify domain_xml_params value-vs-count constraint (#15)
- Provisioning-profile schema implementation plan (#15)
- Require strict integers for profile core fields (#15)
- Strict integer fields + coercion tests; drop redundant pragmas (#15)
- Repair merged test-snippet lines; drop unneeded pragmas (#15)
- Make test imports incremental to avoid F401; fix merged line (#15)
- Type fixtures dict[str, Any] for nested-subscript ty checks (#15)
- Spec + ADR-0025 for libvirt provisioning plane (#16)
- Address spec review — audit ctx, state edge, inert crashkernel (#16)
- Boundary-validate params, reject reprovision of spent allocation (#16)
- Serialize provision/teardown on SYSTEM lock to close domain-leak race (#16)
- TDD implementation plan for libvirt provisioning plane (#16)
- Fix plan — audit/txn bug, deterministic race test, step numbering (#16)
- Record durable provision-side compensation in ADR-0025 (#16)
- Record Investigation+Run lifecycle decisions in ADR-0026 (#17)
- Address spec /challenge round 1 — runs.create idempotency + alloc allowlist (#17)
- Address spec /challenge round 2 — unlink natural-key input + investigation allowlist (#17)
- Spec /challenge loop converged to approve (round 3) (#17)
- Add Investigation+Run lifecycle implementation plan (#17)
- Address plan /challenge round 1 — fix import pruning, ty-check scope, ty ignore directive (#17)
- Address plan /challenge round 2 — defer unused test imports, whole-tree ruff (#17)
- Plan /challenge loop converged to approve (round 3) (#17)
- Record red-team supply-chain & dangerous-API audit

### Fixed

- Harden log serialization, keep strict ty gate, widen ty hook
- Harden transition guard and document edge cases
- Fail fast on missing schema dir and non-idle connection
- Raise explicit errors for impossible-row invariants
- Map botocore transport errors to infrastructure_failure
- Map mid-stream body-read failures to infrastructure_failure
- Keep the worker alive when a heartbeat or iteration errors
- Reject a worker pool too small for dispatch + heartbeat
- Surface current status when cancel hits a terminal job
- Document best-effort counts; test destroy-failure resilience
- Audit pinned deps with pip-audit --no-deps
- Install libvirt headers for the supply-chain audits
- Reject non-int schema_version; cover provider invariant + malformed input (#15)
- Require non-empty domain_xml_params keys (#15)
- Close libvirt connection after provision/teardown; register ns once (#16)
- Teardown retry re-attempts destroy so post-commit failure self-heals (#16)
- Make provision idempotent over an already-running domain (#16)
- Provision finalize tears down only on terminal, not on concurrent ready (#16)
- Tolerate already-terminal System in provision failure branch (#16)
- Undefine domain on real create failure to avoid leak (#16)
- Reap superseded domain on requeue so failed compensation self-heals (#16)

### M0

- Port safety modules + file-ref secret backend (#45)
- Control plane: power + force_crash (gated) (#23) (#46)
- Build plane (local make) (#18) (#47)
- Install + boot plane (#19) (#49)
- Retrieve plane: vmcore capture/fetch + crash postmortem (#24) (#50)
- Connect plane (gdbstub) + DebugSession lifecycle (#20) (#51)
- Debug plane — offline drgn introspection from vmcore (#22) (#53)
- Debug plane: port gdb-MI tier (#21) (#55)
- End-to-end walking-skeleton integration test (#26) (#59)

### Security

- Project-scoped roles on RequestContext + require_role
- Append-only audit record with hashed args_digest
- Three-check destructive-op gate
- Use raw docstring in confine_to_root to drop invalid escape (#48)
- Parse libvirtd XMLDesc with defusedxml in the install plane
- Project-scope jobs.get/wait/cancel/list (close #11 exposure)

### Build

- Add psycopg-pool and testcontainers; pin .sql to LF

[0.2.0]: https://github.com/randomparity/kdive/compare/v0.1.0..v0.2.0
[0.1.0]: https://github.com/randomparity/kdive/tree/v0.1.0

<!-- generated by git-cliff -->
