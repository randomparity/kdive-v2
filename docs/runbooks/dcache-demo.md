# Runbook: the dcache `dhash_entries=1` build → boot → verify demo

Operator guide for reproducing [`docs/test-cases/05-dcache-dhash-entries-oob-read.md`](../test-cases/05-dcache-dhash-entries-oob-read.md)
on the local libvirt/QEMU host: build `~/src/linux` at v7.0, boot it with `dhash_entries=1`
and observe the out-of-bounds read in `__d_lookup()` on the console, apply the 7.0.1 dcache
fix, rebuild, reboot with the same command line, and confirm the kernel now boots to
`kdive-ready`. This is gap **G5** of epic #123; see [ADR-0056](../adr/0056-live-demo-cmdline-wiring-dcache-driver.md)
and the [design spec](../superpowers/specs/2026-06-06-dcache-demo-driver-design.md). It builds
on the seams G1–G4 landed and on the [live-stack runbook](live-stack.md) rather than a separate
harness.

The demo is **console-only**: the crash is a pre-userspace fault read off the serial console,
so no SSH readiness and **no libvirt network** are required.

## 1. Prerequisites

- The #123 host: KVM / nested-virt with `libvirt`, the kernel build toolchain, QEMU.
- `~/src/linux` checked out at the vulnerable **v7.0** base.
- The bootable `kdive-ready` rootfs built by `scripts/live-vm/build-guest-image.sh` (G3).
- The repo set up: `just setup` (or `uv sync --locked`).

## 2. Worker-writable host staging

The build and install planes write under `/var/lib/kdive`. Make the four subdirectories
worker-writable, either by having an OS admin prepare them once:

```bash
sudo install -d -o "$USER" -g "$USER" \
  /var/lib/kdive/rootfs /var/lib/kdive/install /var/lib/kdive/build /var/lib/kdive/console
```

…**or** by pointing the override env vars at writable paths you own:

| var | default | plane |
|-----|---------|-------|
| `KDIVE_BUILD_WORKSPACE` | `/var/lib/kdive/build` | build (warm-tree `make`) |
| `KDIVE_INSTALL_STAGING` | `/var/lib/kdive/install` | install (per-Run kernel/initrd) |

The rootfs and console directories are owned by the provisioning plane; if you cannot make
`/var/lib/kdive/{rootfs,console}` writable, prepare them the same way as above. No libvirt
network is needed — the dcache case reads the console only.

## 3. Build inputs (operator env)

The demo resolves its build inputs and rootfs from four env vars (the G1/G3 seams). Nothing
kernel-sized is committed to the repo; you generate each:

```bash
export KDIVE_KERNEL_SRC=~/src/linux

# A .config that satisfies the build preflight (ADR-0029): CONFIG_CRASH_DUMP plus one of
# CONFIG_DEBUG_INFO_DWARF5 / _DWARF4 / _BTF. From a defconfig:
( cd "$KDIVE_KERNEL_SRC" && make defconfig \
  && ./scripts/config --enable CRASH_DUMP --enable DEBUG_INFO_DWARF5 \
  && make olddefconfig )
export KDIVE_TEST_BUILD_CONFIG="$KDIVE_KERNEL_SRC/.config"

export KDIVE_GUEST_IMAGE=/var/lib/kdive/rootfs/minimal.qcow2   # build-guest-image.sh output

# The 7.0.1 dcache fix as a -p1 patch. Verify it applies against the v7.0 tree BEFORE the run:
export KDIVE_DEMO_FIX_PATCH=~/dcache-dhash-entries-fix.patch
git -C "$KDIVE_KERNEL_SRC" apply --check "$KDIVE_DEMO_FIX_PATCH"
```

A `.config` missing a prerequisite fails Run A at **build** with `build_failure`; a patch that
does not apply fails Run B at **build** with `configuration_error` (G1's `git apply -p1`) — not
at boot.

## 4. Run the demo

```bash
just test-live      # or: uv run python -m pytest tests/integration/test_dcache_demo.py -m live_vm -q
```

The driver provisions one System and runs the vulnerable → fixed A/B against it; it skips
cleanly with the exact missing-env reason if any of the four vars above is unset.

## 5. Agent-facing walkthrough (the same path, tool by tool)

An agent reproduces the demo with the `runs.*` surface. One System hosts both Runs in
sequence:

1. `allocations.request(project, vcpus=2, memory_gb=2)` → `allocation_id`.
2. `systems.provision(allocation_id, profile=<console-only provisioning profile>)` → poll
   `systems.get` until `ready`.
3. `investigations.open(project, title="dcache demo")` → `investigation_id`.
4. **Run A (vulnerable):** `runs.create(investigation_id, system_id, build_profile=<no patch_ref>)`
   → `runs.build(run_id, cmdline="console=ttyS0 dhash_entries=1")` → `jobs.wait` →
   `runs.install(run_id)` → `jobs.wait` → `runs.boot(run_id)`. The boot fails and the System's
   console artifact shows the `__d_lookup` crash.
5. **Run B (fixed):** `runs.create(investigation_id, system_id, build_profile=<with patch_ref>)`
   on the **same** System → `runs.build(run_id, cmdline="console=ttyS0 dhash_entries=1")` →
   install → boot. The boot succeeds and the console reaches `kdive-ready`.

The `cmdline=` argument on `runs.build` is **load-bearing**: omit it and the kernel boots the
method default (`console=ttyS0`, no trigger), the bug does not reproduce, and Run A boots clean
— a silent non-repro. The worker logs the resolved cmdline at install; the vulnerable run's
console must show `dhash_entries=1` on its `Command line:` line. Both Runs use the same
`.config` and the same cmdline, so Run B's clean boot is attributable to the patch alone.

> The vulnerable boot reads `crashed` only if the real `dhash_entries=1` output carries a
> recognized crash signature before the `kdive-ready` line (ADR-0055 §4). If a real capture
> hangs without one, add the signature — a one-line change — rather than altering the driver.

## 6. Cleanup

The driver tears the System down at the end of the run (`allocations.release` + teardown). If a
run is aborted before that, reap the leftover System by hand before re-running:

```bash
virsh destroy  "kdive-<system_id>" 2>/dev/null || true
virsh undefine "kdive-<system_id>" --nvram 2>/dev/null || true
rm -rf /var/lib/kdive/install/<system_id>
```
