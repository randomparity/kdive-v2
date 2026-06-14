# ADR 0052 — Bootable kdive-ready rootfs builder: whole-disk-ext4 layout + managed SSH key

- **Status:** Proposed
- **Date:** 2026-06-06
- **Deciders:** David Christensen
- **Depends on:** [ADR-0030](0030-install-boot-plane.md) (the direct-kernel `<os>` /
  `root=/dev/vda`, no-initramfs boot the rootfs layout must satisfy),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the discriminated rootfs source —
  `path`/`upload`/`url`/`catalog` — the produced image is referenced through).
- **Spec:** [`../superpowers/specs/2026-06-06-rootfs-builder-port-design.md`](../archive/superpowers/specs/2026-06-06-rootfs-builder-port-design.md).
- **Closes:** [#124](https://github.com/randomparity/kdive/issues/124) (gap G3 of
  [#123](https://github.com/randomparity/kdive/issues/123)).

## Context

The live-seam demo needs a guest the vulnerable Linux 7.0 kernel can boot into to reach the
`__d_lookup()` OOB read. The current `scripts/live-vm/build-guest-image.sh` is a placeholder
(empty qcow2, all-zeros base digest) and the rootfs catalog points only at a bare Fedora Cloud
image with no kdive-ready unit and no key. The PoC at `~/src/kdive-v1` has a working unprivileged
builder; this ADR records the decisions that govern porting it into the rewrite, the ones the spec
leaves open: the rootfs **layout**, the **SSH-key source** (the rewrite lacks v1's managed-key
module), and how a host-built image is **referenced** versus the long-term catalog direction.

## Decisions

1. **Whole-disk ext4 qcow2, no partition table.** The image is a single ext4 filesystem written
   directly to the disk image (`virt-make-fs --type=ext4`), not a partitioned/GPT image. This is
   the only layout the direct-kernel boot provider mounts: `root=/dev/vda` with no initramfs
   (ADR-0030). `/etc/fstab` is normalized to a lone `/dev/vda / ext4` entry and `/etc/crypttab`
   removed, because the scratch image's GPT-layout mount entries would stall `local-fs.target` and
   the `kdive-ready` marker would never fire.

2. **Full Fedora ext4 rootfs, not a minimal initramfs.** A full userspace (sshd, systemd,
   optional drgn/kexec-tools) is reusable across the demo's current and future test cases — live
   drgn introspection, vmcore capture, race cases — at the cost of a larger image and a
   libguestfs build. A minimal initramfs would be smaller but would have to be re-grown per
   capability.

3. **Two unprivileged libguestfs stages; no host sudo/pkexec.** Stage 1 `virt-builder` customizes
   a scratch image; Stage 2 `virt-tar-out` + `virt-make-fs` repack to the whole-disk ext4 qcow2.
   The output directory is pre-prepared once by an OS admin; the per-build write and the final
   `chmod 0644` are unprivileged. `0644` (with the host's `virt_image_t` file label) lets the
   separate qemu user read the image under `qemu:///system`.

4. **Guest-internal SELinux is disabled in this debug rootfs.** So the host-written
   `authorized_keys` is read without a relabel and the first boot does not relabel+reboot (which
   would risk a false boot timeout). This is the *guest's* internal SELinux only and is
   independent of the *host-side* `virt_image_t`/0644 labeling of the image file, which still
   applies.

5. **The SSH key comes from a kdive-managed keypair, ported into `src/kdive/prereqs/`.** The
   rewrite had no key module; rather than auto-scraping `~/.ssh` or making the key optional, kdive
   owns a durable ed25519 keypair under `$XDG_DATA_HOME/kdive/ssh` (override `KDIVE_SSH_KEY_DIR`).
   The module is the single source of truth for the key path and its generation, so the builder
   and a future Python connect path cannot disagree. `KDIVE_ROOTFS_AUTHORIZED_KEY` overrides it
   with an operator-supplied `.pub`. The module is stdlib-only and 3.10-safe because the builder
   invokes it through the host `python3`, which may predate the project venv.

6. **`build-guest-image.sh` is overwritten in place, keeping its name.** The fixtures test, the
   gated integration preflight, and the runbooks reference the name; renaming would spread the
   change for no functional gain. The stub's "kdump scaffold" header is replaced wholesale (no
   stale prose). An **idempotency guard is added** that v1 lacked: a pre-existing destination logs
   `idempotent` and exits 0 before any tool is required — the contract the fixtures test and the
   preflight depend on.

7. **A host-built image is referenced by `path`/`upload`, not the catalog; a catalog of pre-built
   images is the recorded long-term direction.** The catalog (`kind: "catalog"`) is `https://`-only
   plus a sha256 — for *fetchable* known-good images, not a host-built file. The builder is
   decoupled from how its output is referenced and emits a content-addressable artifact (prints
   `qemu-img info` + the produced `sha256:`) so a future publish/upload flow can register the same
   bytes. Loosening the catalog `url` constraint to admit pre-built images is named as the
   follow-on, deliberately not done here.

## Consequences

- The vulnerable kernel can boot to a real userspace and the demo's `__d_lookup()` path becomes
  reachable; the image is reusable for later seams (drgn, vmcore) without a rebuild.
- The rewrite gains a `src/kdive/prereqs/` package (one module + tests) it did not have. A future
  SSH transport (ADR-0039) reuses the same managed-key source for its `ssh -i` identity.
- The builder requires the libguestfs suite (`virt-builder`, `virt-tar-out`, `virt-make-fs`,
  `guestfish`) and `qemu-img` on the build host; absent any of them it fails fast with an
  actionable message. The real build stays out of the non-gated test suite (no network/qemu).
- The output directory must be pre-prepared by an OS admin and the image left `0644` for the qemu
  user — an operational obligation documented in the host guide, not enforced by code.
- The pre-built-image catalog remains future work; until then operators wire the image via
  `path`/`$KDIVE_GUEST_IMAGE` or `upload`.

## Alternatives considered

- **Minimal initramfs instead of a full ext4 rootfs.** Smaller and faster to build, but it carries
  no reusable userspace — every later capability (sshd, drgn, vmcore tooling) would have to be
  re-added. Rejected for the demo's multi-test-case reuse goal (Decision 2).
- **Partitioned / GPT image.** The standard cloud-image layout, but the direct-kernel boot mounts
  `root=/dev/vda` with no initramfs and would stall on GPT `local-fs` mounts; the marker would
  never fire (Decision 1).
- **Auto-discover the operator's `~/.ssh/*.pub`, or make the key optional.** Discovery couples the
  guest's trust to whatever ambient key the build host happens to have and is non-deterministic
  across hosts; an optional key leaves sshd installed but unusable, defeating the reusability the
  key path exists for. A kdive-owned managed keypair is deterministic and is the same source a
  future connect path needs (Decision 5).
- **Register the built image in the catalog now (loosen the schema to allow local/file refs).**
  Rejected as scope creep: it changes a validated, security-relevant schema (the `https://` + sha256
  invariant) for a single host-built file that `path`/`upload` already cover. Recorded as the
  long-term direction instead (Decision 7).
- **Rename the script to `build-rootfs.sh` (the v1 name).** More accurate, but it spreads the
  change across the fixtures test, the integration preflight, and the runbooks for no functional
  benefit; overwrite-in-place is the smaller, safer change (Decision 6).
