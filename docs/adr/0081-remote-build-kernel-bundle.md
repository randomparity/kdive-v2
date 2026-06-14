# ADR 0081 — Remote build publishes a single vmlinuz+modules install bundle (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  independent `remote_libvirt` package + the portability diff gate this stays inside),
  [ADR-0078](0078-object-store-in-target-install-seam.md) (the object-store + presigned-URL
  in-target install seam whose GET this bundle is the payload of),
  [ADR-0013](0013-object-store-layout-retention.md) (the run-keyed object layout the bundle is
  stored under), [ADR-0029](0029-build-plane-local-make.md) (the worker-`make` Build plane this realizes
  for remote), [ADR-0051](0051-install-method-conditional-crashkernel.md) /
  [ADR-0061](0061-boot-cmdline-composition.md) (the in-guest install/cmdline composition the
  bundle feeds).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../design/m2-remote-libvirt.md) §Decomposition issue 4.

## Context

`local_libvirt` builds a kernel on the worker and stores two artifacts — the raw `bzImage`
(`kernel_ref`) and the `vmlinux`/debuginfo (`debuginfo_ref`) — then **direct-kernel-boots** the
`bzImage` with a custom config and an initramfs, so the in-tree modules under
`/lib/modules/<ver>` are never needed at boot. The `Builder` port returns exactly those two
refs plus the build-id (`BuildOutput`), and the `runs` ledger has exactly two ref columns
(`kernel_ref`, `debuginfo_ref`) to persist them.

`remote_libvirt` (ADR-0078) does **not** direct-kernel-boot. It boots a persistent disk-image
base OS and iterates kernels by **in-guest install + reboot**: the target pulls the built
kernel over a presigned GET and installs it into the guest's `/boot` + grub. A kernel installed
this way on a general-purpose base image **requires its loadable modules** — `/lib/modules/<ver>`
must be present for the booted kernel to mount root, load virtio/storage drivers, and run kdump.
So remote build must produce **vmlinuz *and* its modules**, where local produced vmlinuz alone.

The constraint is that M2 may not grow this third artifact into a new shared contract:

- The `Builder` port (`BuildOutput`) is a **carried invariant** — M2 satisfies the same
  `ProviderRuntime` ports unchanged (ADR-0076 §Carried invariants 1). Adding a `modules_ref`
  field changes a port every provider shares.
