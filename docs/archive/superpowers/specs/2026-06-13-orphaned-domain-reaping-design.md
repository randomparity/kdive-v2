# Spec — Reap name-orphaned libvirt domains (#372)

- **ADR:** [`../../adr/0111-orphaned-domain-name-fallback-reaping.md`](../../adr/0111-orphaned-domain-name-fallback-reaping.md)
- **Issue:** [#372](https://github.com/randomparity/kdive/issues/372)
- **Date:** 2026-06-13

## Problem

A libvirt domain named by kdive's convention (`kdive-<system_id>`) but with no DB record on
this control plane and no usable kdive metadata tag is invisible to the `leaked_domains`
reaper. `repair_leaked_domains` skips any owned domain whose `system_id` is `None`, and a
provider's `list_owned` only emits domains with a parseable metadata tag — so the orphan is
never even a candidate. Result: `ops.reconcile_now` reports `leaked_domains: 0` with the
orphan live; manual `virsh undefine` required (campaign finding F5).

## Goal

The `leaked_domains` reaper collects a genuinely orphaned `kdive-<uuid>` domain and reports
it in the `leaked_domains` counter — with a predicate that **never** reaps a foreign domain
or one mid-creation.

## Design

### Name → System resolver (`kdive.providers.runtime_paths`)

`system_id_from_domain_name(name: str) -> UUID | None` — the inverse of `domain_name_for`.

- Anchored regex `^kdive-<uuid>$` where `<uuid>` is the canonical 8-4-4-4-12 hex shape.
- Returns the parsed `UUID`, or `None` when the name is not a bare System domain.
- The pattern is anchored so the build-VM form `kdive-build-<uuid>` does **not** match (the
  `build-` infix breaks the `kdive-<hex>` shape), keeping the two reapers disjoint.
- A non-UUID tail (`kdive-foo`) returns `None`.

### Predicate change (`repair_leaked_domains`)

Replace the unconditional skip:

```python
if domain.system_id is None:
    continue
```

with a fallback resolution:

```python
system_id = domain.system_id or system_id_from_domain_name(domain.name)
if system_id is None:
    continue
```

Everything downstream (`advisory_xact_lock`, the `has_live_row` query, the teardown-in-flight
query, `destroy`, counting, per-domain failure isolation) runs against the resolved
`system_id` unchanged. The tag stays authoritative when present; the name is a fallback.

### Provider enumeration (`local_libvirt.discovery.list_owned`)

Widen `list_owned` so a domain whose **name** matches `kdive-<uuid>` is surfaced even when
its metadata tag is absent/empty/unparseable, reported as `OwnedInfra(system_id="", ...)` →
the reconciler reads `system_id` as `None` and falls back to the name. The existing
metadata-tagged path is unchanged; an untagged domain that does **not** match the convention
is still skipped (not ours). `OwnedInfra` requires a `str` `system_id`; the empty string is
the on-the-wire `None`.

### Libvirt-backed `InfraReaper` adapter + assembly (the wiring the review caught)

**Review disposition (findings 1 & 2):** the predicate alone is inert — local-libvirt's
`list_owned` is not wired into `build_reconciler_reaper` (it returns `NullReaper` unless
fault-inject is enabled), and `OwnedInfra`→`OwnedDomain` has no adapter. Take the end-to-end
fix, not a phantom predicate.

`LibvirtInfraReaper` (`kdive.providers.local_libvirt.reaping`) realizes the reconciler
`InfraReaper` port over the local-libvirt discovery + provisioning ports:

- `async list_owned()` → `to_thread(discovery.list_owned)` then adapt each `OwnedInfra` →
  `OwnedDomain` via `_to_owned_domain`: `domain_name → name`; `system_id` is `UUID(s)` when
  `s` is a non-empty valid UUID string, else `None`. **Never** `UUID("")` (raises). Tested
  directly: `""`→None, `"not-a-uuid"`→None, a valid uuid string→that UUID.
- `async destroy(name)` → `to_thread(provisioning.teardown, name)` — destroy+undefine+overlay
  reclaim, idempotent over an already-absent domain (the lifecycle already swallows
  `VIR_ERR_NO_DOMAIN`/`VIR_ERR_OPERATION_INVALID`).
- Built lazily (`from_env`, no live connection at construction) so it is safe to assemble
  unconditionally.

`build_reconciler_reaper` composes `LibvirtInfraReaper` with the fault-inject reaper (when
enabled) through the existing `_CompositeReaper`. Local-libvirt is always-on, so the libvirt
reaper is always present; `NullReaper` is no longer the default for a stock deployment.

`_CompositeReaper.destroy(name)` fans out to **every** composed reaper — intentional and
benign: a libvirt orphan's `destroy` also hits the fault-inject reaper (`inventory.forget` —
a no-op for an unknown name), and a fault-inject domain's `destroy` also hits
`LibvirtInfraReaper.teardown` (a libvirt lookup that swallows `VIR_ERR_NO_DOMAIN` — a no-op
for an absent domain). Both teardowns are idempotent over an absent target, so the cross
fan-out never mis-acts; do not "narrow" it.

