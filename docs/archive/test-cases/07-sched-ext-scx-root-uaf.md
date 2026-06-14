# sched_ext `scx_root` use-after-free during scheduler toggle

## Summary

- **Subsystem**: `sched_ext`, cgroups, BPF scheduler
- **Public reference**: [ChangeLog-7.0.7](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.7)
- **Fix reference**: Upstream `6fae274ce0e3...`, stable backport in 7.0.7
- **Fixed release**: Linux 7.0.7
- **Primary symptom**: Use-after-free or race while changing cgroup scheduler settings during scheduler disable/enable
- **VM suitability**: Medium. Needs `CONFIG_SCHED_CLASS_EXT` and concurrent stress.

## Bug description

Several cgroup-facing sched_ext operations cached the global `scx_root` pointer before taking `scx_cgroup_ops_rwsem`. A concurrent scheduler disable/enable cycle could free the old root and install a new one while the cached pointer still referenced freed memory. The fix moved the pointer load inside the rwsem-protected critical section.

## Suggested starting prompt

> A KASAN or KCSAN kernel reports a race/UAF in sched_ext while one process repeatedly enables and disables a BPF scheduler and another changes cgroup weights. Determine what global pointer is used outside its lifetime.

## Reproduction sketch

1. Boot with `CONFIG_SCHED_CLASS_EXT=y`, BPF support, cgroup v2, and KASAN/KCSAN.
2. Load a simple sched_ext scheduler such as `scx_simple`.
3. In one loop, enable and disable the scheduler.
4. In another loop, write new values to cgroup CPU controls such as `cgroup.weight`.
5. Capture sanitizer reports and lockdep traces.

## Expected signal on vulnerable kernel

- `scx_root` observed before lock acquisition.
- UAF or data race during `scx_group_set_weight()`, `scx_group_set_idle()`, or `scx_group_set_bandwidth()`.
- Failures correlate with scheduler disable/enable timing.

## Fixed-kernel expectation

The cgroup operation reads `scx_root` only while protected by `scx_cgroup_ops_rwsem`.

## A/B scoring hints

- Award credit if the agent identifies stale global-pointer caching.
- Award extra credit for checking the order of pointer load versus rwsem acquisition.
- Penalize if it only blames BPF program behavior without checking sched_ext core synchronization.

## Caveats

Newer kernel feature; make sure the VM image enables sched_ext before including this in a standard benchmark run.
