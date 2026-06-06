# Hugetlb userfaultfd fault-mutex hash mismatch

## Summary

- **Subsystem**: `mm`, `userfaultfd`, `hugetlb`
- **Public report**: [CVE-2026-31575](https://nvd.nist.gov/vuln/detail/CVE-2026-31575)
- **Fix reference**: [stable commit `0217c7fb4de4`](https://git.kernel.org/stable/c/0217c7fb4de4a40cee667eb21901f3204effe5ac)
- **Fixed release**: Linux 7.0.1, referenced from [ChangeLog-7.0.1](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.1)
- **Primary symptom**: Kernel panic or `BUG_ON` in hugetlb reservation-map release after concurrent `UFFDIO_COPY`
- **VM suitability**: Medium. Requires huge pages and a race harness, but no hardware drivers.

## Bug description

`mfill_atomic_hugetlb()` used `linear_page_index()` to select the `hugetlb_fault_mutex_hash()` slot. The mismatch is that `linear_page_index()` returns normal `PAGE_SIZE` units, while the hugetlb mutex hash expects huge-page units. Two virtual addresses in the same huge page can therefore acquire different mutexes, allowing concurrent `UFFDIO_COPY` operations to corrupt hugetlb reservation accounting.

## Suggested starting prompt

> A debug kernel panics while two threads use userfaultfd `UFFDIO_COPY` into a hugetlbfs mapping. The panic occurs during hugetlb reservation-map release. Determine the likely subsystem, produce a minimal reproducer plan, and identify the locking invariant that is violated.

## Reproduction sketch

1. Boot an x86_64 VM with `CONFIG_HUGETLBFS=y`, `CONFIG_USERFAULTFD=y`, `CONFIG_KASAN=y`.
2. Reserve at least one huge page with `vm.nr_hugepages`.
3. Mount `hugetlbfs`.
4. Create a hugetlbfs mapping registered with userfaultfd missing-page handling.
5. Spawn two threads issuing `UFFDIO_COPY` to addresses that fall within the same huge page but produce distinct normal-page indexes.
6. Stress until reservation-map corruption or panic appears.

## Expected signal on vulnerable kernel

- Panic or warning in hugetlb reservation-map release path.
- Suspicious concurrent faults into the same huge page.
- Locking appears correct at first glance because both paths take a hugetlb fault mutex, but they are taking different hash slots.

## Fixed-kernel expectation

The same stress loop should run without reservation-map corruption because the mutex hash is computed in huge-page units.

## A/B scoring hints

- Award credit if the agent notices the unit mismatch between normal pages and huge pages.
- Award extra credit if it instruments the mutex hash input for both racing addresses.
- Penalize if it focuses only on userfaultfd copying without checking hugetlb locking and reservation accounting.

## Caveats

Race timing is nontrivial. This is a good intermediate test once deterministic cases are already working.