**Disposition (finding 3):** the mid-creation guard is reframed around the load-bearing
invariant — *a live (`state <> torn_down`) `systems` row for the resolved id protects the
domain* — and pinned by the `provisioning`-row test, not by define-ordering prose.

## Tests (TDD)

Resolver unit tests (`tests/providers/test_runtime_paths.py` or co-located):
- `kdive-<valid-uuid>` → that UUID.
- `kdive-build-<uuid>` → `None` (build form excluded).
- `kdive-foo`, `vm-leak`, `<uuid>` (no prefix), `kdive-` → `None`.
- Round-trip: `system_id_from_domain_name(domain_name_for(x)) == x`.

Reaper integration tests (`tests/reconciler/test_loop.py`, DB-gated, fake reaper):
- **orphan reaped:** `_FakeDomain(name="kdive-<uuid>", system_id=None)`, no row → count 1,
  destroyed.
- **foreign untouched:** `_FakeDomain(name="vm-untagged", system_id=None)` → count 0 (this is
  the existing `test_untagged_domain_not_reaped`, now asserting the *foreign* case explicitly).
- **mid-creation preserved:** seed a `provisioning` row for `sid`; `_FakeDomain(
  name="kdive-<sid>", system_id=None)` → count 0 (live row protects via name resolution).
- **idempotent:** second pass with the domain gone → count 0.
- Tag still wins: `_FakeDomain(name="kdive-<sid-A>", system_id=<sid-B>)` resolves to B (tag),
  not A — guards apply to B.

Local-libvirt `list_owned` test (fake injected connect): a fake domain whose name matches
the convention but whose `metadata(...)` raises `VIR_ERR_NO_DOMAIN_METADATA` is surfaced with
`system_id=""`; a tagged domain still carries its tag; a non-convention untagged domain is
still skipped.

`LibvirtInfraReaper` adapter tests (fake discovery/provisioning):
- `list_owned` adapts `OwnedInfra` → `OwnedDomain`: `domain_name→name`; `system_id=""`→None;
  `system_id="not-a-uuid"`→None; valid uuid string→that UUID.
- `destroy(name)` calls provisioning `teardown(name)` exactly once.

`build_reconciler_reaper` assembly test: a stock (no fault-inject) composition returns a
reaper whose `list_owned` reaches the (fake) libvirt discovery — i.e. **not** `NullReaper`.

## Out of scope

- Remote-libvirt `list_owned` (still `NullReaper`-backed / deferred).
- Any new MCP tool — the fix rides the existing `leaked_domains` sweep and `ops.reconcile_now`.
- Time-based reaping — ownership is by name + DB-row absence, never elapsed time.
