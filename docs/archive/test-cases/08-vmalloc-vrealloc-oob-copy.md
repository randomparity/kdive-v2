# vmalloc `vrealloc_node_align()` out-of-bounds copy

## Summary

- **Subsystem**: `mm`, vmalloc
- **Public reference**: [ChangeLog-7.0.4](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.4)
- **Fix reference**: Upstream `82d1f01292d3`, stable backport in 7.0.4
- **Fixed release**: Linux 7.0.4
- **Primary symptom**: KASAN slab or vmalloc out-of-bounds write during shrink reallocation
- **VM suitability**: Medium. Easier with NUMA topology or crafted alignment constraints.

## Bug description

`vrealloc_node_align()` could force a reallocation when the existing vmalloc allocation was on the wrong NUMA node or had the wrong alignment. During a shrink, it allocated the new smaller size but copied the old larger size, overwriting beyond the end of the new allocation.

## Suggested starting prompt

> A KASAN report shows an out-of-bounds write during a vmalloc reallocation shrink path. The new allocation is smaller than the old allocation. Determine why the copy length is wrong and under what allocation constraints the realloc path is forced.

## Reproduction sketch

1. Boot with `CONFIG_KASAN=y`.
2. Prefer a VM with emulated NUMA topology using QEMU `-numa` options.
3. Add a small test module or in-kernel test that allocates a vmalloc buffer, then calls the relevant realloc path with a smaller size and forced NUMA/alignment mismatch.
4. Capture the KASAN report and allocation metadata.

## Expected signal on vulnerable kernel

- Copy length equals old size while destination allocation equals new smaller size.
- KASAN reports an overwrite past the new buffer.
- The bug appears only on forced reallocation, not normal in-place shrink.

## Fixed-kernel expectation

The copy length is bounded by the smaller of old and new sizes.

## A/B scoring hints

- Award credit if the agent compares source size, destination size, and copy length.
- Award extra credit if it finds the forced-reallocation condition rather than assuming all shrinks are unsafe.
- Penalize if it only recommends increasing allocation size without fixing copy semantics.

## Caveats

This may require a small kernel module or test harness, so it is more of a kernel-development benchmark than a pure userspace repro.
