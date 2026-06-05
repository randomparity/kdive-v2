# External-build artifact ingestion — design

- **Status:** Draft
- **Date:** 2026-06-05
- **Goal:** Let an agent build a kernel locally (with ordinary tools) and upload the
  finished artifacts so the rest of the build → boot → debug workflow can consume them,
  without first solving server-side kernel builds.
- **Depends on:** the Build plane (#18, [ADR-0029](../../adr/0029-build-plane-local-make.md)
  — the `BuildProfile`, `Run`/`run_steps` ledger, and `BuildOutput` contract this reuses),
  the object-store client ([ADR-0028](../../adr/0028-object-store-client.md)), and the
  Investigation/Run lifecycle (#17, the `runs.*` surface).
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

- `src/kdive/store/objectstore.py` — `presign_put(key, *, sha256, expires_in)` and
  `head(key)` (existence + size + checksum metadata).
- `src/kdive/profiles/build.py` — `BuildProfile.source: Literal["server", "external"]`
  (default `"server"`) plus the external manifest fields (declared artifact set, cmdline,
  rootfs reference).
- `src/kdive/mcp/tools/artifacts.py` — `artifacts.create_upload` (mint a presigned PUT per
  declared artifact under an owner's deterministic keys; owner is a Run for build
  artifacts or a System for a rootfs).
- `src/kdive/mcp/tools/runs.py` — `runs.complete_build` (validate the Run's uploads, record
  the `BuildOutput` + cmdline in the step ledger, drive `created → running → succeeded`).
- `src/kdive/profiles/provisioning.py` — extend the rootfs reference so `rootfs_image_ref`
  resolves a source kind (`upload` object key / `url` / `catalog` name), enabling "rootfs
  image or URL". The existing provisioning *attach* is unchanged.
- `src/kdive/providers/local_libvirt/build.py` — `validate_external_artifacts(...)`,
  reusing `parse_gnu_build_id` semantics for the shape checks.
- A ported rootfs catalog (`catalog`-kind references), from v1
  `src/kdive/rootfs/catalog_data.json`.

Out of scope (next spec): install-plane fetch from the store (gap B2), serial-marker
readiness (B4), the rootfs builder port (A1), and actually booting the uploaded kernel.
This lane only **produces and records** an install-ready Run.

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
  artifacts (kernel/initrd/vmlinux/cmdline) belong to the Run; the rootfs belongs to the
  System and is attached by the existing provisioning plane.

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
- Object keys are the **deterministic owner-keyed keys** the build plane already uses
  (`runs/<run_id>/<name>`, `systems/<system_id>/<name>`), so install/provisioning read them
  by the same convention.
- Each `upload_url` is a presigned PUT that pins the agent-declared `sha256` via S3's
  `x-amz-checksum-sha256`, so the store rejects a mismatched body on upload — integrity is
  enforced at the store with no server-side download.
- `size_bytes` is bounded by a configurable per-artifact cap; an implausible declaration is
  rejected before a URL is minted.
- Idempotent: re-calling re-mints (short-TTL) URLs for the same keys.

### `runs.complete_build`

```
in:  run_id,
     build_id?,                       # declared, from the agent's vmlinux; required iff vmlinux present
     cmdline                          # e.g. "console=ttyS0 dhash_entries=1"
out: ToolResponse{ run_id, status: "succeeded",
                   refs: [kernel, initrd?, vmlinux?],
                   suggested_next_actions: ["runs.get"] }
```

- Validates the Run's uploaded artifacts (§5), records `BuildOutput` + cmdline in the
  `run_steps` ledger, and drives `created → running → succeeded` under the per-Run advisory
  lock — the same finalize path as `_finalize_build`, fed by uploads. The rootfs is **not** an
  input here; it was bound to the System at provisioning. `suggested_next_actions` is
  `runs.get` because the System is already `ready` — the next real action (install/boot) is
  the following spec's tool.
- Idempotent via the existing `run_steps` `ON CONFLICT (run_id, step) DO NOTHING` plus the
  `_existing_build_result` short-read: a second `complete_build` is a no-op success.

## 5. Validation & integrity

At `complete_build`, per declared Run artifact (kernel/initrd/vmlinux):

- **Existence + size** — object-store `HEAD`; a missing object → `configuration_error`
  naming `artifacts.create_upload` (an upload was skipped or failed).
- **Integrity** — the store enforced `sha256` on PUT; `complete_build` reads back the
  checksum metadata and confirms it matches the declared digest. No full download.
- **Shape** — a small ranged read of the leading bytes asserts ELF magic (`\x7fELF`) for
  `vmlinux` and the bzImage magic for `kernel`; catches a truncated or wrong file cheaply.
- **`build_id`** — recorded as **agent-declared metadata** (extracted from the agent's
  local `vmlinux`), used for symbol pairing. It is **not** re-derived server-side, which
  would require downloading the multi-GB `vmlinux`. The `sha256` pin is the integrity
  anchor; `build_id` is the pairing hint. A deferred server-side `objcopy` (after the
  install/debug plane downloads the artifact anyway) is the future hardening, tracked in
  the install/debug spec, not here.
**Rootfs** is validated when its reference is resolved at System provisioning, not at
`complete_build`: `kind: upload` → `HEAD` the System-owned key; `kind: url` → a
reachability/`HEAD` check and a required declared `sha256`; `kind: catalog` → resolve the
name against the ported catalog (real, checksummed entries).

## 6. Error handling, state, security

- **State guards** — every transition goes through `domain/state.py` `can_transition`.
  `complete_build` acts only on a `CREATED` Run; `complete_build` on a `server`-source Run
  → `configuration_error`. A repeat `complete_build` is an idempotent success.
- **Error taxonomy** (existing `ErrorCategory`, no new strings) — missing upload →
  `configuration_error`; checksum/magic mismatch → `validation_failure` if defined for the
  surface, else `configuration_error`; presign/object-store failure →
  `infrastructure_failure`. Every failure envelope carries `error_category`, enforced at
  `ToolResponse` construction.
- **Concurrency** — `complete_build` holds the per-Run advisory lock, like
  `_build_locked`/`_finalize_build`, so concurrent completes serialize to a single ledger
  row.
- **Security** — presigned URLs are short-TTL and scoped to a single Run-keyed object key
  (no list, no other keys). Artifacts are stored `SENSITIVE` with the `build` retention
  class, as for server builds. No secrets cross the MCP boundary — the agent uploads
  straight to the store; the redactor covers any URL credentials in responses.

## 7. Testing

Tests mirror the package tree; the `ObjectStore` is injected, so no real S3 in unit tests.

- **Unit** — `create_upload` presign shaping (keys, TTL, checksum pin); state-guard
  rejections (wrong source, wrong Run state); `complete_build` happy path writes the ledger
  and drives `CREATED→SUCCEEDED`; idempotent re-complete; each validation failure path
  (missing object, checksum mismatch, bad magic, unreachable URL, unknown catalog name).
- **Adversarial** (`tests/adversarial/`) — concurrent `complete_build` on one Run
  serializes to a single ledger row.
- **`live_stack`-gated** — the real presigned round-trip against MinIO (upload then
  `complete_build`); skips cleanly without the stack.

## 8. Success criterion

Done = an agent can upload locally-built artifacts and a completed external-build Run lands
`succeeded` with a validated `BuildOutput` (`kernel_ref`, optional `vmlinux`/`build_id`)
plus a resolved rootfs reference — i.e., everything the install plane needs, stopping short
of booting. Install/boot consumption (B2 fetch, serial-marker readiness, the rootfs builder
port) is the next spec.
