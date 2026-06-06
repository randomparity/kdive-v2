# Bootable kdive-ready rootfs builder (port v1 `build-rootfs.sh`) — design

- **Date:** 2026-06-06
- **Issue:** [#124](https://github.com/randomparity/kdive/issues/124) (gap **G3** of epic
  [#123](https://github.com/randomparity/kdive/issues/123) — the one hard blocker).
- **ADR:** [`../../adr/0052-bootable-rootfs-image-builder.md`](../../adr/0052-bootable-rootfs-image-builder.md)
- **Status:** Accepted

## 1. Problem

`scripts/live-vm/build-guest-image.sh` is a placeholder. It `qemu-img create`s an **empty**
qcow2 and pins a base image by an all-zeros digest
(`docker://quay.io/kdive/fedora-kdump@sha256:0000…`). The rootfs catalog
(`src/kdive/rootfs/catalog_data.json`) points only at a raw Fedora Cloud image with **no
kdive-ready service and no SSH key** — it boots to a bare `login:` marker. There is therefore
no image the vulnerable Linux 7.0 kernel can boot into to reach a userspace path lookup and
trigger the `__d_lookup()` OOB read that drives the demo
(`docs/test-cases/05-dcache-dhash-entries-oob-read.md`).

The proof-of-concept at `~/src/kdive-v1` has a **working** unprivileged builder
(`scripts/build-rootfs.sh`, ~12 KB). This spec ports it into the rewrite's `scripts/live-vm/`
layout and brings across the one Python dependency it needs (the managed SSH-key helper), which
the rewrite does not yet have.

## 2. Goals / non-goals

**Goals**

- An **unprivileged** run on this host emits a qcow2 that boots under `qemu:///system` to the
  `kdive-ready` console marker on `ttyS0`.
- `build-guest-image.sh` is **replaced** with the ported builder (no dead code, no stub left).
- The builder is **idempotent** on a pre-existing destination (exit 0, no qemu invocation) —
  the contract `tests/scripts/test_live_vm_fixtures.py` and the gated integration preflight rely
  on.
- Host-side image labeling/readability is documented (the separate qemu user reads the image
  under `qemu:///system`).
- The full Fedora ext4 rootfs is reusable across current and future test cases (live drgn
  introspection, vmcore capture, race cases) — chosen over a minimal initramfs.

**Non-goals (deferred to sibling gaps of #123)**

- The boot/readiness consumption seams (G4 — `install.py:_real_readiness` / `_real_kdump_check`).
- Build checkout (G1), install fetch (G2), and the end-to-end driver (G5).
- Any catalog-schema change. Today the produced image is referenced by `rootfs: {kind: "path"}`
  (or `kind: "upload"` for the temporary/special case); a catalog of *pre-built* images is a
  recorded direction, not work in this issue (§7).
- A live SSH transport. The managed keypair is produced so the image is reusable, but nothing in
  this issue connects over SSH (the crash shows on the console pre-readiness).

## 3. Components

### 3.1 `scripts/live-vm/build-guest-image.sh` (overwrite in place)

Two unprivileged libguestfs stages, ported verbatim from v1 with the rewrite-specific changes in
§4:

1. **Stage 1 — customize.** `virt-builder fedora-${RELEASEVER}` (default 43) into a scratch
   qcow2: `--install openssh-server`, enable `sshd`, create the SSH user when it is not `root`,
   `--ssh-inject` the resolved authorized key, upload a `kdive-ready.service` oneshot
   (`ExecStart=/bin/sh -c 'echo kdive-ready > /dev/ttyS0'`, `WantedBy=multi-user.target`) and
   enable it. Optional debug tooling (`KDIVE_ROOTFS_DEBUG=1` → `drgn,kexec-tools,makedumpfile`)
   and optional paired-`vmlinux` staging at `/usr/lib/debug/lib/modules/<release>/vmlinux`.
2. **Stage 2 — repack to whole-disk ext4.** `virt-tar-out` the root tree, then
   `virt-make-fs --type=ext4 --format=qcow2` into a **no-partition-table whole-disk ext4 qcow2** —
   the only layout the boot provider mounts (`root=/dev/vda`, no initramfs). `guestfish` then
   normalizes `/etc/fstab` to a lone `/dev/vda / ext4 defaults 0 1`, removes `/etc/crypttab`, and
   disables guest-internal SELinux. Final `chmod 0644` so the separate qemu user can read it under
   `qemu:///system`.

**Inputs (environment).** `KDIVE_ROOTFS` (output path; default
`/var/lib/kdive/rootfs/minimal.qcow2`), `KDIVE_ROOTFS_RELEASEVER` (43), `KDIVE_ROOTFS_SIZE` (6G),
`KDIVE_ROOTFS_SSH_USER` (root), `KDIVE_ROOTFS_AUTHORIZED_KEY` (a `.pub` file; overrides the
managed helper), `KDIVE_ROOTFS_DEBUG` (0), `KDIVE_ROOTFS_VMLINUX` + `KDIVE_ROOTFS_KERNEL_RELEASE`
(optional debug-symbol staging). The first positional argument overrides `KDIVE_ROOTFS`.

### 3.2 `src/kdive/prereqs/` (new package) + `managed_ssh_key.py`

Ported verbatim from v1. kdive owns a durable ed25519 keypair (never `~/.ssh`): the public half is
baked into the rootfs at build, the private half is the future connect-time `ssh -i` identity. The
module is the single source of truth for both the key **path** and its **generation**, so the
builder (which shells out to `python3 -m kdive.prereqs.managed_ssh_key`) and any future Python
connect path never disagree.

- **Path resolution (pure, no I/O):** `KDIVE_SSH_KEY_DIR` (must be absolute) wins; else
  `$XDG_DATA_HOME/kdive/ssh` when `XDG_DATA_HOME` is set, non-empty, and absolute; else
  `~/.local/share/kdive/ssh`. Refuses a path containing a control character (it is pasted into a
  `virt-builder` argument).
- **Generation:** `0700` dir, `flock`-guarded, idempotent — generates the pair if the private key
  is absent, re-derives only the public half if it is missing, otherwise re-asserts `0600` on the
  private key. Never overwrites an existing private key. Strict modes (`0600` private, `0644`
  public) regardless of umask. Refuses a symlinked or group/other-accessible key dir.
- **CLI:** `python -m kdive.prereqs.managed_ssh_key [--ensure-public-key]` prints the module's own
  computed path (never `ssh-keygen` output); errors to stderr, non-zero exit. On a missing
  `ssh-keygen` it names `KDIVE_ROOTFS_AUTHORIZED_KEY` as the operator escape hatch.

It is intentionally **stdlib-only** and avoids 3.11+ runtime-only constructs: the builder invokes
it through the host `python3`, which may predate the project's venv.

### 3.3 Docs

- A host-side image-labeling note: the per-build write and final `chmod 0644` are unprivileged;
  the output directory is pre-prepared once by an OS admin; `0644` (with the host's
  `virt_image_t`/0644 file labeling) lets the separate qemu user read the image under
  `qemu:///system`. This guest-internal SELinux-disable is independent of the host file label.
- ADR-0052 records the whole-disk-ext4 rootfs-layout decision and the pre-built-image-catalog
  direction.

## 4. Rewrite-specific changes from the v1 port

1. **Idempotency guard (new).** v1's `build-rootfs.sh` had none. The rewrite's stub *and* the
   fixtures test require: a pre-existing destination → log a message containing `idempotent` and
   exit 0 before requiring any tool. The guard runs first (after arg parsing), so a second run on
   an existing image is a no-op even with an empty `PATH`.
   The guard is **presence-only**: it keys solely on path existence and consults no build input,
   so it neither validates the qcow2 nor rebuilds when inputs change. Two consequences follow, and
   both share one recovery — **delete the destination and re-run**: (a) a build killed mid-Stage-2
   can leave a truncated or zero-byte file, which is never auto-repaired; (b) re-running with a
   changed input (`KDIVE_ROOTFS_DEBUG`, `KDIVE_ROOTFS_VMLINUX`, `KDIVE_ROOTFS_SSH_USER`,
   `KDIVE_ROOTFS_SIZE`, or after the managed keypair is rotated so the baked-in public key is
   stale) keeps the **old** image and exits 0 — a *valid* but stale image that can silently pass
   §6.1 acceptance. The idempotency log line therefore states explicitly that no rebuild occurred
   and inputs were not consulted, so the no-op is unambiguous in logs. The `[[ -f "$dest" ]]` test
   follows symlinks, so a symlinked
   destination pointing at a regular file short-circuits to the `idempotent` exit before the
   Stage-0 symlink refusal (change 2) is reached; this is safe because the guard performs no write,
   but it means the symlink refusal only governs the *build* path, not the no-op path.
2. **Early output-dir preflight (new, before Stage 1).** v1 only `mkdir -p`s the output parent in
   Stage 2 — *after* the minutes-long `virt-builder` run — so a missing or unwritable output
   directory (the §3.3 operational obligation) wastes the whole build before failing. The port adds
   a **Stage-0 preflight** that runs after the idempotency guard and before any libguestfs tool:
   refuse a symlinked destination, canonicalize the parent, and assert the output directory exists
   and is writable (creating it when its own parent is writable). It fails fast with an actionable
   message naming `KDIVE_ROOTFS` and the host-guide pre-prepare step. This subsumes v1's late
   `mkdir -p`; the post-build symlink re-assertion is retained.
3. **Managed-key path.** v1 invoked `kdive.prereqs.managed_ssh_key` from a `src` layout under the
   repo root. The rewrite's package is `src/kdive/prereqs/`; the builder sets
   `PYTHONPATH="${REPO_ROOT}/src"` and invokes `python3 -m kdive.prereqs.managed_ssh_key
   --ensure-public-key`. `KDIVE_ROOTFS_AUTHORIZED_KEY` still short-circuits the helper.
4. **Name retained.** The file stays `build-guest-image.sh` (overwrite in place) so the fixtures
   test, the gated integration preflight, and the runbooks need no rename. The header comment is
   rewritten to describe the real two-stage rootfs build (the old "kdump scaffold" comment is
   removed — no stale prose).

Everything else is ported verbatim, including the security hardening: SSH-username regex
validation before it reaches a guest shell / the colon-delimited `--ssh-inject`; output-path
symlink refusal + canonicalized parent; `KDIVE_ROOTFS_KERNEL_RELEASE` regex validation (rejecting
`.`/`..`) before it is interpolated into a guest path; `mktemp` configs at `0600` and the
external-tool outputs `chmod 0600` after each write; the unquoted-heredoc `guestfish` upload.

## 5. Integration point (consumption, unchanged here)

The builder is the **producer**. A System references the produced file through the existing
discriminated rootfs source on `LibvirtProfile`:

- `rootfs: {kind: "path", path: "<KDIVE_ROOTFS>"}` — the operator-built local file (this issue);
- `rootfs: {kind: "upload"}` — a System-owned uploaded qcow2 for the temporary/special case
  (already implemented, ADR-0048 §5).

The gated integration tests point `$KDIVE_GUEST_IMAGE` at the builder's output. The catalog
(`kind: "catalog"`, `https://`-only + sha256) is for *fetchable* known-good images and is **not**
the path for a host-built artifact today.

## 6. Testing

- **Port the managed_ssh_key unit suite** (`tests/prereqs/test_managed_ssh_key.py`): path
  resolution is pure and runs everywhere; generation/idempotency/repair/mode tests are gated on
  `ssh-keygen` being installed (`pytest.mark.skipif`); the CLI tests pin a hermetic `HOME` and
  clear the dir overrides.
- **Extend `tests/scripts/test_live_vm_fixtures.py`** so the ported `build-guest-image.sh` still
  passes `bash -n`, declares `set -euo pipefail`, and is idempotent on a pre-existing image
  (exit 0 with `idempotent` in stderr, `PATH=""`). These never run the real `virt-builder` build
  (no network, no qemu), so they stay in the non-gated suite.
- **Gating is unchanged.** The real build is exercised only by the gated `live_vm` tier; nothing
  in this issue un-gates or widens an existing gate.

### 6.1 Manual boot-to-marker acceptance (runnable check for acceptance #1)

Acceptance #1 is a host-and-network-dependent boot that does not run in CI; it is verified once,
by hand, on this host. The check uses a **good** kernel and a **clean** cmdline — *not* the demo's
`dhash_entries=1` cmdline, which crashes in `__d_lookup()` before userspace and so never reaches
the marker (the demo's failure signal is the separate G4 console-crash path, not this readiness
marker). Procedure:

1. Build: `KDIVE_ROOTFS=/var/lib/kdive/rootfs/minimal.qcow2 scripts/live-vm/build-guest-image.sh`.
2. Direct-kernel boot under `qemu:///system` with a known-good Linux 7.0 `bzImage` (built from the
   tree `scripts/live-vm/fetch-kernel-tree.sh` checks out, via `make` — gap G1), no initrd,
   `root=/dev/vda console=ttyS0`, the console teed to a file (the same layout ADR-0030 boots).
3. **Pass signal:** the literal line `kdive-ready` appears on the `ttyS0` console log within the
   boot-timeout window. **Fail signals:** a `login:` prompt with no preceding `kdive-ready`
   (unit did not fire), or a `local-fs.target` stall (a layout/fstab regression).

This procedure is the falsifiable definition of "boots to the `kdive-ready` marker"; record its
result in the PR.

## 7. Pre-built-image-catalog direction (recorded, not built)

The long-term target is a **catalog of pre-built images**, with local `path`/`upload` as the
special-case escape hatch. The builder is decoupled from *how* its output is referenced, so the
direction stays open without a schema change now:

- The builder emits a **content-addressable** artifact — it prints `qemu-img info` and the
  produced image's `sha256:` so a future publish/upload flow can register the *same bytes*.
- A future catalog of pre-built images loosens the catalog schema's current `https://`-only `url`
  constraint (e.g. an object-store / content-addressed ref) and adds a publish step. That is
  explicitly out of scope here; ADR-0052 names it as the deferred follow-on so this issue does not
  paint it into a corner.

## 8. Acceptance (maps to #124)

1. Builder runs unprivileged on this host and emits a qcow2 that boots under `qemu:///system` to
   the `kdive-ready` console marker — verified by the §6.1 manual procedure. **Host prerequisites
   for this real build:** the libguestfs suite (`virt-builder`, `virt-tar-out`, `virt-make-fs`,
   `guestfish`) + `qemu-img`, and **network reachability** (or a pre-seeded `virt-builder`
   template cache) so `virt-builder fedora-43` can fetch the Fedora template on first use. The
   builder fails fast with an actionable message when a tool or the `fedora-43` template index
   entry is absent.
2. `build-guest-image.sh` is replaced (not a stub); the produced image is referenceable via
   `rootfs: {kind: "path"}` / `$KDIVE_GUEST_IMAGE`.
3. Host-side image labeling/readability is documented.
4. Guardrails green: `just lint type test`, `lint-shell` (shellcheck), and the fixtures + key
   unit tests.
