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

Because both lanes share the one `created → running` transition, lane selection is a
**symmetric, enforced guard**, not a convention: `runs.build` rejects an `external`-source
Run and `runs.complete_build` rejects a `server`-source Run, each with
`configuration_error`. Without the `runs.build` half, an external Run could be driven into
the stubbed server `make` path or race a concurrent `complete_build` for the transition.

### 2. Presigned uploads — bytes go straight to the store, never through MCP

`artifacts.create_upload` mints a short-TTL presigned PUT per declared artifact, scoped to
a single owner-keyed object key — the existing `{tenant}/{kind}/{object_id}/{name}` layout
(`_artifact_key`), i.e. `{tenant}/runs/<run_id>/<name>` for build artifacts and
`{tenant}/systems/<system_id>/<name>` for a rootfs. The agent uploads multi-GB images
directly to the object store. This is the only transport that handles large images without
bloating the MCP transport and that works whether the agent is local or remote.
Inline-bytes and shared-local-path transports are rejected for not generalizing.

The presigned URL **signs the upload conditions**, not just the key: the agent-declared
`x-amz-checksum-sha256` (required header) and a `content-length-range` pinned to the
declared size. The store then rejects, at PUT time, a body whose checksum or length does
not match the signed declaration — so neither the size cap nor the integrity pin depends on
the client behaving. Enforcement of presigned-PUT checksums by MinIO is an assumption this
ADR records as a verification item (a `live_stack` test asserts a mismatched/oversized body
is rejected, §7 of the spec).

### 3. `complete_build` is synchronous, not a worker job

Server builds offload `make` (30+ min) to the worker. External ingestion has no
long-running step: validation is an object `HEAD`, a checksum-metadata read, and a small
ranged magic read. `runs.complete_build` therefore validates inline and finalizes the Run
under the per-Run advisory lock, rather than enqueuing a job. This is the one structural
difference from the server lane and keeps the ingestion contract simple.

### 4. Integrity is pinned at the store against a persisted manifest

`create_upload` **persists the declared manifest** — per artifact `(name, sha256,
size_bytes)` — as Run (or System) state at the moment it mints the URLs. That persisted
manifest is the reference value: `complete_build` reads each object's stored
`x-amz-checksum-sha256` (and size) via `head` and confirms it matches the manifest, with no
download. Without the persisted manifest there is nothing to compare against, so the
persistence is load-bearing, not incidental. Combined with decision 2's signed conditions,
integrity is anchored at two points (PUT-time rejection and finalize-time confirmation).

The GNU `build_id` is recorded as agent-declared metadata (extracted from the agent's local
`vmlinux`) for symbol pairing. The hazard this creates is explicit: a `build_id` that does
**not** match the uploaded `vmlinux` (stale value, wrong/mismatched vmlinux) produces
silently *mispaired* symbols downstream — the debug plane decodes a vmcore against the wrong
symbol table and yields plausible-looking garbage, corrupting the exact output the platform
exists to produce. To bound this within scope, `complete_build` verifies the declared
`build_id` against the uploaded `vmlinux` by extracting the `.note.gnu.build-id` server-side
**without downloading the whole file**: read the ELF header (`e_shoff`), then the section
header table, then the note section — a few S3 byte-range GETs feeding the existing
`parse_gnu_build_id`. (The note is not at a fixed offset, so this is section-header parsing,
not a single leading read — but still bytes, not gigabytes.) The full-artifact `objcopy`
re-derivation stays deferred to whenever a plane downloads the `vmlinux` anyway.

### 5. Rootfs is a System/provisioning input, supplied before the Run

`runs.create` binds a Run to an already-`ready` System, so provisioning — which attaches
the rootfs disk — precedes the Run. The rootfs is therefore **not** a `complete_build`
input. It is the existing `LibvirtProfile.rootfs_image_ref`, extended to resolve a source
kind: `upload` (a qcow2 uploaded via a System-owned `create_upload`), `url` (external URL +
declared `sha256`), or `catalog` (a name resolved against a catalog ported from v1). The
reference is validated when resolved at provisioning and attached by the existing
provisioning plane. This matches the "rootfs image or URL" requirement and keeps the
build/provisioning boundary intact.

