# ADR 0101 — Local-libvirt builds on a remote build host (transport-capable local builder)

- **Status:** Proposed
- **Date:** 2026-06-13
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0099](0099-remote-build-host-targets.md)
  (the `BuildTransport` seam, `build_hosts` inventory + provider-agnostic selection/capacity,
  git-clone provenance, and presigned-PUT artifact upload this extends to a second provider),
  [ADR-0100](0100-ephemeral-libvirt-build-vm.md) (the `ephemeral_libvirt` kind that, being a
  remote host, is admitted by the same capability-based dispatch this introduces),
  [ADR-0029](0029-build-plane-local-make.md) (the `LocalLibvirtBuild` warm-tree `make` +
  two-artifact store this makes transport-capable), and
  [ADR-0076](0076-remote-libvirt-provider-package.md) (the rule that `remote_libvirt` and
  `local_libvirt` share no provider-internal layer — honored by extracting the shared
  artifact-publish helper into the neutral `build_host` layer, not by coupling the providers).
- **Spec:** [`../superpowers/specs/2026-06-13-local-libvirt-remote-build-host.md`](../archive/superpowers/specs/2026-06-13-local-libvirt-remote-build-host.md)
- **Issue:** [#356](https://github.com/randomparity/kdive/issues/356)
- **Milestone:** remote build-host targets

## Context

ADR-0099 made the `BuildTransport` seam provider-neutral but wired only the **remote-libvirt**
provider to use it. A `local_libvirt`-provider Run that selects an SSH (or, after ADR-0100,
ephemeral) build host passes selection — `build_host_selection.resolve_and_admit` admits any
non-`local` host for a git-source profile **regardless of provider** and acquires a capacity
lease — then fails late in the BUILD handler: `_require_remote_builder` narrows the resolved
runtime builder to `RemoteLibvirtBuild` and raises `NOT_IMPLEMENTED` for anything else.

So the only barrier to a local-provider remote build is an artificial type-narrowing in the
handler plus the fact that `LocalLibvirtBuild` has no transport-bound build path. Nothing in
the schema, the selection/capacity model, the lease lifecycle, or the error taxonomy needs to
change.

The leverage points already exist:

- **The build orchestration is transport-agnostic.** `BuildHostOrchestrator` runs every slow
  step (checkout, `olddefconfig`, `.config` read+preflight, `make`) through injected seams;
  `build_host/transport_seams.py` already provides transport-backed realizations of all of
  them (`transport_git_checkout`, `transport_run_olddefconfig`, `transport_read_config`,
  `transport_run_make`, `transport_read_build_id`), used today by `RemoteLibvirtBuild`.
- **The presigned-publish-of-a-host-file helper already exists** inside `remote_libvirt/build.py`
  (`ArtifactSource`/`ArtifactBytes`/`ArtifactRemoteFile` + `_publish_remote_file`): hash the
  file on the host, presign a PUT bound to that checksum, upload from the host, return the row.
  It is entirely provider-neutral — it takes a tenant, a name, a path, a transport, and a store.

What does not exist: (1) a transport-bound build path on `LocalLibvirtBuild`; (2) a home for
the publish helper that both providers may use without violating ADR-0076; (3) handler dispatch
that admits a transport-capable builder of either provider.

Local-provider builds are **simpler** on a transport than remote ones: a local System boots the
kernel via direct-kernel boot, so it needs only `arch/x86/boot/bzImage` (as `kernel_ref`) and
`vmlinux` (as `debuginfo_ref`) — no `make modules_install`, no `/lib/modules` bundle, no `tar`.
The local transport build is a strict subset of the remote post-make pipeline.

## Decision

1. **Make `LocalLibvirtBuild` transport-capable by adding `over_transport`, mirroring
   `RemoteLibvirtBuild.over_transport` — not a parallel build path.** `over_transport(transport,
   *, host_workspace_root, git_remote, git_ref, secret_registry)` returns a sibling
   `LocalLibvirtBuild` whose orchestrator seams (checkout/`olddefconfig`/`read_config`/`make`)
   and `read_build_id` are the transport-backed seams from `build_host/transport_seams.py`, and
   whose kernel/vmlinux artifacts are produced as host-resident files published via presigned
   PUT. The worker-side config (catalog fetch, component-root allowlist, store factory, tenant)
   is reused so config-fragment resolution and the presigned publish stay worker-side. The
   per-run workspace lives under `host_workspace_root` on the host.

2. **Unify `LocalLibvirtBuild.build()` on the `ArtifactSource` model.** Replace the two
   `ReadBytes` artifact seams (`read_kernel_image`/`read_vmlinux`, always PUT from worker memory)
   with two `Callable[[Path], ArtifactSource]` seams (`read_kernel_source`/`read_vmlinux_source`)
   and a single `publish()` that PUTs `ArtifactBytes` directly or presigns+uploads an
   `ArtifactRemoteFile` — exactly the shape `RemoteLibvirtBuild` already has. The warm-tree
   default seams return `ArtifactBytes(real_read_bzImage/vmlinux(...))`, byte-for-byte today's
   behavior. The transport seams return `ArtifactRemoteFile(path, transport)`.

3. **Extract the `ArtifactSource` model + presigned-publish helper into the neutral
   `build_host` layer; both providers depend on it.** A new `build_host/artifact_publish.py`
   owns `ArtifactSource`/`ArtifactBytes`/`ArtifactRemoteFile`, a `StorePort` protocol
   (`put_artifact` + `presign_put`), and `publish_artifact_source(store, run_id, name, source,
   *, tenant, sensitivity, retention_class)` (with the host `sha256sum`/`stat` readers and the
   capped presign TTL). `RemoteLibvirtBuild` is refactored to import these from the neutral home
   and delete its private copies (replace, no shim — ADR-0076 bars only provider↔provider
   coupling; `build_host` is the sanctioned shared build layer both providers already use).

4. **Handler dispatch becomes capability-based, not provider-type-based.** Replace
   `_require_remote_builder` (`isinstance … RemoteLibvirtBuild`) with a check against a
   `TransportCapableBuilder` protocol (a `Builder` that also exposes `over_transport`), defined
   beside `Builder` in `providers/ports/build.py`. The handler binds via `builder.over_transport(
   …)` through a single patchable module seam. Because both remote host kinds (`ssh`,
   `ephemeral_libvirt`) already share the bind path, this makes **both kinds accept both
   providers**: the seam is provider-neutral, as the issue states. A builder without
   `over_transport` (e.g. fault-inject) still fails `NOT_IMPLEMENTED`.

5. **No schema, selection, capacity, lease, or taxonomy change.** Selection is already
   provider-agnostic; the git⟂warm cross-check (`local` host requires a warm-tree ref, non-local
   host requires a git ref) is unchanged and already correct for a local-provider run on a
   remote host (git required, enforced at the tool boundary). The lease lifecycle
   (acquire-at-admission, release-on-success, retain-on-failure, reconciler-reclaim-by-job-
   liveness) is identical for both providers because it keys on `run_id`, not provider.

6. **Scope.** Builder capability (`over_transport` + `ArtifactSource` unification) + the neutral
   publish-helper extraction + capability-based handler dispatch. The real git-fetch/`make`/ssh
   path is exercised only under the existing `live_vm` gate and the operator runbook (gates
   unchanged, not widened). No migration, no new `ErrorCategory`, no new `LockScope`.

## Consequences

- A `local_libvirt`-provider Run with a git-source profile naming an `ssh` or
  `ephemeral_libvirt` build host now builds on that host: git checkout + `make` run remotely and
  `bzImage`+`vmlinux` publish via presigned PUT, so the worker never reads the artifact bytes.
  The built kernel installs and direct-kernel-boots on a local-libvirt System exactly as a
  worker-local local build does (builder ⟂ build-host — the build host is independent of where
  the System boots).
- `RemoteLibvirtBuild`'s `ArtifactSource`/publish code moves to `build_host/artifact_publish.py`;
  `remote_libvirt/build.py` re-exports the moved names it still constructs, so its public surface
  and behavior are unchanged. The existing remote transport tests pin this as behavior-preserving.
- `LocalLibvirtBuild`'s constructor signature changes (two artifact-source seams replace the two
  byte-reader seams). This is an internal provider-assembly contract; its unit tests and
  `from_env` update in the same change. No external/tool/DDL contract changes.
