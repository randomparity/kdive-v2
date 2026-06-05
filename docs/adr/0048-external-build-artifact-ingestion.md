# ADR 0048 — External-build artifact ingestion: agent uploads, no server-side make

- **Status:** Proposed
- **Date:** 2026-06-05
- **Depends on:** [ADR-0029](0029-build-plane-local-make.md) (the `BuildProfile`,
  `BuildOutput`, `runs.*` surface, and `run_steps` ledger this reuses),
  [ADR-0026](0026-investigation-run-lifecycle.md) (the Run lifecycle and `(run_id, step)`
  ledger), [ADR-0016](0016-repository-layer-locks-idempotency.md) (the per-Run advisory
  lock and step ledger), [ADR-0017](0017-object-store-client-interface.md) /
  [ADR-0013](0013-object-store-layout-retention.md) (the artifact store).
- **Spec:** [`../superpowers/specs/2026-06-05-external-build-artifact-ingestion-design.md`](../superpowers/specs/2026-06-05-external-build-artifact-ingestion-design.md)

## Context

The server-side Build plane is the only producer of object-store artifacts, and its
host-touching seams are stubs (`_real_checkout` raises). `artifacts.list`/`artifacts.get`
are read-only — there is no ingestion path, so no Run ever acquires a real kernel and
nothing downstream (install → boot → debug) can run.

To begin chipping away at the build → boot → debug workflow without first implementing
server-side kernel builds, we let an agent build locally and upload finished artifacts.
Several decisions follow; they are settled here so reviews do not re-litigate them.

## Decision

### 1. A parallel build lane selected by `BuildProfile.source`, reusing the Run

`BuildProfile` gains `source: Literal["server", "external"]` (default `"server"`). An
external-build Run is an **ordinary Run** — no new `RunState`, no new domain object. The
external lane reuses the `created → running → succeeded` state machine and the `run_steps`
ledger; it only swaps the producer of the artifacts (client uploads instead of a server
`make` job). The recorded `BuildOutput` (`kernel_ref`, `debuginfo_ref`, `build_id`) lands
in the same ledger row the install plane reads, so install → boot → debug are unchanged,
and A/B (vulnerable vs. fixed) remains two Runs over one System.

### 2. Presigned uploads — bytes go straight to the store, never through MCP

`artifacts.create_upload` mints a short-TTL presigned PUT per declared artifact, scoped to
a single owner-keyed object key (`runs/<run_id>/<name>` for build artifacts,
`systems/<system_id>/<name>` for a rootfs). The agent uploads multi-GB images directly to
the object store. This is the only transport that handles large images without bloating the
MCP transport and that works whether the agent is local or remote. Inline-bytes and
shared-local-path transports are rejected for not generalizing.

### 3. `complete_build` is synchronous, not a worker job

Server builds offload `make` (30+ min) to the worker. External ingestion has no
long-running step: validation is an object `HEAD`, a checksum-metadata read, and a small
ranged magic read. `runs.complete_build` therefore validates inline and finalizes the Run
under the per-Run advisory lock, rather than enqueuing a job. This is the one structural
difference from the server lane and keeps the ingestion contract simple.

### 4. Integrity is pinned at the store; `build_id` is client-declared metadata

The presigned PUT pins the agent-declared `sha256` via S3's `x-amz-checksum-sha256`, so the
store rejects a mismatched body on upload and `complete_build` confirms integrity from
checksum metadata with no download. The GNU `build_id` is recorded as agent-declared
metadata (extracted from the agent's local `vmlinux`) for symbol pairing; it is **not**
re-derived server-side, which would require downloading the multi-GB `vmlinux`. The
`sha256` pin is the integrity anchor; `build_id` is the pairing hint. A deferred
server-side `objcopy` — once the install/debug plane downloads the artifact anyway — is the
future hardening, and belongs to that spec, not this one.

### 5. Rootfs is a System/provisioning input, supplied before the Run

`runs.create` binds a Run to an already-`ready` System, so provisioning — which attaches
the rootfs disk — precedes the Run. The rootfs is therefore **not** a `complete_build`
input. It is the existing `LibvirtProfile.rootfs_image_ref`, extended to resolve a source
kind: `upload` (a qcow2 uploaded via a System-owned `create_upload`), `url` (external URL +
declared `sha256`), or `catalog` (a name resolved against a catalog ported from v1). The
reference is validated when resolved at provisioning and attached by the existing
provisioning plane. This matches the "rootfs image or URL" requirement and keeps the
build/provisioning boundary intact.

### 6. Scope stops at an install-ready Run

This lane produces and records an install-ready Run; it does not boot. Install-plane fetch
from the store (gap B2), serial-marker readiness (B4), and the rootfs builder port (A1)
are explicitly out of scope and tracked in the next spec.

## Consequences

- Unblocks the workflow end-to-end without server-side `make`: a Run can reach an
  install-ready `succeeded` state from locally-built artifacts.
- Adds two `operator`-scoped tools (`artifacts.create_upload`, `runs.complete_build`), two
  object-store methods (`presign_put`, `head`), a `BuildProfile.source` field, a
  `rootfs_image_ref` source-kind extension in the provisioning profile, and a ported rootfs
  catalog — no change to the install/boot/debug planes.
- The client-declared `build_id` is a deliberate, bounded trust point, mitigated by the
  store-enforced `sha256` and revisited when a plane downloads the `vmlinux` anyway.
- The synchronous `complete_build` means a future large server-side validation step (if one
  is ever added) would need to move back onto the job queue — acceptable, since none is
  planned for this lane.
