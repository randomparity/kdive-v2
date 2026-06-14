# Local-libvirt builds on a remote build host

- **Issue:** [#356](https://github.com/randomparity/kdive/issues/356)
- **ADR:** [ADR-0101](../../adr/0101-local-libvirt-remote-build-host.md)
- **Status:** Draft
- **Date:** 2026-06-13

## Problem

ADR-0099 made the `BuildTransport` seam provider-neutral but wired only the **remote-libvirt**
provider to use it. Today a `local_libvirt`-provider Run that names an SSH (or, per ADR-0100,
`ephemeral_libvirt`) build host:

1. parses its server-build profile and passes `runs.build` host selection
   (`build_host_selection.resolve_and_admit` admits any non-`local` host for a git-source
   profile **regardless of provider** and acquires a `build_host_leases` row), then
2. fails inside the BUILD handler: `_require_remote_builder` narrows the resolved runtime
   builder to `RemoteLibvirtBuild` and raises `ErrorCategory.NOT_IMPLEMENTED` for the
   `LocalLibvirtBuild` it actually gets.

The goal: a local-provider Run with a git-source profile naming a remote build host builds on
that host and produces the same two direct-kernel-boot artifacts a worker-local local build
produces (`bzImage` → `kernel_ref`, `vmlinux` → `debuginfo_ref`), published via presigned PUT.
Builder ⟂ build-host: the build host is independent of where the System later boots.

## Non-goals

- No change to the `build_hosts` schema, host registration, selection, capacity/lease model, or
  the error taxonomy. (Selection is already provider-agnostic; the lease keys on `run_id`.)
- No modules bundle for the local provider. A local System direct-kernel-boots; it needs no
  in-guest `/lib/modules`. Local publishes the bare `bzImage`, unchanged.
- No widening of any test gate. The real git-fetch/`make`/ssh path stays behind `live_vm` and
  the operator runbook.
- No new MCP tool, payload field, or external contract.

## Design

### 1. Neutral artifact-publish helper (`build_host/artifact_publish.py`)

Move the provider-neutral publish machinery out of `remote_libvirt/build.py` into a new neutral
module both providers may import (ADR-0076 bars provider↔provider coupling, not use of the
shared `build_host` layer):

- `ArtifactBytes(data: bytes)` — an artifact the worker holds in memory; published with a direct
  `put_artifact`.
- `ArtifactRemoteFile(path: str, transport: BuildTransport)` — an artifact that lives on the
  build host; published via a presigned PUT whose sha256 is computed on the host.
- `type ArtifactSource = ArtifactBytes | ArtifactRemoteFile`.
- `StorePort` — a `Protocol` with `put_artifact` + `presign_put`.
- `publish_artifact_source(store, run_id, name, source, *, tenant, sensitivity,
  retention_class) -> StoredArtifact` — matches on the source: `ArtifactBytes` → `put_artifact`;
  `ArtifactRemoteFile` → hash (`sha256sum`) + size (`stat`) on the host, `presign_put` bound to
  the base64 checksum, `transport.upload_file`, return the row. The `sha256sum`/`stat` readers
  and the capped-presign-TTL helper move with it.

`RemoteLibvirtBuild` is refactored to import these and delete its private copies. The names it
still constructs (`ArtifactBytes`, `ArtifactRemoteFile`, `ArtifactSource`) are re-exported from
`remote_libvirt/build.py` so its public surface and existing tests are unaffected.

### 2. Transport-capable `LocalLibvirtBuild`

Refactor `LocalLibvirtBuild` so its `build()` produces an `ArtifactSource` per artifact and
publishes via the shared helper:

- Replace constructor seams `read_kernel_image: ReadBytes` / `read_vmlinux: ReadBytes` with
  `read_kernel_source: Callable[[Path], ArtifactSource]` /
  `read_vmlinux_source: Callable[[Path], ArtifactSource]`.
- `build()` becomes: `workspace = orchestrator.build_workspace(...)`;
  `build_id = read_build_id(workspace)`;
  `kernel = publish(run_id, "kernel", read_kernel_source(workspace))`;
  `vmlinux = publish(run_id, "vmlinux", read_vmlinux_source(workspace))`;
  return `BuildOutput(kernel.key, vmlinux.key, build_id)`.
- `publish()` delegates to `publish_artifact_source(..., tenant=self._tenant,
  sensitivity=SENSITIVE, retention_class="build")`.
- `from_env` default seams: `read_kernel_source = lambda ws: ArtifactBytes(
  real_read_kernel_image(ws))`, `read_vmlinux_source = lambda ws: ArtifactBytes(
  real_read_vmlinux(ws))` — byte-for-byte the current warm-tree behavior.

Add `over_transport(transport, *, host_workspace_root, git_remote, git_ref, secret_registry)
-> LocalLibvirtBuild`, mirroring the remote method but simpler (no `modules_install`/bundle):

- orchestrator seams: `transport_git_checkout(transport, git_remote, git_ref, secret_registry)`,
  `transport_run_olddefconfig(transport)`, `transport_read_config(transport)`,
  `transport_run_make(transport)`;
- `read_build_id = transport_read_build_id(transport)`;
- `read_kernel_source = lambda ws: ArtifactRemoteFile(str(ws / "arch/x86/boot/bzImage"),
  transport)`;
- `read_vmlinux_source = lambda ws: ArtifactRemoteFile(str(ws / "vmlinux"), transport)`;
- reuse `self`'s worker-side config (catalog fetch, component-root allowlist, store factory,
  tenant); workspace root = `Path(host_workspace_root)`.

