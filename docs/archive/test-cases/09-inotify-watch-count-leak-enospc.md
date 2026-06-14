# inotify watch-count leak causing permanent ENOSPC

## Summary

- **Subsystem**: VFS, fsnotify, inotify
- **Public reference**: [ChangeLog-7.0.4](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.4)
- **Fix reference**: Upstream `6a320935fa4293e9e599ec9f85dc9eb3be7029f8`, stable backport in 7.0.4
- **Fixed release**: Linux 7.0.4
- **Primary symptom**: `inotify_add_watch()` returns `ENOSPC` even with no active watches
- **VM suitability**: Easy with fault injection.

## Bug description

`inotify_new_watch()` incremented the user's inotify watch count before calling into fsnotify mark setup. If `fsnotify_add_inode_mark_locked()` failed, the error path removed the watch from the IDR but failed to decrement the user watch count. Repeated failures leaked watch-count quota until all future watch additions returned `ENOSPC`.

## Suggested starting prompt

> A user repeatedly triggers failed `inotify_add_watch()` calls under fault injection. Afterward, even with no active watches, every new watch fails with `ENOSPC`. Find the missing cleanup path.

## Reproduction sketch

1. Boot with `CONFIG_INOTIFY_USER=y`, `CONFIG_FAULT_INJECTION=y`, and `CONFIG_FAILSLAB=y`.
2. Enable allocation failure for the relevant mark allocation path.
3. Create an inotify fd and repeatedly call `inotify_add_watch()`.
4. Ensure the mark-add path fails after the watch count increments.
5. Disable fault injection and attempt a normal watch.
6. Observe persistent `ENOSPC`.

## Expected signal on vulnerable kernel

- Failed add-watch calls increase per-user watch accounting.
- No corresponding active watches exist.
- Later normal `inotify_add_watch()` calls fail with `ENOSPC`.

## Fixed-kernel expectation

Error paths decrement watch accounting when mark installation fails.

## A/B scoring hints

- Award credit if the agent checks accounting symmetry: increment and decrement paths.
- Award extra credit if it uses fault injection to make the bug deterministic.
- Penalize if it only raises `fs.inotify.max_user_watches` instead of explaining the leak.

## Caveats

This is one of the best initial cases because it is deterministic, requires no special hardware, and produces a user-visible failure.
