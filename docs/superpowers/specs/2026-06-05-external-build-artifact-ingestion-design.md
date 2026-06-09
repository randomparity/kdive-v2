# External-build artifact ingestion — design

- **Status:** Draft
- **Date:** 2026-06-05
- **Goal:** Let an agent build a kernel locally (with ordinary tools) and upload the
  finished artifacts so the rest of the build → boot → debug workflow can consume them,
  without first solving server-side kernel builds.
- **Depends on:** the Build plane (#18, [ADR-0029](../../adr/0029-build-plane-local-make.md)
  — the `BuildProfile`, `Run`/`run_steps` ledger, and `BuildOutput` contract this reuses),
  the object store ([ADR-0017](../../adr/0017-object-store-client-interface.md) /
  [ADR-0013](../../adr/0013-object-store-layout-retention.md)), and the Investigation/Run
  lifecycle (#17, the `runs.*` surface).
- **ADR:** [ADR-0048](../../adr/0048-external-build-artifact-ingestion.md) (the decisions
  this spec settles).

## 1. Problem

The server-side Build plane is the only way artifacts enter the object store today, and
its host-touching seams are stubs (`_real_checkout` raises; gap B1). The
agent-facing tools `artifacts.list`/`artifacts.get` are read-only — there is **no
ingestion path**. So nothing downstream (install → boot → debug) can run, because no Run
ever acquires a real kernel.

This design adds a **parallel build lane**: the agent builds a kernel with ordinary tools
on its own machine, uploads the finished artifacts directly to the object store, and the
Run lands `succeeded` with the same `BuildOutput` the install plane already reads. It
sidesteps server-side `make` entirely, unblocking the workflow so later increments can
implement install/boot against a real, install-ready Run.

## 2. Scope

In scope:

- `src/kdive/store/objectstore.py` — `presign_put(key, *, sha256, size_bytes, expires_in)`
  (signs the checksum + content-length conditions), `head(key)` (existence + size + checksum
  metadata), and `list(prefix)` + `delete(key)` for the reaper.
- `src/kdive/profiles/build.py` — make `BuildProfile` a **`source`-discriminated** schema.
  `BuildProfile` today is `extra="forbid", frozen=True` with required `kernel_source_ref` +
  `config_ref` (source-tree fields a local build does not have), so a flat field addition
  won't fit. Split by `source`: the `server` variant keeps `kernel_source_ref`/`config_ref`
  (+ optional `patch_ref`); the `external` variant requires none of them (the discriminator
  `source: external` is enough). `parse` dispatches on `source` (default `server`, preserving
  existing documents). The artifact manifest is declared at `create_upload` (§4) and the
  `cmdline` at `complete_build` (§4) — neither lives in the profile — and the rootfs stays on
  the provisioning profile (§3).
- `src/kdive/mcp/tools/artifacts.py` — `artifacts.create_upload` (mint a presigned PUT per
  declared artifact under an owner's deterministic keys; owner is a Run for build
  artifacts or a System for a rootfs).
- `src/kdive/mcp/tools/runs.py` — `runs.complete_build` (validate the Run's uploads, record
  the `BuildOutput` + cmdline in the step ledger, drive `created → running → succeeded`).
- `src/kdive/profiles/provisioning.py` + the provisioning plane — extend the rootfs
  reference so `rootfs_image_ref` resolves a source kind (`upload` object key / `url` /
  `catalog` name), enabling "rootfs image or URL"; when it consumes an `upload`-kind rootfs,
  the plane commits its write-once `artifacts` row (so the reaper exempts it). The existing
  disk *attach* is otherwise unchanged.
- `src/kdive/providers/local_libvirt/build.py` — `validate_external_artifacts(...)`,
  reusing `parse_gnu_build_id` (shape checks + the ranged `build_id` extraction).
- `src/kdive/db/` (migration) — owner-scoped upload-manifest storage (the declared
  `(name, sha256, size_bytes)` set + deadline written at `create_upload`); no change to the
  write-once `artifacts` row shape.
- the reconciler (ADR-0021) — an owner-agnostic reaper that prefix-lists an owner's objects
  (a `created` Run or a `defined`/never-provisioned System) and deletes the uncommitted ones
  past the upload deadline (§6). No `artifacts`-row state change (the row stays write-once).
- Rootfs `catalog`-kind references now resolve through the provider fixture catalog
  (`fixtures/local-libvirt/manifest.yaml` and `kdive.provider_components.catalog`).

Out of scope (next spec): install-plane fetch from the store (gap B2), serial-marker
readiness (B4), the rootfs builder port (A1), and actually booting the uploaded kernel.
This lane only **produces and records** a well-formed Run; it does not prove the kernel
boots.

## 3. Domain mapping: reuse the Run, swap the producer

The server-build lane is:

```
runs.create  → Run CREATED (build_profile stored, bound to a ready System)
runs.build   → CREATED→RUNNING, enqueue a make job; _finalize_build writes the
               run_steps ledger and goes RUNNING→SUCCEEDED
```

The external lane reuses that **state machine and ledger** verbatim, replacing the worker
`make` job with client-uploaded bytes. Because `runs.create` binds a Run to an
**already-`ready` System** (provision precedes the Run), the rootfs is supplied earlier, at
System provisioning — *not* during the Run:

```
# rootfs (System/provisioning-time): upload a local rootfs OR name a url/catalog ref
artifacts.create_upload(system_id, [{name:"rootfs", ...}])  → presigned PUT (if uploading)
systems.* provision the System with rootfs_image_ref = <resolved ref>   → System READY

# build artifacts (Run-time):
runs.create(build_profile.source = "external")          → Run CREATED on the ready System
artifacts.create_upload(run_id, [kernel, initrd?, vmlinux?])  → one presigned PUT each
  agent PUTs bytes straight to the object store (off the MCP transport)
runs.complete_build(run_id, build_id?, cmdline)         → validate → write BuildOutput
                                                          → CREATED→RUNNING→SUCCEEDED
```

Settled properties:

- **No new `RunState`, no new domain object.** An external-build Run is an ordinary Run;
  `source: external` selects the lane. The recorded `BuildOutput` (`kernel_ref`,
  `debuginfo_ref`, `build_id`) lands in the same `run_steps` row the install plane reads,
  so install → boot → debug are unchanged. A/B (vulnerable vs. fixed kernel) is two Runs
  over one System, exactly as for server builds.
- **`complete_build` is synchronous, not a worker job.** Validation is cheap (object
  `HEAD` + checksum metadata + a small ranged magic read), so there is no long-running
  step to offload. This is the one structural difference from the server lane.
- **Rootfs is a System/provisioning input, supplied before the Run.** It is the existing
  `LibvirtProfile.rootfs_image_ref`, extended to resolve a source kind — `upload` (an
  uploaded qcow2 object key from a System-owned `create_upload`), `url` (an external URL +
  declared `sha256`), or `catalog` (a name resolved against the ported catalog). The build
  artifacts (kernel/initrd/vmlinux) and the `cmdline` belong to the Run; the rootfs belongs
  to the System and is attached by the existing provisioning plane.

## 4. Tool surface

Both tools are `operator` RBAC, matching `runs.create`/`runs.build`.

### `artifacts.create_upload`

```
in:  owner_kind: "run" | "system",
     owner_id,
     artifacts: [{ name: "kernel" | "initrd" | "vmlinux" | "rootfs", sha256, size_bytes }]
out: ToolResponse{ uploads: [{ name, key, upload_url, expires_in }],
                   suggested_next_actions: ["runs.complete_build"]   # or systems.* for a rootfs
                 }
```

- For `owner_kind: run` the Run must be `CREATED` with profile `source: external`, and the
  names are build artifacts (`kernel`/`initrd`/`vmlinux`); for `owner_kind: system` the name
  is `rootfs` and the System must not yet be provisioned. A mismatch → `configuration_error`.
- Object keys are the existing `{tenant}/{kind}/{object_id}/{name}` layout (`_artifact_key`):
  `{tenant}/runs/<run_id>/<name>`, `{tenant}/systems/<system_id>/<name>`, so
  install/provisioning read them by the same convention.
- Each `upload_url` is a presigned PUT that **signs the upload conditions**: the
  agent-declared `x-amz-checksum-sha256` (required header) and a `content-length-range`
  pinned to the declared `size_bytes`. The store rejects, at PUT time, a body whose checksum
  or length does not match — so the cap and the integrity pin do not depend on the client
  behaving. (MinIO presigned-PUT checksum enforcement is the `live_stack`-tested assumption.)
- `create_upload` **persists the declared manifest** — per artifact `(name, sha256,
  size_bytes)`, plus an upload-deadline timestamp — in owner-scoped mutable state (a
  dedicated upload-manifest column/row keyed by owner), **not** the immutable `build_profile`.
  The manifest holds the **checksum reference values** `complete_build` compares each stored
  object against, and the **deadline** the reaper keys off (the reaper itself lists by
  prefix, not from the manifest — §6). No `artifacts` row is written yet — the row stays
  write-once and is created at `complete_build`. `size_bytes` over a configurable cap is
  rejected before a URL is minted.
- **Manifest semantics: one call, full set.** A `create_upload` call **replaces** the
  owner's manifest with the declared set — the agent declares all of an owner's artifacts in
  a single call. Re-calling is therefore idempotent (re-mints short-TTL URLs, rewrites the
  same manifest) and also the way to correct a declaration before finalize; it is **not** a
  way to add artifacts incrementally (a second call with a narrower set drops the others
  from the manifest, and their objects become uncommitted — prefix-reaped on abandon). The
  kernel-required check (§5) reads this single authoritative manifest.

### `runs.complete_build`

```
in:  run_id,
     build_id?,                       # declared, from the agent's vmlinux; required iff vmlinux present
     cmdline                          # debug args, e.g. "dhash_entries=1"
out: ToolResponse{ run_id, status: "succeeded",
                   refs: [kernel, initrd?, vmlinux?],
                   suggested_next_actions: ["runs.get"] }
```

- Validates the Run's uploaded artifacts (§5), writes the write-once `artifacts` rows
  (`register_artifact_row`, as in the server lane), records `BuildOutput` + cmdline in the
  `run_steps` ledger, and drives `created → running → succeeded` under the per-Run advisory
  lock — the same finalize path as `_finalize_build`, fed by uploads. The rootfs is **not** an input here; it was bound to the System at
  provisioning. `suggested_next_actions` is `runs.get` because the System is already `ready`
  — the next real action (install/boot) is the following spec's tool.
- Idempotent, and the order is load-bearing: `complete_build` **first** consults the
  `run_steps` `_existing_build_result` short-read and returns the recorded success if the
  build step is already finalized — **before** applying the `CREATED`/`source` state guard
  (§6). So a retry after a dropped connection (the Run is now `succeeded`, not `CREATED`)
  returns the prior success, not an illegal-transition/`configuration_error`. Only a Run with
  no finalized build step reaches the guard. The write-once `artifacts` rows are likewise
  written under the short-read, so a retry never double-inserts. (This mirrors how the server
  lane's `_finalize_build` short-read shields a re-dispatched job.)

## 5. Validation & integrity

At `complete_build`:

- **Required set** — a `kernel` artifact must be present in the manifest and uploaded
  (`HEAD` hit); a `complete_build` with no `kernel` → `configuration_error` (the install
  plane reads `kernel_ref` as mandatory). `initrd`/`vmlinux` are optional. This is the one
  invariant that gates finalize regardless of what the agent declared.

Then, per declared Run artifact (kernel/initrd/vmlinux):

- **Existence + size** — object-store `HEAD`; a missing object → `configuration_error`
  naming `artifacts.create_upload` (an upload was skipped or failed).
- **Integrity** — the store enforced the signed `sha256` + length on PUT; `complete_build`
  reads each object's stored checksum/size via `head` and confirms it matches the
  **persisted manifest** from `create_upload`. No full download.
- **Shape** — a small ranged read of the leading bytes asserts ELF magic (`\x7fELF`) for
  `vmlinux` and the bzImage magic for `kernel`; catches a truncated or wrong file cheaply.
- **`build_id`** — verified, not merely trusted: when a `vmlinux` is present, the declared
  `build_id` is checked against the uploaded file by extracting its `.note.gnu.build-id`
  server-side **without a full download** — read the ELF header (`e_shoff`), then the
  section header table, then the note section (a few byte-range GETs feeding the existing
  `parse_gnu_build_id`). A mismatch → `build_failure` (a defective uploaded build, the same
  category ADR-0029 uses for a server build with no build-id). This guards against silently
  *mispaired* symbols (the debug plane decoding a vmcore against the wrong symbol table —
  plausible-looking garbage). Full-artifact `objcopy` re-derivation stays deferred to a
  plane that downloads the `vmlinux` anyway.
**Rootfs** is validated when its reference is resolved at System provisioning, not at
`complete_build`: `kind: upload` → `HEAD` the System-owned key; `kind: url` → a
reachability/`HEAD` check and a required declared `sha256`; `kind: catalog` → resolve the
name against the ported catalog (real, checksummed entries).

## 6. Error handling, state, security

- **State guards (symmetric lane gate)** — every transition goes through `domain/state.py`
  `can_transition`. Both entry tools are source-gated: `complete_build` acts only on a
  `CREATED` `external`-source Run (a `server`-source Run → `configuration_error`), and
  `runs.build` rejects an `external`-source Run with `configuration_error`. Without the
  `runs.build` half, an external Run could be driven into the stubbed server `make` path or
  race a concurrent `complete_build` for the single `created → running` transition. A repeat
  `complete_build` is an idempotent success.
- **Orphan lifecycle (prefix-reaped, no row state)** — the `artifacts` row stays write-once
  and is written when an object is *committed*: by `complete_build` for build artifacts, by
  the provisioning plane for a rootfs it consumes. The reconciler (ADR-0021) reaps by **owner
  key-prefix**: for any owner still in its pre-finalize state past the upload deadline — a
  `created` Run **or** a `defined`/never-provisioned System — it `list`s every object under
  `{tenant}/runs/<run_id>/` or `{tenant}/systems/<system_id>/` and `delete`s only those with
  **no committed `artifacts` row**. The "uncommitted past deadline" predicate exempts a
  referenced/in-flight object (a rootfs the operator is slow to provision, a Run before
  `complete_build`), so the reaper never deletes live input — only true orphans. Prefix
  listing (not the manifest's current key-list) is immune to a re-mint that dropped a
  declared name. The deadline is a fixed TTL stamped on the manifest at mint time.
- **Error taxonomy** (existing `ErrorCategory`, no new strings) — missing/skipped upload →
  `configuration_error` (an input the agent didn't provide); a defective uploaded artifact
  (checksum/size mismatch vs. manifest, bad ELF/bzImage magic, `build_id` mismatch) →
  `build_failure` (matching ADR-0029's defective-build categorization); presign/object-store
  failure → `infrastructure_failure`. Every failure envelope carries `error_category`,
  enforced at `ToolResponse` construction.
- **Concurrency** — `complete_build` holds the per-Run advisory lock, like
  `_build_locked`/`_finalize_build`, so concurrent completes serialize to a single ledger
  row.
- **Security** — presigned URLs are short-TTL and scoped to a single owner-keyed object key
  (no list, no other keys). Artifacts are stored `SENSITIVE` with the `build` retention
  class, as for server builds. No secrets cross the MCP boundary — the agent uploads
  straight to the store; the redactor covers any URL credentials in responses.

## 7. Testing

Tests mirror the package tree; the `ObjectStore` is injected, so no real S3 in unit tests.

- **Unit** — `create_upload` presign shaping (keys, TTL, signed checksum + length
  conditions), manifest + deadline persistence (no `artifacts` row yet); state-guard
  rejections both directions
  (`runs.build` on external, `complete_build` on server, wrong Run state); `complete_build`
  happy path writes the write-once `artifacts` rows, writes the ledger, drives
  `CREATED→SUCCEEDED`; idempotent
  re-complete; each validation failure path (missing object, checksum/size mismatch vs.
  manifest, bad magic, `build_id` mismatch vs. the uploaded `vmlinux`, unreachable rootfs
  URL, unknown catalog name).
- **Adversarial** (`tests/adversarial/`) — concurrent `complete_build` on one Run
  serializes to a single ledger row; the reconciler prefix-reaps every **uncommitted** object
  under an abandoned `created` Run's and an abandoned `defined` System's key-prefix past the
  deadline (including a dropped-on-re-mint name), but **leaves a committed object** (a
  rootfs row written by a slow-but-real provision) untouched — no untracked object survives,
  no live input is destroyed.
- **`live_stack`-gated** — the real presigned round-trip against MinIO, asserting the store
  **rejects** a body whose checksum or length disagrees with the signed declaration, then a
  matching upload + `complete_build` succeeds; skips cleanly without the stack.

## 8. Success criterion

Done = an agent can upload locally-built artifacts and a completed external-build Run lands
`succeeded` with a **validated, well-formed** `BuildOutput` (`kernel_ref`, optional
`vmlinux`/`build_id`) — checksum matched against the manifest, magic present, `build_id`
paired to the `vmlinux` — over a System whose rootfs reference resolved. This is everything
the install plane needs to *attempt* a boot; it does **not** prove the kernel boots.
Bootability is established only by the next spec's install/boot work (B2 fetch, serial-marker
readiness, the rootfs builder port), which is where actual install/boot verification lives.