**Host-side cleanup.** Like `RemoteLibvirtBuild.over_transport` — which registers cleanup only
for its `modroot` staging tree and never removes the per-run workspace clone at
`host_root/<run_id>` — the local transport build does **not** clean the per-run clone on the
build host, and (having no `modules_install`) has no staging tree to clean at all. This matches
remote's existing behavior; per-run host-workspace reclamation is a pre-existing shared gap in
the ssh/ephemeral build-host design (ADR-0099), out of scope for #356 and left to the operator
or a future shared follow-up. The change here introduces no new cleanup obligation it fails to
meet — it inherits remote's contract exactly.

### 3. Capability-based handler dispatch

In `providers/ports/build.py`, add beside `Builder`:

```python
@runtime_checkable
class TransportCapableBuilder(Builder, Protocol):
    def over_transport(self, transport, *, host_workspace_root, git_remote, git_ref,
                       secret_registry) -> Builder: ...
```

In `jobs/handlers/runs.py`:

- Replace `_require_remote_builder(builder, host, run_id) -> RemoteLibvirtBuild` with
  `_require_transport_capable(builder, host, run_id) -> TransportCapableBuilder` that raises
  `NOT_IMPLEMENTED` (same message intent) when `builder` lacks `over_transport`.
- Replace the `build_over_transport(builder, …)` call (imported from `remote_libvirt.build`)
  with a single module-level patchable seam `bind_over_transport(builder, transport, *, …)` that
  calls `builder.over_transport(…)`. Both the `ssh` and `ephemeral_libvirt` branches use it, so
  both kinds now accept both providers.
- Remove the now-unused import of `RemoteLibvirtBuild`/`build_over_transport`; delete the
  now-dead free function `build_over_transport` from `remote_libvirt/build.py`.

## Test plan (TDD, behavior-first)

Unit (`tests/providers/build_host/test_artifact_publish.py` — new):

- `ArtifactBytes` → `put_artifact` with the right tenant/owner/sensitivity/retention; no presign.
- `ArtifactRemoteFile` → presign bound to base64(sha256(file)) + host size; `upload_file` called;
  no `put_artifact`; worker never reads the file bytes.
- `sha256sum`/`stat` non-zero or unparseable → `BUILD_FAILURE`.

Local provider (`tests/providers/local_libvirt/test_build.py` — update + add):

- Existing happy/preflight/failure tests pass with the `ArtifactSource` seams (warm =
  `ArtifactBytes`).
- New `over_transport` test (fake transport, same `_FakeTransport`/`_FakeStore` style as the
  remote transport test): build publishes `bzImage`+`vmlinux` via presign, returns the keys +
  build-id, and the worker never reads the artifact bytes (only the objcopy note). Asserts the
  checkout/`olddefconfig`/`read_config`/`make` steps ran over the transport.

Handler (`tests/jobs/handlers/test_build_handler_transport.py` — update + add):

- Update the `build_over_transport` monkeypatches to the new `bind_over_transport` seam.
- New: an `ssh` host with a **local-libvirt** runtime builder (a real
  `LocalLibvirtBuild.from_env`, whose `over_transport` is exercised via the patched bind seam)
  succeeds and releases the lease — the case that returns `NOT_IMPLEMENTED` today.
- A builder lacking `over_transport` (e.g. the `_RecordingBuilder` fake) still fails
  `NOT_IMPLEMENTED` and retains the lease.

## Guardrails

`just lint` · `just type` (whole tree) · `just test`. CI runs the recipes individually
(`lint`/`type`/`test`), so all must be green locally before each commit.

## Risks

- **Constructor-shape change to `LocalLibvirtBuild`** ripples to every test that builds one
  directly. Mitigated: it is an internal provider-assembly contract; updated in the same change.
- **The neutral extraction touches `remote_libvirt/build.py`.** Mitigated: re-export the moved
  names; the remote transport tests pin behavior-preservation.
- **Capability dispatch also admits `local` on `ephemeral_libvirt`.** This is the deliberate
  provider-neutral consequence (recorded in ADR-0101): the handler no longer returns
  `NOT_IMPLEMENTED` for that pairing. It is **not** an end-to-end guarantee. The ephemeral
  lifecycle (`ephemeral_build_session`, ADR-0100) provisions the throwaway build VM on the
  single `KDIVE_REMOTE_LIBVIRT_*`-configured host, so the combination requires **both**
  remote-libvirt (for the build VM) and local-libvirt (for the System) configured; a local-only
  deployment hits `CONFIGURATION_ERROR` at provision time exactly as the remote provider does.
  Only the **dispatch wiring** is unit-tested (with a fake ephemeral session); the real
  provision→build→teardown path stays under the existing `live_vm` gate. The tests here prove the
  seam admits the builder, not that the end-to-end pairing functions.
