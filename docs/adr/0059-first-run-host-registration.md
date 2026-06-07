# ADR 0059 — First-run local-libvirt host registration at reconciler startup

- **Status:** Proposed
- **Date:** 2026-06-06
- **Depends on:** [ADR-0021](0021-reconciler-drift-repair.md) (the reconciler process this
  hooks), [ADR-0023](0023-resource-registration.md) (`register_local_libvirt_resource`, the
  idempotent upsert this reuses).

## Context

`register_local_libvirt_resource` persists the discovered libvirt host as the single
`resources` row that `allocations.request` admits against. Until now nothing in the running
system called it — only tests did — so a freshly migrated database has zero `resources` rows
and the very first `allocations.request` fails `configuration_error` ("no resource of kind
`local-libvirt`") until an operator seeds the row out of band. This was found driving the live
build→boot→verify pipeline: the demo could not request an allocation against a clean stack.

## Decisions

### 1. The reconciler registers the local host once at startup

`_run_reconciler` calls `ensure_local_host_registered(pool)` before entering the repair loop.
An unregistered host is exactly the kind of Postgres/infra drift the reconciler exists to
repair, and the reconciler is the process that owns host-facing reconciliation, so it is the
natural bootstrap site — no new operator step, no `server`-startup coupling.

### 2. It is best-effort and idempotent

`ensure_local_host_registered` delegates to `register_local_libvirt_resource`, which upserts by
`(kind, host_uri)`, so every startup re-asserts the row (and refreshes the advertised
capabilities) without creating duplicates. A registration failure (libvirt unreachable, bad
`KDIVE_LIBVIRT_ALLOCATION_CAP`) is logged and swallowed: it must not crash the reconciler, the
other repairs still run, and the next startup retries.

### 3. Scope: local-libvirt only

This bootstraps the one provider that exists (ADR-0009's single M0 provider). Multi-host /
remote providers register through their own discovery path when they land. Resource limits
beyond the concurrent-allocation cap (max VMs, vCPUs per VM) are out of scope here; they will
extend the advertised `Resource.capabilities` in a later change.