- The `runs` ledger and the migration set are **frozen** — M2's only DDL is migration `0020`
  (the `resources.kind` CHECK widen); the spec states "No new columns or tables." A
  `runs.modules_ref` column would be core DDL beyond `0020` and **trip the portability gate**
  (the gate's core surface is `domain/db/jobs/reconciler/services/store/security/mcp`).

So the modules cannot be carried as a *third ref*. They must travel **inside an existing ref**.

## Decision

`RemoteLibvirtBuild` publishes the kernel as a **single install bundle** referenced by
`kernel_ref`, and the `vmlinux` debuginfo as `debuginfo_ref`, leaving `BuildOutput`, the
`Builder` port, and the `runs` ledger **unchanged**:

1. Run the worker build exactly as local does — warm-tree checkout (rsync + staged `.config` +
   optional patch), `make olddefconfig`, the kdump/debuginfo `.config` preflight, `make`. This
   plumbing is **duplicated** into `remote_libvirt/build.py`, not shared with `local_libvirt`
   (ADR-0076: no shared layer with the doomed provider).
2. Run `make modules_install INSTALL_MOD_PATH=<staging>` to materialize `/lib/modules/<ver>`
   under a private per-build staging root, then **drop the `build`/`source` symlinks** it plants
   there (they are absolute paths into the worker's build tree and would extract in-guest as
   dangling links), so the bundle carries only real module files.
3. **Package one bundle**: a **gzip-compressed tar** (`.tar.gz`) containing `boot/vmlinuz` (the
   built image) and `lib/modules/<ver>/…` (the installed modules). This bundle is stored under
   the run-keyed `kernel` object (`{tenant}/runs/{run_id}/kernel`, ADR-0013) and its key is
   returned as `kernel_ref`. For `remote_libvirt`, `kernel_ref` therefore names a
   **gzip-compressed vmlinuz+modules install bundle**, not a raw image — a provider-private
   payload format the paired remote Installer (issue 5) pulls and **decompresses** in-guest
   (`tar xzf`). Compression is part of the build→install contract: the bundle is always
   `.tar.gz`, so issue 5 always gunzips.
4. Extract the GNU build-id from the produced `vmlinux` (the unit-tested note parser, not a
   locale-fragile `readelf` scrape) and store `vmlinux` as `debuginfo_ref`. `build_id` is
   returned in `BuildOutput` and recorded in the ledger for later vmcore↔debuginfo matching.

The build publishes via the **object store** (`put_artifact`) — the canonical artifact channel
ADR-0078 fixes. Minting the presigned GET and running the in-guest pull are the **Install**
plane's job (issue 5), not the build's; build only writes the object.

## Consequences

- **Zero core/port change.** `BuildOutput`, the `Builder` port, the `runs` ledger, and the
  migration set are untouched; the remote build slots into the existing worker build/complete
  path (`jobs/handlers/runs.py`, `mcp/tools/lifecycle/runs/build.py`) transparently, because
  those persist `kernel_ref`/`debuginfo_ref`/`build_id` generically. The portability gate stays
  green (only `providers/` + composition + docs are touched).
- **One object, one presigned GET at install.** Because vmlinuz and modules share one bundle
  object, the Installer mints a **single** presigned GET — matching the ADR-0078 "scoped to one
  object" capability shape — instead of two bearer URLs to register and redact.
- **The bundle is gzip-compressed, and its whole-object size is a build-time concern.** A
  `make modules_install` tree is far larger than local's raw `bzImage` — commonly hundreds of
  MB uncompressed for a kdump/debug `.config`, against a ~10 MB `bzImage`. `store/objectstore.py`
  records a 5 GiB single-object PUT ceiling, and `put_artifact(data: bytes)` holds the entire
  object in worker memory for the PUT. Gzip is therefore not a nicety: module `.ko` files
  compress heavily, which keeps the stored object, the worker's PUT memory, and the guest's
  presigned-GET download all an order of magnitude smaller, and keeps a fat debug build well
  under the single-PUT ceiling. The build path reads/compresses the bundle into one in-memory
  buffer (the same whole-object model local already uses for its `bzImage`/`vmlinux` puts), so
  per-concurrent-build worker RSS scales with the **compressed** bundle size — a known, bounded
  cost, not a streaming PUT (streaming would be a `store/` core change beyond the M2 allowlist).
- **`kernel_ref` is provider-format-private.** Its bytes mean "raw `bzImage`" for local and
  "vmlinuz+modules tar" for remote. This is coherent because the Builder and Installer are
  paired per provider: each provider's Installer consumes only its own provider's boot-artifact
  format. The format is documented on `RemoteLibvirtBuild` and consumed only by the remote
  Installer (issue 5).
- **Build mechanics are duplicated, not shared.** `remote_libvirt/build.py` reimplements the
  checkout/make/build-id seams (reusing only the already-neutral `provider_components` /
  `providers.build_validation` helpers). This is the deliberate cost of ADR-0076 independence —
  `local_libvirt` stays deletable in isolation. The rule-of-three is not yet met (this is the
  second real `make` builder), so extraction to a shared module is not warranted now.
- **New obligation on issue 5 (Install).** The remote Installer must pull the `.tar.gz` bundle
  in-guest and **decompress + extract** it (`boot/vmlinuz` → `/boot`, `lib/modules/<ver>` →
  `/lib/modules`) before the grub entry + crashkernel-cmdline composition and reboot. This ADR
  fixes the build→install payload contract — a single gzip-compressed object — that issue 5
  consumes.

## Alternatives considered

- **Add a `modules_ref` field to `BuildOutput` + a `runs.modules_ref` column.** The most
  explicit shape — modules are a first-class third artifact. Rejected: it changes a port every
  provider shares (against ADR-0076 carried-invariant 1) and needs core DDL beyond migration
  `0020` (against the M2 "no new columns" schema delta), which trips the portability gate. The
  modules are a remote-only concern and must not enter the shared contract.
- **Publish modules as a separate artifact under a derivable key** (`{tenant}/runs/{run_id}/modules`),
  reconstructed by the Installer from `run_id` without a ledger field. Avoids the port/DDL
  change. Rejected: it creates an implicit "derive-the-key" contract spanning the build and
  install issues (a silent coupling no type enforces), and it forces the Installer to mint
  **two** presigned GETs — two bearer capabilities to register and redact — against ADR-0078's
  one-object capability scope. The bundle carries the same bytes with one object and one
  capability.
- **Extract a shared `kernel_build` module** that both `local_libvirt` and `remote_libvirt` use,
  to avoid duplicating the checkout/make seams. Rejected: ADR-0076 deliberately keeps the
  providers decoupled so `local_libvirt` is deletable as a standalone follow-up without touching
  remote; a shared build module reintroduces the cross-provider dependency that decision avoids,
  and the rule-of-three for extraction is not met (one synthetic + two real builders, where the
  synthetic fault-inject builder runs no `make`).
- **Keep two separate objects but bundle only at the seam.** Store vmlinuz and modules
  separately, and have the worker tar them at install time. Rejected: it still needs the
  derivable-key coupling above, and moves packaging work onto the latency-sensitive install path
  for no benefit over packaging once at build time.
