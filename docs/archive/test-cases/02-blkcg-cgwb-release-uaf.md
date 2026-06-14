# blkcg writeback cgroup release use-after-free

## Summary

- **Subsystem**: `mm`, block cgroup, cgroup writeback
- **Public report**: [CVE-2026-31586](https://security-tracker.debian.org/tracker/CVE-2026-31586)
- **Fix reference**: Upstream `51d8c78be0c2`, stable backport referenced in [ChangeLog-7.0.1](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.1)
- **Fixed release**: Linux 7.0.1
- **Primary symptom**: KASAN use-after-free in cgroup writeback release path
- **VM suitability**: Medium. Needs cgroup v2, writeback activity, and preferably fault injection.

## Bug description

`cgwb_release_workfn()` dropped a reference with `css_put(wb->blkcg_css)` and later used `wb->blkcg_css` again in `blkcg_unpin_online()`. If the reference drop freed the blkcg object, the later use dereferenced stale memory. The correct pattern is to capture any required parent pointer or state before dropping the final reference.

## Suggested starting prompt

> A KASAN-enabled VM reports a use-after-free during cgroup writeback teardown after a blkcg is destroyed under I/O load. Determine whether this is a refcounting bug, identify the stale pointer, and propose a minimal instrumentation plan.

## Reproduction sketch

1. Boot with `CONFIG_CGROUPS=y`, `CONFIG_CGROUP_WRITEBACK=y`, `CONFIG_BLK_CGROUP=y`, `CONFIG_KASAN=y`.
2. Mount cgroup v2.
3. Create and destroy blkcg-associated cgroups while generating buffered writeback to a virtio-blk disk.
4. Use fault injection or delay injection around writeback release to widen the race.
5. Capture KASAN reports and workqueue stack traces.

## Expected signal on vulnerable kernel

- KASAN UAF in `cgwb_release_workfn()` or adjacent blkcg/cgroup release path.
- Object lifetime shows a reference drop preceding later field access.
- Workqueue timing influences reproducibility.

## Fixed-kernel expectation

The release path no longer dereferences `wb->blkcg_css` after the reference-dropping operation.

## A/B scoring hints

- Award credit if the agent reconstructs object lifetime rather than treating the crash as generic memory corruption.
- Award extra credit for identifying `css_put()` as the lifetime boundary.
- Penalize if it recommends broad locking without explaining the stale reference.

## Caveats

This is a race and may require KASAN plus injected delays for reliable A/B scoring.
