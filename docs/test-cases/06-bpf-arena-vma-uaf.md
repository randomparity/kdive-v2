# BPF arena parent-VMA use-after-free

## Summary

- **Subsystem**: BPF, `bpf_arena`, VMA lifecycle
- **Public reference**: [ChangeLog-7.0.7](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.7)
- **Fix reference**: Upstream `4fddde2a732de60bb97e3307d4eb69ac5f1d2b74`, stable backport in 7.0.7
- **Fixed release**: Linux 7.0.7
- **Primary symptom**: KASAN use-after-free through stale VMA pointer after fork or remap
- **VM suitability**: Easy to medium. Pure BPF/userspace once BPF arena support is enabled.

## Bug description

`arena_vm_open()` incremented mapping accounting but did not correctly register child VMAs created by `fork()` or unsafe remap patterns. The arena bookkeeping could retain a pointer to the parent VMA. If the parent unmapped the arena while a child still operated on it, later arena operations could dereference a freed VMA.

## Suggested starting prompt

> A KASAN-enabled kernel reports a use-after-free when a process forks after mapping a BPF arena, the parent unmaps the arena, and the child later frees arena pages. Diagnose the VMA lifetime bug.

## Reproduction sketch

1. Boot with `CONFIG_BPF=y`, `CONFIG_BPF_SYSCALL=y`, `CONFIG_BPF_ARENA=y`, `CONFIG_KASAN=y`.
2. Load a small BPF program using a BPF arena map.
3. Map the arena into userspace.
4. Fork.
5. Unmap the arena in the parent.
6. Trigger arena page free activity in the child.
7. Capture KASAN stack traces around VMA use.

## Expected signal on vulnerable kernel

- KASAN UAF involving arena VMA bookkeeping.
- Parent VMA address appears in child-side arena state.
- Crash occurs after parent `munmap()` rather than at map creation.

## Fixed-kernel expectation

The mapping is not inherited in unsafe ways, or VMA split/remap operations are rejected so the arena cannot keep a stale parent VMA pointer.

## A/B scoring hints

- Award credit if the agent follows VMA open/close lifecycle across fork.
- Award extra credit if it suggests checking `VM_DONTCOPY`, `may_split`, and `mremap` behavior.
- Penalize if it treats the BPF program itself as corrupt without checking arena mapping ownership.

## Caveats

Requires a kernel and userspace toolchain new enough for BPF arena tests.
