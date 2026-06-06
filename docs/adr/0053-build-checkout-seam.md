# ADR 0053 — Build checkout seam: warm-tree rsync + local config/patch refs

- **Status:** Proposed
- **Date:** 2026-06-06
- **Deciders:** David Christensen
- **Depends on:** [ADR-0029](0029-build-plane-local-make.md) (the build plane, the
  `ServerBuildProfile`, the injected-seam shape, and the `configuration_error` vs
  `build_failure` split — §3 — this seam slots into),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the external-build lane that owns
  remote/object-store artifact resolution, which this lane deliberately does not).
- **Spec:** [`../superpowers/specs/2026-06-06-build-checkout-seam-design.md`](../superpowers/specs/2026-06-06-build-checkout-seam-design.md).
- **Closes:** [#125](https://github.com/randomparity/kdive/issues/125) (gap G1 of
  [#123](https://github.com/randomparity/kdive/issues/123)).

## Context

The local-libvirt build plane runs `make` in-tree against a per-Run `workspace`
(`make -C workspace`), but the one step that *populates* that workspace —
`build.py:_real_checkout` — is a stub that raises `MISSING_DEPENDENCY`. The PoC at
`~/src/kdive-v1` has a working checkout/config path; this ADR records the decisions that
govern porting it into the rewrite's seam shape, the ones the build-plane ADR (0029) left
open: where the source comes from, how it is materialized into the workspace, how the
profile's `config_ref`/`patch_ref` resolve, and what category a non-applying patch is.

## Decisions

1. **The source is the operator's warm `KDIVE_KERNEL_SRC` tree, copied per-Run — not a
   clone of `kernel_source_ref`.** The env-pinned tree (`~/src/linux`) is the warm,
   pre-built source the operator maintains; `from_env` already threads it into the seam.
   `profile.kernel_source_ref` is recorded provenance on the Run, not a fetch instruction
   for this lane — cloning per Run would discard the warm build state the whole plane is
   designed around. The seam copies the tree into the per-Run `workspace` and patches the
   *copy*, so a patch never mutates the shared tree and concurrent Runs never collide.

2. **Materialize with `rsync -a --delete <src>/ <workspace>/`.** `-a` preserves the
   source's build products so the in-tree `make` recompiles only what the patch touched
   (the warm-tree performance contract); `--delete` makes a re-sync into a
   partially-populated workspace exactly mirror the source, which is the idempotency the
   issue asks for. A copy failure (disk, permissions) is `INFRASTRUCTURE_FAILURE`; an
   absent `rsync` is `MISSING_DEPENDENCY`; an unset/invalid `KDIVE_KERNEL_SRC` is caught
   before rsync as `CONFIGURATION_ERROR`.

3. **`config_ref` and `patch_ref` resolve as *local* references only.** A `file://` URL or
   a bare absolute path resolving to an existing regular file. A non-local scheme
   (`http(s)://`, `git+…`, `s3://`) or a missing file is `CONFIGURATION_ERROR`. Remote /
   object-store resolution is the external-build lane's concern (ADR-0048); the warm-tree
   server-build lane is host-local by construction, so admitting remote schemes here would
   be unused surface and a second, untested fetch path. The staged config is copied to
   `workspace/.config`, overwriting any `.config` the warm tree carried, so the resolved
   config is deterministic per profile and is exactly what `_real_read_config` preflights.

4. **A patch that does not apply is `CONFIGURATION_ERROR`, not `BUILD_FAILURE`.** ADR-0029
   §3 already split the build taxonomy this way: an operator/agent-supplied input that does
   not fit the resolved tree (there, a `.config` missing a required option) is a
   configuration defect the operator fixes with the most specific, most actionable
   category, while `BUILD_FAILURE` is reserved for a `make`/toolchain failure. A `patch_ref`
   that fails `git apply` is the same shape of defect — the agent's "write a fix" patch
   (demo step 4) is precisely such an input — so it takes `CONFIGURATION_ERROR`. The patch
   is applied with `git apply -p1` (the natural consumer of a `git diff`); an absent `git`
   is `MISSING_DEPENDENCY`.

5. **Decompose into host-free helpers; only rsync/`make` stays `live_vm`-gated.**
   `_resolve_local_ref`, `_stage_config`, and `_apply_patch` are unit-tested directly
   (the first two host-free; `_apply_patch` needs only the `git` binary and skips if
   absent), while `_sync_tree` and the composed `_real_checkout` keep the
   `# pragma: no cover - live_vm` because they shell out to rsync. The injected `checkout`
   seam on `LocalLibvirtBuild` is unchanged, so the existing fake-seam tests of `build()`
   still cover the orchestration without a toolchain — the contract ADR-0029 §4 set.

## Consequences

- A server-build Run against `~/src/linux` produces a real `bzImage` + `vmlinux` through
  the existing `build()`; the live demo's build plane (G1) is unblocked.
- The build host now needs `rsync` and `git` for a real build; absent either, the build
  fails fast with `MISSING_DEPENDENCY` and an actionable message. CI is unaffected — the
  rsync/`make` path stays `live_vm`-gated; the patch/config/ref logic is covered host-free.
- A bad patch and a bad config-ref are both `CONFIGURATION_ERROR` (operator-fixable),
  cleanly distinct from a `make` `BUILD_FAILURE`; the Run carries the specific category.
- Remote/object-store `config_ref`/`patch_ref` resolution is explicitly deferred to the
  external-build lane; if the warm-tree lane ever needs a fetchable ref, that is a named
  follow-on, not a quiet widening of `_resolve_local_ref`.
- `profile.kernel_source_ref` is recorded but unverified against the warm tree in this lane;
  provenance verification (tree matches the declared ref) is a future follow-on.

## Considered & rejected

- **Clone `kernel_source_ref` per Run instead of copying the warm tree.** Rejected: it
  discards the operator's warm build state (every Run a cold full build of minutes), and
  the issue/epic pin the source to `KDIVE_KERNEL_SRC`. The ref stays as provenance.
- **Build out-of-tree against the shared source (`make O=<workspace>`), as v1 did.**
  Rejected: the rewrite must apply a per-Run patch, and patching the shared source in place
  is neither isolated nor idempotent across concurrent Runs. Copying the tree and building
  in-tree is what makes the patch safe; the warm `.o` files come along in the copy.
- **`shutil.copytree` instead of rsync.** Rejected: not incrementally idempotent (needs
  `dirs_exist_ok`, recopies everything, no `--delete` mirroring of a crashed partial
  workspace) — it fights the issue's explicit "incremental/idempotent sync" requirement.
  rsync is a standard host tool present on the build host and gated behind `live_vm`.
- **Make a non-applying patch `BUILD_FAILURE`.** Rejected: it conflates "your patch is
  wrong" with "the compiler failed," losing the more actionable category. ADR-0029 §3's
  precedent puts operator-supplied-input defects under `CONFIGURATION_ERROR` (Decision 4).
- **Admit `https://`/object-store `config_ref`/`patch_ref` now.** Rejected as speculative:
  the warm-tree lane is host-local and the demo needs only local files; remote ingestion is
  ADR-0048's lane. Recorded as a named follow-on instead of unused, untested surface.
- **Apply the patch with `patch -p1` rather than `git apply`.** Either works; `git apply` is
  chosen because the agent's fix is produced as a `git diff` and `git` is already a build-host
  prerequisite, so it adds no dependency `patch` would have saved.
