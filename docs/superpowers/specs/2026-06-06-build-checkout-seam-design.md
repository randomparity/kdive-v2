# Build checkout seam — design (issue #125, gap G1)

- **Status:** Draft
- **Date:** 2026-06-06
- **Issue:** [#125](https://github.com/randomparity/kdive/issues/125) (gap **G1** of
  [#123](https://github.com/randomparity/kdive/issues/123)).
- **Depends on:** [ADR-0029](../../adr/0029-build-plane-local-make.md) (the build plane,
  the `ServerBuildProfile`, the injected-seam shape, and the
  `configuration_error` vs `build_failure` split this seam slots into).
- **ADR:** [ADR-0053](../../adr/0053-build-checkout-seam.md) (the open decisions this
  spec settles).
- **Port from:** `~/src/kdive-v1` `LocalKernelBuildProvider` (kernel checkout/config path).

## 1. Problem

`src/kdive/providers/local_libvirt/build.py:_real_checkout` is the last placeholder in an
otherwise-real build plane. Everything else in `build()` is wired: the `.config`
preflight (`CONFIG_CRASH_DUMP` + DWARF/BTF), `_real_run_make` (`make -C`),
`_real_read_build_id` (`objcopy` + the unit-tested note parser), and the two-artifact
store. The stub raises `MISSING_DEPENDENCY`, so a server-build Run cannot produce a real
kernel and the live demo (#123) is blocked at its first plane.

`build()` runs in-tree (`make -C workspace`), so the per-Run `workspace` must be a
self-contained source tree. The seam's job is to populate it: bring in the source, stage
the resolved `.config`, and apply the profile's optional patch — the input demo step 4
("agent writes a fix") rides on.

## 2. Scope

In scope (one file + tests):

- `src/kdive/providers/local_libvirt/build.py` — replace `_real_checkout`'s `raise` with
  a real warm-tree checkout, decomposed into testable helpers.
- `tests/providers/local_libvirt/test_build.py` — unit tests for the host-free helpers
  (config staging, patch application, ref resolution) and the error contract.

Out of scope: the rsync full-tree copy and the full `make` stay behind the existing
`live_vm` gate (no toolchain in CI); object-store / `https://` ref resolution (that is the
external-build lane, [ADR-0048](../../adr/0048-external-build-artifact-ingestion.md)); any
change to `build()`'s orchestration, the profile schema, or the artifact store.

## 3. Source of the warm tree

The source is the operator-provided warm tree at `KDIVE_KERNEL_SRC` (`~/src/linux` on the
demo host), **not** a clone of `profile.kernel_source_ref`. The env is the source of truth
for the server-build lane: it is the tree the operator keeps pre-built (warm), and
`from_env` already threads it into the checkout seam (`_make_checkout(kernel_src)`).
`profile.kernel_source_ref` is recorded provenance on the Run; the warm-tree lane does not
clone it and does not validate the tree against it (out of scope — a future
provenance-verification follow-on).

A patch must not mutate the shared warm tree, and concurrent Runs must not collide, so the
seam copies the source into the per-Run `workspace` (`workspace_root / run_id`) and patches
the copy.

## 4. Behavior

`_real_checkout(kernel_src, profile, workspace)` performs three ordered steps:

### 4a. Sync the warm tree → workspace (`live_vm`)

`rsync -a --delete <kernel_src>/ <workspace>/`. Rationale:

- `-a` preserves the source's build products (`.o`, `.cmd`, `vmlinux`…), so the in-tree
  `make` is incremental relative to the operator's warm build state — only what the patch
  touches recompiles.
- `--delete` makes a re-sync into a partially-populated `workspace` (a crashed prior
  attempt) exactly mirror the source: idempotent.
- The trailing slash on `<kernel_src>/` copies the tree *contents* into `workspace`, not a
  nested `workspace/linux/`.

Failure mapping:

- `kernel_src` empty or not an existing directory → `CONFIGURATION_ERROR` (the operator has
  not pointed `KDIVE_KERNEL_SRC` at a tree). Checked before invoking rsync.
- `rsync` binary absent → `MISSING_DEPENDENCY`.
- `rsync` non-zero exit → `INFRASTRUCTURE_FAILURE` (a copy failure — disk full, permissions
  — is host infrastructure, not a build/config defect), with a redacted stderr tail.

### 4b. Stage the `.config`

Resolve `profile.config_ref` as a **local** reference and copy its bytes to
`workspace/.config` (overwriting whatever `.config` the warm tree carried, so the resolved
config is deterministic per profile). `_real_read_config` reads `workspace/.config`
immediately after checkout, so this is the file the preflight inspects.

`config_ref` resolution (`_resolve_local_ref`): a `file://` URL (parsed to its path) or a
bare absolute path. A non-local scheme (`http`, `https`, `git+…`, `s3`) or a path that does
not resolve to an existing regular file → `CONFIGURATION_ERROR` (a bad/unsupported
reference the operator fixes). This step needs no toolchain and is unit-tested host-free.

### 4c. Apply the patch (when present)

If `profile.patch_ref` is set, resolve it with the same `_resolve_local_ref` rules and apply
it with `git apply -p1 <patch>` (cwd = `workspace`). `git apply` is used outside a strict
repository context — it patches files like `patch(1)` and does not require the workspace to
be a git work tree (though rsync copies `.git` anyway). It is the natural consumer of a
`git diff` (what an agent's "write a fix" produces).

Failure mapping:

- `patch_ref` resolves to a non-local scheme or a missing file → `CONFIGURATION_ERROR`.
- `git` binary absent → `MISSING_DEPENDENCY`.
- `git apply` non-zero exit (the patch does not apply against this tree) →
  `CONFIGURATION_ERROR`, with a redacted stderr tail in `details`. Per ADR-0029 §3, an
  operator/agent-supplied input that doesn't fit the tree is a configuration defect to fix,
  distinct from a `make`/toolchain `build_failure`.

A failure aborts `build()` before `make`; the per-Run workspace is disposable, so a
partially-applied patch is never built (the Run drives to `failed`).

## 5. Decomposition (keeps the injected-seam shape)

`_real_checkout` composes four helpers so the host-free logic is unit-tested and only the
rsync/`make` path stays `live_vm`:

| helper | host-bound? | tested |
|--------|-------------|--------|
| `_resolve_local_ref(ref) -> Path` | no | host-free unit |
| `_stage_config(config_ref, workspace)` | no (file copy) | host-free unit |
| `_apply_patch(patch_ref, workspace)` | git only | unit (skip if no `git`) |
| `_sync_tree(kernel_src, workspace)` | rsync | `live_vm` |
| `_real_checkout(...)` (composition) | rsync + git | `live_vm` |

`_real_checkout` keeps its `# pragma: no cover - live_vm` (it shells out to rsync); the
helpers it delegates to lose the pragma and are covered directly. The injected `checkout`
seam in `LocalLibvirtBuild` is unchanged, so the existing fake-seam unit tests of `build()`
still drive the orchestration host-free.

## 6. Redaction

`git apply` / `rsync` stderr can echo file paths and patch content. The stderr tail placed
in error `details` is passed through `Redactor().redact_text` (the same boundary util the
rest of the plane uses) and bounded to a short suffix before it is returned or persisted.

## 7. Error taxonomy summary

| condition | category |
|-----------|----------|
| `KDIVE_KERNEL_SRC` unset / not a directory | `configuration_error` |
| `config_ref` / `patch_ref` non-local scheme or missing file | `configuration_error` |
| patch does not apply | `configuration_error` |
| `rsync` / `git` binary absent | `missing_dependency` |
| `rsync` copy failure (disk/permissions) | `infrastructure_failure` |

## 8. Testing

Host-free unit tests (run in the normal suite):

- `_resolve_local_ref`: `file://` URL → path; bare absolute path → path; `https://` →
  `CONFIGURATION_ERROR`; missing file → `CONFIGURATION_ERROR`.
- `_stage_config`: copies bytes to `workspace/.config`, overwriting an existing one; a
  missing `config_ref` file → `CONFIGURATION_ERROR`.
- `_apply_patch` (skip if `shutil.which("git") is None`): a clean unified diff applies and
  the target file content changes; a conflicting patch → `CONFIGURATION_ERROR` and the
  target is reported in a redacted detail, not the raw patch.

`live_vm` (existing gate): the real-`make` test already in the file asserts the produced
`vmlinux`'s build-id equals `readelf -n`; with the seam implemented it now also exercises
the rsync sync end-to-end against `KDIVE_KERNEL_SRC`.

## 9. Acceptance (from the issue)

- A server-build Run against `~/src/linux` produces a real `bzImage` + `vmlinux` with a
  build-id via the existing `build()` orchestration. → `live_vm`.
- A `patch_ref` is applied; a bad patch is a clean categorized failure. → unit-tested
  (`CONFIGURATION_ERROR`).
- Honors ADR-0029 (build plane); keeps the injected-seam shape so unit tests stay
  host-free. → §5.
