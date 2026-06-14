# io_uring zcrx freelist out-of-bounds write

## Summary

- **Subsystem**: `io_uring`, zero-copy receive, networking
- **Public report**: [CVE-2026-43121](https://nvd.nist.gov/vuln/detail/CVE-2026-43121)
- **Fix reference**: [stable commit `003049b1c4fb`](https://git.kernel.org/stable/c/003049b1c4fb8aabb93febb7d1e49004f6ad653b)
- **Fixed release**: Post-7.0 stable
- **Primary symptom**: KASAN slab out-of-bounds write from duplicated zcrx freelist entry
- **VM suitability**: Hard. Requires zcrx-capable NIC support or a realistic software substitute.

## Bug description

The vulnerable path used a non-atomic `atomic_read()` plus `atomic_dec()` sequence in `io_zcrx_put_niov_uref()` while another path used `atomic_xchg()` in `io_zcrx_scrub()`. On SMP, the same `niov` could be pushed to a freelist twice. Once `free_count` exceeded `nr_iovs`, later operations wrote beyond the allocated freelist array.

## Suggested starting prompt

> A KASAN-enabled kernel reports an out-of-bounds write in io_uring zero-copy receive cleanup under heavy receive and teardown stress. Determine how a freelist count can exceed the number of registered IOVs.

## Reproduction sketch

1. Boot with `CONFIG_IO_URING=y`, zcrx support, `CONFIG_KASAN=y`, and SMP enabled.
2. Provision a supported NIC or virtual function if available.
3. Run a zero-copy receive workload that repeatedly registers and unregisters receive queues.
4. Race refill and scrub/teardown paths under traffic.
5. Capture KASAN reports and freelist counters.

## Expected signal on vulnerable kernel

- `free_count > nr_iovs`.
- Same `niov` appears in the freelist twice.
- KASAN reports a small write past a `kvmalloc`-allocated array.

## Fixed-kernel expectation

Reference accounting prevents double-push and keeps freelist count within bounds.

## A/B scoring hints

- Award credit if the agent looks for non-atomic read/modify/write races.
- Award extra credit if it proves double insertion by logging `niov` identities.
- Penalize if it assumes the overwrite is an allocator bug or NIC driver bug without checking zcrx ownership.

## Caveats

This is intentionally marked advanced. Include it only if the MCP environment can provision zcrx-capable networking.
