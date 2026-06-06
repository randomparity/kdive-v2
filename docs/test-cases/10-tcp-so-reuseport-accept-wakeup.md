# TCP SO_REUSEPORT accept wakeup loss

## Summary

- **Subsystem**: networking, TCP, `SO_REUSEPORT`
- **Public reference**: [ChangeLog-7.0.4](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.4)
- **Fix reference**: Upstream `3864c6ba1e041bc75342353a70fa2a2c6f909923`, stable backport in 7.0.4
- **Fixed release**: Linux 7.0.4
- **Primary symptom**: Blocking `accept()`, `poll()`, or `epoll_wait()` sleeps despite a migrated child socket being queued
- **VM suitability**: Medium. Pure loopback networking, but requires timing.

## Bug description

During listener shutdown, `inet_csk_listen_stop()` can migrate a child socket to another listener in a `SO_REUSEPORT` group. The vulnerable path enqueued the child on the new listener's accept queue but did not call the wakeup path for the receiving listener. As a result, a thread waiting in `accept()` or `epoll_wait()` could remain asleep even though work was available.

## Suggested starting prompt

> A loopback TCP test with multiple `SO_REUSEPORT` listeners occasionally hangs. A child connection is present on a listener's accept queue, but the blocking accept thread is not woken. Determine what notification was missed during listener migration.

## Reproduction sketch

1. Boot a normal x86_64 VM; no special hardware required.
2. Create two or more TCP listeners on the same loopback port using `SO_REUSEPORT`.
3. Block one thread in `accept()` or `epoll_wait()` on one listener.
4. Generate client connections while closing another listener in the reuseport group.
5. Repeat until a child connection is migrated but the waiter does not wake.
6. Instrument accept queue length and wakeup callbacks.

## Expected signal on vulnerable kernel

- Accept queue has an enqueued child.
- The userspace waiter remains asleep.
- Wakeup callback for the receiving listener is missing.

## Fixed-kernel expectation

Migrated child sockets trigger the same accept-queue accounting and data-ready wakeup path as normally accepted children.

## A/B scoring hints

- Award credit if the agent distinguishes queue insertion from waiter notification.
- Award extra credit if it instruments `sk_acceptq_added()` and `sk_data_ready()`.
- Penalize if it only tunes client/server timing without identifying the missing wakeup.

## Caveats

This case is good for networking-debug workflow evaluation, but it needs a timing-controlled harness to avoid flaky benchmark results.
