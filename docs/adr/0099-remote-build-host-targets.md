# ADR 0099 — Remote build-host targets via a `BuildTransport` seam + `build_hosts` inventory

- **Status:** Proposed
- **Date:** 2026-06-13
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0029](0029-build-plane-local-make.md)
  (the `runs.build` / `BuildProfile` / build-handler plane this extends),
  [ADR-0081](0081-remote-build-kernel-bundle.md)
  (the remote worker `make` build and vmlinuz+modules bundle whose execution this relocates
  off the worker), [ADR-0096](0096-kdump-config-fragment-build-input.md) (the config-fragment
  resolution + fragment-survival preflight that stays worker-side), [ADR-0097](0097-not-found-conflict-error-categories.md)
  (the precedent for adding a taxonomy value for a genuine new failure mode), and
  [ADR-0077](0077-qemu-tls-control-transport.md) (the secret-by-reference + materialized
  credential-file pattern reused for the SSH identity).
- **Spec:** [`../superpowers/specs/2026-06-13-remote-build-host-design.md`](../superpowers/specs/2026-06-13-remote-build-host-design.md)
- **Issue:** [#342](https://github.com/randomparity/kdive/issues/342)
- **Milestone:** remote build-host targets

## Context

The build plane was meant to offer two delivery paths: upload a locally built artifact, or
request a build from a remote build server. The first ships as the external-build lane
(`source="external"`). The second does not exist: the server-build lane runs `make` in the
worker process itself (`providers/build_host/execution.py`), so a worker without the kernel
toolchain and a warm source tree cannot build at all, builds contend with the control-plane
worker for resources, and there is no way to point builds at a dedicated builder.

The slow build steps are already injected seams behind `BuildHostOrchestrator`
(`checkout` / `run_olddefconfig` / `read_config` / `run_make`), defaulting to local
subprocess implementations. The config-fragment resolution and the kdump/debuginfo
preflight (`_validate_final_config`, ADR-0096) run around those seams on the worker. This is
the leverage point: a remote target can be new seam realizations rather than a parallel
build path.

Three build targets are in view: (1) local upload (exists), (2) a dedicated SSH build host,
(3) an ephemeral remote-libvirt build VM. This ADR settles the shared model and the SSH
target; target 3 is a designed-for follow-up.

## Decision

1. **Keep `BuildHostOrchestrator`; introduce a `BuildTransport` port.** The orchestrator
   contract (`build_workspace`), the `Builder` port, `BuildOutput`, and the `runs` ledger do
   not change. A `BuildTransport` abstracts the host-side primitives (`run` fixed-argv,
   `read_text`/`read_bytes`, `clone`, `cleanup`, `presign_put`). Two realizations:
   `LocalBuildTransport` (a pure refactor of today's subprocess behavior, no observable
   change) and `SshBuildTransport` (`ssh`/`sftp`, fixed argv, identity from a secret-ref).
   The seams gain transport-backed implementations; the config preflight stays worker-side
   (worker resolves the fragment, host builds, worker reads `.config` back and validates).

2. **A DB-backed `build_hosts` inventory is the selection seam.** A `build_hosts` table
   (`name`, `kind` ∈ {`local`,`ssh`}, `address`, `ssh_credential_ref`, `workspace_root`,
   `max_concurrent`, `active_builds`, `enabled`, `state`) with a seeded
   `('worker-local', kind='local')` row that preserves today's behavior when nothing else is
   registered. The `kind` CHECK admits `'ephemeral_libvirt'` later without a second migration
   to the column shape. `ServerBuildProfile.build_host` (optional name) selects a host;
   absent → `worker-local`. Registration (`build_hosts.register/list/disable/remove`) is a
   `PLATFORM_ADMIN`, audited admin plane; `list` is read-only passthrough.

3. **Capacity is fail-fast.** A new `LockScope.BUILD_HOST` advisory lock guards an atomic
   `active_builds < max_concurrent` check-then-debit; over-capacity returns
   `capacity_exhausted` immediately (no queue). The debit is credited on success and on
   failure (`finally`), keyed by `run_id`+step so a retried job does not double-count, and
   the reconciler reclaims leaked debits from dead workers.

4. **Add `ErrorCategory.CAPACITY_EXHAUSTED`.** Host-at-capacity is a genuine new failure
   mode with no fitting existing value (`quota_exceeded` is per-project accounting;
   `infrastructure_failure` is non-specific). Following the ADR-0097 precedent, add
   `capacity_exhausted` with its exit-code mapping, taxonomy docs, and closed-set test in the
   same change.

5. **Git-clone provenance for the SSH builder; warm-tree for local.**
   `kernel_source_ref` becomes meaningful: a plain string keeps warm-tree provenance (local
   builder, unchanged); an object `{git: {remote, ref}}` selects git-clone provenance (ssh
   builder clones the ref into an isolated per-run workspace subdir). The cross-checks fail
   closed — the local builder rejects a git ref and the ssh builder rejects a warm-tree
   string — so the builder-dependent interpretation of one shared field can never silently
   mismatch.

6. **Artifacts upload from the build host via presigned PUT.** The worker mints presigned
   S3 PUT URLs; the SSH host uploads the kernel bundle and vmlinux directly, so the worker
   never holds the bytes. This matches the remote-libvirt in-guest upload pattern (#206) and
   keeps the build host's outbound dependency explicit (S3 egress).

7. **Build execution is not gated; registration is.** `build_hosts.register` (creating
   remote-exec infrastructure) requires `PLATFORM_ADMIN` and is audited. Running a build on a
   registered host is a normal server-lane build and is **not** added to the destructive-op
   gate — it is not power/teardown/force_crash, consistent with how builds work today.
   Bounding controls are admin-only registration, isolated per-run workspace, the
   component-root allowlist, fixed argv, and the existing build timeout.

8. **Scope.** This ADR's PR implements the seam + local refactor + the SSH target +
   inventory + selection + capacity + reconciler upkeep. The ephemeral remote-libvirt build
   VM (target 3) is a follow-up that adds `kind='ephemeral_libvirt'` plus a build-VM
   provisioning lifecycle; the schema and the `BuildTransport` port are designed to admit it.

## Consequences

- The local build path is refactored onto `LocalBuildTransport` with no behavior change; a
  golden `BuildOutput` back-compat test pins that the seed-only deployment builds exactly as
  before.
- One new table + migration (0027), one new error category, one new `LockScope`, one new
  admin plane, and reconciler additions. No change to `BuildOutput`, the `Builder` port, or
  the `runs` ledger contract.
- `kernel_source_ref` now carries structure (string | `{git}`); existing server profiles
  that omit it or use the warm-tree form are unaffected. The profile model, the build-tool
  input schema, and profile (de)serialization change to admit the git form.
- A build host that executes a caller-named git ref is operator-registered infrastructure;
  the threat surface is the git remote and `make`, bounded by isolated workspace + fixed
  argv + allowlist + timeout. Secrets resolve by reference and are redacted from all remote
  output.
- The fail-fast capacity contract pushes retry/backoff to the caller; under heavy contention
  this is worse UX than a queue, accepted for now and revisitable (alternative below).

## Alternatives considered

- **Parallel `RemoteSshBuild` orchestrator.** Mirror `BuildHostOrchestrator` for the remote
  path. Rejected: duplicates the preflight + ledger logic the issue explicitly warns against;
  the seam already abstracts the slow steps.
- **Build agent that polls the job queue.** A kdive build agent on each host pulling BUILD
  jobs. Rejected for this issue: a whole new process and deployment surface, no operator
  demand, and harder to reason about than worker-initiated SSH.
- **Config-only single build host (no DB).** `KDIVE_BUILD_HOST_*` env, one global builder.
  Simpler (no DDL), but cannot model several builders, per-host capacity, drain, or health,
  which the inventory needs. Rejected in favor of the table; the seed row keeps the
  zero-config case trivial.
- **Pull artifacts back over SSH, worker PUTs.** Smaller build-host surface (SSH only, no S3
  egress), but routes the (large) bundle through the worker's memory/bandwidth. Rejected in
  favor of presigned PUT, which matches the established remote pattern and avoids worker
  memory pressure; the build host's S3 egress is an explicit, documented requirement.
- **Worker ships source over SSH (rsync) / no provenance change.** Keeps the warm-tree model
  by rsyncing the worker tree to the host per build. Rejected: large per-build transfer, and
  it leaves `kernel_source_ref` meaningless; git-clone gives the remote builder a real,
  reviewable provenance and a smaller transfer.
- **Reuse `quota_exceeded` / `infrastructure_failure` for capacity.** Avoids a taxonomy
  addition. Rejected: `quota_exceeded` is project accounting and `infrastructure_failure` is
  non-specific; a capacity failure with a clear retry affordance warrants its own value, per
  the ADR-0097 precedent.
- **FIFO build queue on capacity (ADR-0069 scheduler).** Better UX under contention.
  Rejected for this PR: adds queue state, ordering, and reconciler reclaim for build hosts —
  a larger build than fail-fast; can be added later behind the same selection seam.
- **Build execution behind the destructive-op gate.** Maximal caution. Rejected:
  inconsistent with local/remote builds today and disproportionate — execution is bounded by
  admin-only host registration and the workspace/argv/allowlist controls.
