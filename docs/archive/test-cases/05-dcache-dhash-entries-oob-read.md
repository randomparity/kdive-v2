# dcache `dhash_entries=1` out-of-bounds read

## Summary

- **Subsystem**: VFS, dcache
- **Public report**: [CVE-2026-43071](https://nvd.nist.gov/vuln/detail/CVE-2026-43071)
- **Additional tracker**: [Debian CVE-2026-43071](https://security-tracker.debian.org/tracker/CVE-2026-43071)
- **Fix reference**: Stable backport referenced in [ChangeLog-7.0.1](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.1)
- **Fixed release**: Linux 7.0.1
- **Primary symptom**: Page fault or OOB read in `__d_lookup()`
- **VM suitability**: Easy. Deterministic boot-parameter test.

## Bug description

Booting with `dhash_entries=1` caused `dcache_init()` to compute a degenerate hash configuration. The resulting `d_hash_shift` value caused a bad right shift in lookup arithmetic, and `__d_lookup()` could index far outside the dentry hash table.

## Suggested starting prompt

> A kernel booted with `dhash_entries=1` crashes during early filesystem lookup, with the instruction pointer in `__d_lookup()`. Determine why this boot parameter produces an out-of-bounds dcache lookup.

## Reproduction sketch

1. Boot vulnerable 7.0 kernel with kernel command-line argument `dhash_entries=1`.
2. Let init proceed or run any path lookup such as `ls /proc`.
3. Capture the page fault or KASAN report.
4. Compare against a boot without the parameter and against the fixed kernel.

## Expected signal on vulnerable kernel

- Crash in `__d_lookup()`.
- Dentry hash table has a single bucket.
- Hash shift/index math produces an invalid table offset.

## Fixed-kernel expectation

The kernel rejects, clamps, or safely handles pathological `dhash_entries` values and filesystem lookup proceeds normally.

## A/B scoring hints

- Award credit if the agent connects the boot parameter to dcache initialization.
- Award extra credit if it identifies bit-shift width or one-bucket hash-table arithmetic.
- Penalize if it focuses only on the specific lookup path rather than dcache table sizing.

## Caveats

This is very deterministic and therefore excellent as a smoke test, but it is less realistic than race cases.
