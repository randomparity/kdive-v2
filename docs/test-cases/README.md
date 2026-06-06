# Linux 7.0.x Kernel Bug A/B Test Cases

This directory contains individual Linux kernel bug case files for evaluating whether a VM-based MCP debugging environment improves agent performance on kernel issue identification and diagnosis.

Each case is intended to be run as an A/B experiment:

- **Baseline**: Give the agent the public bug report or symptom and normal terminal access.
- **MCP-assisted**: Give the agent the same starting information plus the KDIVE environment setup, VM provisioning tools, debug-kernel configuration, log capture, repro execution, and kernel-source navigation helpers.
- **Scoring**: Compare time to first useful hypothesis, ability to reproduce, correct subsystem identification, quality of root-cause explanation, and whether the agent can identify the relevant fix or minimal patch direction.

## Recommended order

Start with deterministic cases, then move into race/concurrency cases:

1. `05-dcache-dhash-entries-oob-read.md`
2. `09-inotify-watch-count-leak-enospc.md`
3. `06-bpf-arena-vma-uaf.md`
4. `03-io-uring-poll-ownership-signedness.md`
5. `10-tcp-so-reuseport-accept-wakeup.md`
6. `01-hugetlb-userfaultfd-mutex-hash.md`
7. `02-blkcg-cgwb-release-uaf.md`
8. `08-vmalloc-vrealloc-oob-copy.md`
9. `07-sched-ext-scx-root-uaf.md`
10. `04-io-uring-zcrx-freelist-oob-write.md`

## Kernel config baseline

Use a debug kernel for most cases:

```text
CONFIG_KASAN=y
CONFIG_KASAN_INLINE=y
CONFIG_KCSAN=y
CONFIG_FAULT_INJECTION=y
CONFIG_FAILSLAB=y
CONFIG_FAIL_PAGE_ALLOC=y
CONFIG_HUGETLBFS=y
CONFIG_USERFAULTFD=y
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_BPF_ARENA=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_IO_URING=y
CONFIG_INOTIFY_USER=y
CONFIG_CGROUPS=y
CONFIG_CGROUP_WRITEBACK=y
```

## Common scoring rubric

| Dimension | 0 | 1 | 2 |
|---|---|---|---|
| Reproduction | Cannot reproduce | Partial or flaky reproduction | Reliable repro with logs |
| Subsystem localization | Wrong subsystem | Broad area only | Correct files/functions |
| Root cause | Incorrect | Plausible but incomplete | Explains the actual bug mechanism |
| Debug method | Ad hoc | Uses logs/traces | Uses targeted kernel instrumentation |
| Fix direction | None/wrong | Broad direction | Identifies precise invariant or patch area |
