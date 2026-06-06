# ADR 0050 — Method-aware vmcore storage: first-method-wins per System

- **Status:** Proposed
- **Date:** 2026-06-06
- **Depends on:** [ADR-0049](0049-crash-capture-tiers.md) (the provider-agnostic capture method
  vocabulary, the method-aware admission dedup key, and the `host_dump`/`kdump` split this
  extends), [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) (the `Retriever.capture` port,
  the System-scoped raw `vmcore` object, and the `_existing_raw_key` idempotency guard +
  `postmortem.*` resolution this makes method-aware).
- **Spec:** [`../superpowers/specs/2026-06-06-vmcore-method-aware-storage-design.md`](../superpowers/specs/2026-06-06-vmcore-method-aware-storage-design.md)
- **Closes:** [#118](https://github.com/randomparity/kdive/issues/118).

## Context

ADR-0049 gave `vmcore.fetch` a `method` argument and a method-aware **admission** dedup key
(`{system_id}:capture_vmcore:{method}`), so distinct methods enqueue distinct jobs. But the
capture **storage/idempotency** layer stayed method-agnostic: the raw core is stored under the
fixed System-scoped object name `vmcore`, and `_existing_raw_key` guards re-capture with a
`LIKE '%/vmcore'` match. The two layers disagree about identity.

Once `kdump` joins `LOCAL_LIBVIRT_SUPPORTED` (tracked by #115), the disagreement is a
correctness bug: `vmcore.fetch(method=host_dump)` stores a `vmcore` row; a later
`vmcore.fetch(method=kdump)` is **admitted** (distinct dedup key) and its job runs, but the
storage guard finds the existing `%/vmcore` row and no-ops, returning the prior **host_dump**
core. The agent asked for a `kdump` core and silently received a `host_dump` core, with no error.

The bug is unreachable in M0 (`kdump ∉ LOCAL_LIBVIRT_SUPPORTED`, so `vmcore.fetch(kdump)` is
rejected at admission), but the storage layer must be made correct-and-ready before #115 lands.
Two layers consume the raw core today and a third writes it; the `artifacts` row has no free-form
metadata column, so the capturing method has nowhere to live **except the object key**.

## Decisions

1. **One vmcore per System; the first method to capture wins.** A System holds at most one raw
   core. A second `vmcore.fetch` whose method differs from the stored core's does **not**
   re-capture and does **not** silently substitute — it fails with a typed `configuration_error`
   that names the existing method and the requested method. This matches the established
   one-artifact-per-System pattern (the console artifact, ADR-0049 Decision 4 / #117: a single
   System-scoped object). Holding multiple cores per System was rejected (see below).

2. **The capturing method is encoded in the raw object key (`vmcore-{method}`).** With no
   metadata column, the key is the only no-migration place to persist which method produced a
   core. The raw object becomes `…/systems/{system_id}/vmcore-{method}` and its redacted
   derivative `…/vmcore-{method}-redacted`. The capturing method is recovered by parsing the key
   suffix, co-located with the data it describes — not inferred from a sibling table.

3. **The idempotency guard is the correctness boundary, and it is method-aware.** The decision is
   enforced in the `capture_vmcore` **handler**, under the per-System advisory lock that already
   serializes capture. Given an existing raw core: a **same-method** re-dispatch returns it
   (idempotent, as today); a **different-method** core raises `configuration_error` so the worker
   dead-letters the job rather than storing or substituting. The reject is placed in **both**
   `_precheck_system` (before the slow `capture()` seam — so the common case writes no object) and
   `_finalize_capture` (after `capture()`, the race backstop). The lock makes this correct even
   when two different-method jobs race post-#115 — the loser's finalize re-check rejects. The
   agent-facing signal is the job's `error_category` (`configuration_error`); the
   `existing_method`/`requested_method` detail rides the `CategorizedError` for logs, since
   `queue.fail` persists the category, not the details.

4. **No admission-layer cross-method pre-check in M0.** A synchronous reject at `vmcore.fetch`
   admission would need both methods to be admittable; in M0 only `host_dump` is, so an admission
   pre-check can never fire and would be untestable, speculative code. It is deliberately omitted.
   This means M0 satisfies the issue's "fails or explains" with **fails**: a typed
   `configuration_error` on the dead-lettered job (distinct from a silent substitution), not a
   synchronous, method-naming explanation to the agent. When #115 makes `kdump` admittable, an
   admission fast-path that returns the same `configuration_error` — synchronously, with the method
   detail in the response `data` — before enqueuing is a cheap, then-testable addition.

5. **The two raw-core readers stay single-core and keep their tool surface.** `postmortem.crash` /
   `postmortem.triage` (`vmcore.py`) and `introspect.from_vmcore` (`introspect.py`) each resolve
   *the* one raw core for a System. Their SQL changes from `LIKE '%/vmcore'` to "the single
   method-suffixed raw key" (`LIKE '%/vmcore-%'` excluding the `-redacted` derivative); no
   agent-facing `method` argument is added to either tool, because first-method-wins guarantees at
   most one raw core to resolve.

## Consequences

- The silent-substitution bug cannot occur the moment `kdump` joins `LOCAL_LIBVIRT_SUPPORTED`:
  the second method either is the same (idempotent) or is rejected with a clear categorized error.
- The raw/redacted object key format changes (`vmcore` → `vmcore-{method}`). M0 carries no
  persisted production cores, so this is a one-time format shift, not a migration; the three
  `%/vmcore` readers and the `vmcore-redacted` list filter move to the new shape in lockstep.
- A System provisioned for one method and later wanting another method's core has no in-place
  remediation (no vmcore-delete tool exists). This is accepted: the demo path captures one method
  per crashed System, and re-capturing a different method on the *same* crash is not a current
  need. A delete/replace affordance can be added if one materializes — it is not built speculatively.
- **Object-store orphan on the race backstop.** The `_finalize_capture` reject fires after
  `capture()` has already written the loser's object to the store, so that object is left
  unreferenced (no row). No inline cleanup is added: it carries `retention_class="vmcore"` like any
  core and is reaped by the existing retention/reconciler sweep, and the path is only reachable
  post-#115 under genuine two-method concurrency. This is the same orphan shape the existing
  same-method post-capture race already produces; this change adds a second trigger, not a new kind
  of leak. The `_precheck_system` reject keeps the non-racing common case orphan-free.
- A bare `vmcore` object key (no method suffix) is unsupported after this change: the producer only
  writes method-suffixed keys, the readers' `LIKE '%/vmcore-%'` would not match a bare key, and
  `_captured_method` raises (`infrastructure_failure`) rather than treat the whole key as a method.
  M0 carries no such persisted keys; fixtures move to the suffixed shape in lockstep.
- `postmortem.*` and `introspect.from_vmcore` remain method-blind by construction: they read
  whatever single core the System holds. If a future need requires choosing among cores, that
  reopens Decision 1.

## Considered & rejected

- **Re-capture per method (multiple cores per System).** Store `vmcore-host_dump` and
  `vmcore-kdump` side by side so a second method genuinely produces its own core. Rejected:
  the two readers (`postmortem.*`, `introspect.from_vmcore`) would each need a method-selection
  rule — a fixed precedence (e.g. `kdump > host_dump`) or a new agent-facing `method` argument —
  which is surface growth that no current consumer needs (No speculative features). It also breaks
  the one-artifact-per-System pattern the console artifact established.
- **Status quo (method-agnostic storage).** Keep the `%/vmcore` guard. Rejected: this *is* the
  bug — it silently returns the wrong method's core.
- **Persist the method in a new `artifacts` metadata column.** Rejected: a schema migration for a
  single discriminator the object key already carries unambiguously; the key encoding is
  zero-migration and keeps the method co-located with the object.
- **Synchronous admission reject in M0.** Rejected as unreachable/untestable while `kdump` is
  unsupported (Decision 4); deferred to the #115 change that makes it reachable.