- The handler no longer references `RemoteLibvirtBuild` by type; it depends only on the
  `TransportCapableBuilder` protocol, removing the provider-name coupling from the BUILD handler.
- A local-provider remote build occupies a capacity slot on the shared build host on exactly the
  same lease terms as a remote-provider build; a deployment that drains or removes the host
  affects both providers identically.

## Considered & rejected

- **Special-case only the `ssh` branch in the handler (issue-literal scope).** The `ssh` and
  `ephemeral_libvirt` paths currently share one bind step after the builder check; admitting the
  local builder for `ssh` only would split that unified path and leave an unjustifiable
  `local`-on-`ephemeral` `NOT_IMPLEMENTED`. The capability check is strictly less code and makes
  the seam genuinely provider-neutral, matching the issue's framing. Rejected.
- **Duplicate the presigned-publish helper into `local_libvirt`.** ADR-0076 forbids
  provider↔provider coupling, not use of the neutral `build_host` layer (which both providers
  already build on). Duplicating ~80 lines of identical, provider-neutral logic to avoid a
  neutral home is the wrong trade. Rejected in favor of the `build_host` extraction.
- **Give the local provider a modules bundle like remote.** A local System direct-kernel-boots
  and needs no in-guest `/lib/modules`; adding `modules_install`+bundle would change the
  artifact shape and boot semantics for no benefit. Rejected — local publishes the bare
  `bzImage`, unchanged.
- **Add capacity/lease handling for the local-provider-on-remote-host case.** The existing lease
  seam already acquires/releases for any non-`local` host keyed on `run_id`; provider identity
  never enters it. No new machinery is needed. Rejected.
- **Keep `_require_remote_builder` and add a sibling `_require_local_builder`.** Two
  near-identical type-narrowing functions where one protocol check suffices. Rejected.