> **Note.** The `upload` source kind awaits its producer: it needs a `DEFINED` System (the
> pre-provision upload window), and nothing creates a `DEFINED` System yet — the
> create-without-provision path (`systems.define`) is tracked by #111. Until then the
> provisioning tool boundary (`validate_rootfs_reference`) rejects an `upload` reference
> (fail-fast `configuration_error`, no dead-lettered job or leaked domain); the worker-side
> resolver and commit consumers remain as forward-plumbing. The `path`/`url`/`catalog` kinds
> are unaffected and usable today.

### 6. Orphaned uploads are prefix-reaped, no row state

Direct presigned upload separates the object write (done by the agent) from the
`artifacts`-row write. In the server lane `register_artifact_row` runs right after the
object write, so every stored object has a row the reconciler/retention sweep can see. Here,
an agent that calls `create_upload`, uploads, then never reaches `complete_build` (crash,
abandon, validation failure) would otherwise leave **rows-less objects** the reconciler
cannot reap — an unbounded storage leak.

The lane closes this **without** adding a row state. The `Artifact` row stays write-once
(`models.py`: "write-once"); `register_artifact_row` is written when an object is
*committed* — by `complete_build` for a Run's build artifacts, and by the provisioning plane
for a System's rootfs when it consumes the reference. The reconciler (ADR-0021) gains an
owner-agnostic pass: for any owner still in its pre-finalize state past an **upload
deadline** — a `created` Run, or a `defined`/never-provisioned System — it lists every
object under that owner's **key-prefix** (`{tenant}/runs/<run_id>/`,
`{tenant}/systems/<system_id>/`) and deletes only those with **no committed `artifacts`
row**. The "uncommitted past deadline" predicate is load-bearing: it exempts an object that
is referenced/in-flight (a rootfs an operator is slow to provision, a Run whose
`complete_build` hasn't run) so the reaper can never delete live input — only true orphans.
Prefix listing (not a manifest key-list) makes this bounded per owner and immune to a
re-mint that changed the declared set: any stray uncommitted object under the prefix is
swept, not just the keys the latest manifest happens to name. The deadline is a fixed TTL
(config) stamped on the persisted manifest at mint time. This covers the Run-owned build
artifacts and the System-owned rootfs upload uniformly, and keeps the "every committed
object has a row" invariant intact.

This needs two object-store primitives the store does not have today — `list(prefix)` and
`delete(key)` — added alongside `presign_put`/`head` (see consequences).

### 7. Scope stops at a recorded, well-formed Run

This lane produces and records a Run with **validated, well-formed** artifacts — checksum
matched against the manifest, ELF/bzImage magic present, `build_id` paired to the
`vmlinux`. It deliberately does **not** establish bootability: magic-plus-checksum cannot
prove a kernel boots, and a boot smoke-test is out of scope per the agreed ingestion-only
slice. "Install-ready" therefore means "well-formed and recorded, ready for the install
plane to attempt" — actual install/boot verification (the only signal that proves the
artifacts work) lands in the next spec, alongside install-plane fetch (gap B2),
serial-marker readiness (B4), and the rootfs builder port (A1).

## Consequences

- Unblocks the workflow without server-side `make`: a Run reaches `succeeded` with
  validated, well-formed artifacts from a local build. Bootability is proven only later, by
  the install/boot spec.
- Adds two `operator`-scoped tools (`artifacts.create_upload`, `runs.complete_build`), four
  object-store methods (`presign_put` with signed checksum + content-length conditions,
  `head`, and `list(prefix)` + `delete(key)` for the reaper), a `BuildProfile.source` field,
  a persisted upload manifest plus an owner-agnostic prefix reconciler reaper for abandoned
  uploads (no artifact-row state change — the row stays write-once), a `rootfs_image_ref`
  source-kind extension in the provisioning profile, and a ported rootfs catalog — no change
  to the install/boot/debug planes.
- The client-declared `build_id` is a deliberate, bounded trust point, mitigated by the
  store-enforced `sha256` and revisited when a plane downloads the `vmlinux` anyway.
- The synchronous `complete_build` means a future large server-side validation step (if one
  is ever added) would need to move back onto the job queue — acceptable, since none is
  planned for this lane.
