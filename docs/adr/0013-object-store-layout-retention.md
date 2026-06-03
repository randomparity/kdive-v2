# ADR 0013 — Object-store layout & retention

- **Status:** Proposed
- **Date:** 2026-06-03
- **Refines:** [0005](0005-postgres-object-store-state.md) (object store for bulk
  artifacts) and the spec's open follow-up "Object-store layout and retention
  policy for vmcores and transcripts".

## Context

The S3-compatible object store holds bulk artifacts referenced by Postgres rows
(see [0005](0005-postgres-object-store-state.md)). Raw artifacts are sensitive and
must be retained and garbage-collected on policy, and only redacted artifacts may
reach a response. See the spec's "System topology" and "Cross-cutting concerns"
(Mandatory redaction).

## Decision

Artifacts live in an S3-compatible store under a **prefix scheme
`{tenant}/{object_kind}/{object_id}/{artifact}`**, each object tagged with a
**sensitivity class** (`raw` = sensitive, `redacted` = shareable) and a
**retention class** (for example `vmcore`, `transcript`, `build-output`) that maps
to a bucket **lifecycle policy**. Rows reference artifacts by **key + etag**; raw
objects are fetched only by an explicit `artifacts.get` and are never inlined in a
response. **Tenant isolation is enforced by access control** (per-tenant bucket
policy or scoped credentials), not by the prefix alone — the prefix organizes, it
is not a security boundary. Artifacts are **write-once**, and retention **never
deletes an object still referenced by a live (non-terminal) row**: expiry applies
only once the referrer is terminal, and a reference whose object is gone surfaces
as `stale_handle`.

## Consequences

- Per-tenant prefix isolation and predictable keys make access control and
  garbage collection straightforward.
- Retention classes let vmcores (large, short-lived) and transcripts (small,
  audited) expire on different schedules, bounding storage cost.
- Sensitivity tagging enforces the redaction rule at the storage layer: only
  `redacted` objects are response-eligible.
- Requires bucket lifecycle configuration and an etag-consistency check on fetch; a
  deleted or rotated object surfaces as `stale_handle`.

## Alternatives considered

- **Flat random keys.** Rejected: no tenant isolation, no retention reasoning, hard
  to audit.
- **Store artifacts in Postgres.** Rejected by [0005](0005-postgres-object-store-state.md):
  GB-scale vmcores do not belong in the row store.
- **No retention policy.** Rejected: vmcores accumulate without bound; cost and
  exposure grow indefinitely.
