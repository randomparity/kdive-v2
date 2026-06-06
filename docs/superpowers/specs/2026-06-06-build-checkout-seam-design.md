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
  (config staging, patch application, ref resolution), the `_real_checkout` composition
  (order/wiring), and the error contract; plus filling the existing
  `test_live_vm_real_make_build_id_matches_readelf` stub with the real end-to-end assertion
  (still `live_vm`-gated).

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

`_sync_tree` **creates the workspace first** (`workspace.mkdir(parents=True,
exist_ok=True)`) — `build()` computes `workspace = workspace_root / run_id` but does not
create it, and `rsync` only creates the destination's final path component, not missing
parents. Creating it here means a missing `KDIVE_BUILD_WORKSPACE` root is handled
deterministically rather than surfacing as an opaque rsync copy failure. It then runs:

`rsync -a --delete <kernel_src>/ <workspace>/`. Rationale:

- `-a` preserves the source's build products (`.o`, `.cmd`, `vmlinux`…), so the in-tree
  `make` is incremental relative to the operator's warm build state — only what the patch
  touches recompiles.
- `--delete` makes a re-sync into a partially-populated `workspace` (a crashed prior
  attempt) exactly mirror the source: idempotent. Because the resetting rsync runs on every
  checkout *before* config-staging and patch-application, a re-dispatched checkout always
  starts from a pristine tree — so re-applying the patch (4c) never hits the
  "already-applied → does not apply" failure; the whole `_real_checkout` is idempotent.
- The trailing slash on `<kernel_src>/` copies the tree *contents* into `workspace`, not a
  nested `workspace/linux/`.

Failure mapping:

- `kernel_src` empty or not an existing directory → `CONFIGURATION_ERROR` (the operator has
  not pointed `KDIVE_KERNEL_SRC` at a tree). Checked before invoking rsync.
- `rsync` binary absent → `MISSING_DEPENDENCY`.
- `rsync` non-zero exit → `INFRASTRUCTURE_FAILURE` (a copy failure — disk full, permissions
  — is host infrastructure, not a build/config defect), with a redacted stderr tail.

