# io_uring poll cancellation signedness ownership bug

## Summary

- **Subsystem**: `io_uring`, poll cancellation
- **Public reference**: [ChangeLog-7.0.4](https://www.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0.4)
- **Fix reference**: Upstream `45cd95763e198d74d369ede43aef0b1955b8dea4`, stable backport in 7.0.4
- **Fixed release**: Linux 7.0.4
- **Primary symptom**: Incorrect poll-cancellation ownership under high concurrency
- **VM suitability**: Medium. Pure userspace repro, but often needs stress and KCSAN/tracing.

## Bug description

`io_poll_get_ownership()` compared `atomic_read(&req->poll_refs)` as a signed integer against `IO_POLL_REF_BIAS`. When `IO_POLL_CANCEL_FLAG` set bit 31, the signed value became negative, so the `>= IO_POLL_REF_BIAS` guard could never pass. That skipped slow-path ownership handling during concurrent cancellation and completion.

## Suggested starting prompt

> Under io_uring poll cancellation stress, requests occasionally fail to follow the expected cancellation/completion ownership path. Inspect the ownership accounting and determine why a guard never fires when a cancellation flag is set.

## Reproduction sketch

1. Boot an x86_64 VM with `CONFIG_IO_URING=y`, `CONFIG_KCSAN=y`, and ftrace support.
2. Create many pollable FDs, such as pipes, sockets, or eventfds.
3. Submit many poll requests through io_uring.
4. Concurrently issue `IORING_OP_ASYNC_CANCEL` while completions are racing.
5. Trace `io_poll_get_ownership()` and log `poll_refs` as both signed and unsigned values.

## Expected signal on vulnerable kernel

- The high bit in `poll_refs` causes a negative signed value.
- A branch that should detect ownership eligibility never fires.
- Completion/cancellation ordering anomalies appear under stress.

## Fixed-kernel expectation

Ownership checks handle flag bits without signed comparison failure.

## A/B scoring hints

- Award credit if the agent explicitly identifies bit 31 signedness.
- Award extra credit if it proposes printing `poll_refs` in hex and signed decimal.
- Penalize generic race explanations that ignore the integer representation issue.

## Caveats

The symptom may be subtle without tracing. This is best used after easier crash-style cases.