The workspace root (`KDIVE_BUILD_WORKSPACE`, default `/var/lib/kdive/build`) being
worker-writable is an operator prerequisite (epic #123 host-prep); `_sync_tree`'s `mkdir`
creates the per-Run leaf and any missing parents, but a non-writable root still fails as
the rsync/`mkdir` error it is.

### 4b. Stage the `.config`

Resolve `profile.config_ref` as a **local** reference and copy its bytes to
`workspace/.config` (overwriting whatever `.config` the warm tree carried, so the resolved
config is deterministic per profile). `_real_read_config` reads `workspace/.config`
immediately after checkout, so this is the file the preflight inspects.

`config_ref` is assumed to be a **complete, target-tree-matched `.config`**, not a kconfig
*fragment*. The seam copies it verbatim; it does not run `make olddefconfig`/`syncconfig`
(that is `build()`'s `make` step, unchanged here). Two consequences the operator must know,
recorded as known limitations rather than silently assumed:

- A *fragment* (or a config from a mismatched kernel version) leaves symbols unspecified;
  kbuild's `syncconfig` (run by the first `make`) fills them with defaults, so the built
  config can differ from the staged bytes.
- The §4b/preflight check (`_missing_config_groups`, in `build()`) inspects the *staged*
  `.config`, not the *post-`syncconfig`* effective config. A config that passes preflight
  but whose `CONFIG_CRASH_DUMP`/debuginfo option is dropped by `syncconfig` for an unmet
  dependency would still build. Re-preflighting the effective config is a possible
  follow-on; it is out of scope here (it changes `build()`'s `make` orchestration).

`config_ref` resolution (`_resolve_local_ref`): a `file://` URL with an **empty** authority
(`file:///abs/path`) or a bare **absolute** path. Rejected as `CONFIGURATION_ERROR` (a
bad/unsupported reference the operator fixes): a non-local scheme (`http`, `https`,
`git+…`, `s3`); a `file://` URL with a non-empty netloc (e.g. `file://host/path`, a
remote-file URL whose host `urlsplit` would silently drop); a resolved path that is not
absolute; or a path that does not resolve to an existing regular file. This step needs no
toolchain and is unit-tested host-free.

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
single rsync subprocess leaf stays `live_vm`:

| helper | host-bound? | tested |
|--------|-------------|--------|
| `_resolve_local_ref(ref) -> Path` | no | host-free unit |
| `_stage_config(config_ref, workspace)` | no (file copy) | host-free unit |
| `_apply_patch(patch_ref, workspace)` | git only | unit (skip if no `git`) |
| `_sync_tree(kernel_src, workspace)` | mkdir + rsync subprocess | `live_vm` (only the rsync exec) |
| `_real_checkout(...)` (composition) | delegates to the above | **host-free unit** (see below) |

Only the actual `rsync` subprocess invocation carries `# pragma: no cover - live_vm`.
`_real_checkout`'s *orchestration* — calling sync → stage-config → patch in that order with
the right paths — is **not** gated, and its wiring assertion does **not** depend on `git`
(so it can never silently skip on a git-less machine, which is exactly where a wiring
regression would otherwise hide). The wiring test monkeypatches **all three** step helpers
(`_sync_tree`, `_stage_config`, `_apply_patch`) with recorders and asserts (a) the call
order is sync→stage→patch and (b) each receives the right arguments (the patched workspace,
the profile's `config_ref`/`patch_ref`). The *real* patch behavior — a clean diff applies, a
bad diff raises `CONFIGURATION_ERROR` — lives in the separate, `git`-gated `_apply_patch`
tests, and `_stage_config`'s real byte-copy lives in its own host-free test. This split keeps
the anti-regression ordering guarantee unconditional while leaving the toolchain-touching
behavior to the tests that genuinely need it. The injected `checkout` seam on
`LocalLibvirtBuild` is unchanged, so the existing fake-seam tests of `build()` still drive
that orchestration host-free too.

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

- `_resolve_local_ref`: `file:///abs/path` → that path; a bare absolute path → that path;
  `https://…` and `git+…` → `CONFIGURATION_ERROR`; `file://host/path` (non-empty netloc) →
  `CONFIGURATION_ERROR`; a bare relative path → `CONFIGURATION_ERROR`; an absolute path to a
  nonexistent file → `CONFIGURATION_ERROR`.
- `_stage_config`: copies bytes to `workspace/.config`, overwriting an existing one; a
  missing `config_ref` file → `CONFIGURATION_ERROR`.
- `_apply_patch` (skip if `shutil.which("git") is None`): a clean unified diff applies and
  the target file content changes; a conflicting patch → `CONFIGURATION_ERROR` and the
  target is reported in a redacted detail, not the raw patch.
- `_real_checkout` wiring (host-free, **never skipped** — all three step helpers
  monkeypatched to recorders, no `git`/`rsync`): asserts the sync→stage→patch call order and
  that each helper receives the right arguments (the patched workspace, `config_ref`,
  `patch_ref`). The real per-step behavior is covered by the helper tests above, so this test
  guards only the composition.

`git` presence: only the `_apply_patch` tests (real `git apply` on a real diff) skip when
`git` is absent, matching the existing suite's convention (e.g.
`test_source_revision_for_git_tree`). CI installs git, so they run and gate there; the skip
only relieves a git-less developer machine. The `_real_checkout` wiring test and the
`_stage_config`/`_resolve_local_ref` tests are git-free and always run.

`live_vm` end-to-end: `test_live_vm_real_make_build_id_matches_readelf` is **currently a
`NotImplementedError` stub** (`tests/.../test_build.py:256-267`). This change fills its body
so that, under the `live_vm` gate with `KDIVE_KERNEL_SRC` + `readelf` present, it drives the
real `build()` (rsync sync → config preflight → `make` → build-id extraction) and asserts
the extracted build-id equals `readelf -n vmlinux`. It keeps the existing skip when the env
or `readelf` is absent. This is the executable form of acceptance criterion 1; it runs only
on the demo host, not in CI.

## 9. Acceptance (from the issue)

- A server-build Run against `~/src/linux` produces a real `bzImage` + `vmlinux` with a
  build-id via the existing `build()` orchestration. → the `live_vm` test
  `test_live_vm_real_make_build_id_matches_readelf`, whose stub body this change replaces
  with the real end-to-end assertion (gated; runs on the demo host, §8).
- A `patch_ref` is applied; a bad patch is a clean categorized failure. → host-free unit
  tests of `_apply_patch` and the `_real_checkout` composition (`CONFIGURATION_ERROR`).
- Honors ADR-0029 (build plane); keeps the injected-seam shape so unit tests stay
  host-free. → §5.
