# ADR 0105 — Reap name-orphaned libvirt domains via the kdive naming convention

- **Status:** Proposed
- **Date:** 2026-06-13
- **Depends on:** [ADR-0021](0021-reconciler-drift-repair.md) (the reconciler drift-repair
  loop and its leaked-domain sweep), the provider `InfraReaper` /`OwnedDomain` port
  (`kdive.providers.reaping`), and the deterministic System domain name
  `kdive-<system_id>` (`kdive.providers.runtime_paths.domain_name_for`). Mirrors the
  name-derived-owner pattern already used by the ephemeral build-VM reaper
  ([ADR-0100](0100-ephemeral-libvirt-build-vm.md), `run_id_from_build_vm_name`).
- **Spec:** [`../superpowers/specs/2026-06-13-orphaned-domain-reaping-design.md`](../superpowers/specs/2026-06-13-orphaned-domain-reaping-design.md)
- **Issue:** [#372](https://github.com/randomparity/kdive/issues/372).

## Context

The reconciler's `leaked_domains` sweep (`repair_leaked_domains`) destroys provider domains
whose tagged System is gone and no teardown is in flight. It learns the owning System from
each domain's **kdive metadata element** (`parse_metadata_system_id`): a provider's
`list_owned` returns only domains carrying a parseable `<kdive:system>` tag, and the sweep
skips any domain it sees with `system_id is None`.

The MCP coverage campaign (`docs/reports/mcp-coverage-campaign-2026-06-13.md`, finding F5)
hit a live domain that matched kdive's naming convention (`kdive-<uuid>`) but had **no DB
record on this control plane and no usable metadata tag**. Such a domain is invisible to
the sweep: `ops.reconcile_now` reported `leaked_domains: 0` with the orphan still running,
and the operator had to remove it out-of-band with `virsh undefine`. A domain can lose its
metadata-derived ownership while still being unambiguously ours — e.g. a crash between
`defineXML` and the metadata write, a libvirt store that drops the metadata element, or a
provider that names by convention without re-reading metadata at list time. The DB-backed
ownership signal (the deterministic name) is then the only one left.

The domain name is a **stronger** ownership signal than the metadata tag for orphan
detection: kdive always defines a System domain as `kdive-<system_id>` (`render_domain_xml`
sets `<name>` and the metadata to the same id). So a `kdive-<uuid>` domain whose `uuid` has
no live `systems` row is genuinely orphaned. The load-bearing safety invariant is **not**
define-ordering prose but the existing guard restated against the resolved id: *a live
(`state <> torn_down`) `systems` row for the resolved System protects its domain*. A System
mid-creation has such a row (the provisioning job that defines the domain operates on an
already-inserted `defined`/`provisioning` row), so it is preserved without any new race —
and a pinned `provisioning`-row test, not an ordering claim, enforces this.

A correct predicate is necessary but **not sufficient**: on a real deployment the local
libvirt provider's `list_owned` was never wired into the reconciler reaper
(`build_reconciler_reaper` returned `NullReaper` unless fault-inject was enabled), so the
orphan never reached `repair_leaked_domains`. This ADR therefore also adds the missing
libvirt-backed `InfraReaper` adapter and assembles it, so the end-to-end F5 path actually
reaps.

## Decision

### 1. Derive the owning System from the domain name when the metadata tag is absent

Add `system_id_from_domain_name(name) -> UUID | None` to `kdive.providers.runtime_paths`,
the inverse of `domain_name_for`. It matches the anchored convention
`^kdive-<uuid-v-shaped>$` and returns the parsed `UUID`, or `None` for any name that is not
a bare System domain — foreign names, the build-VM form `kdive-build-<uuid>` (a different
reaper owns it; the System pattern is anchored so `build-` can never match), the probe/other
prefixed forms, and anything not UUID-shaped.

`repair_leaked_domains` resolves each domain's owning System as:

```
system_id = domain.system_id or system_id_from_domain_name(domain.name)
if system_id is None:
    continue          # not a kdive System domain → foreign/unmanaged → never reaped
```

The metadata tag, when present, stays authoritative (a future tag scheme that diverges from
the name is honored). The name is a **fallback**, consulted only when the tag is `None`.

### 2. The safe predicate: name-ownership AND no DB backing AND no in-flight teardown

A domain is reaped only when **all** hold:

- **It is ours by naming convention** — `system_id` resolved (tag or name). A name that does
  not match `kdive-<uuid>` is left strictly untouched. This is the foreign-domain guard.
- **No live `systems` row backs it** — same `state <> torn_down` check as today, under the
  per-System `advisory_xact_lock`. *A live row for the resolved id protects the domain* —
  this is the load-bearing invariant and the mid-creation guard: a `defined`/`provisioning`/
  `ready`/… row leaves the domain untouched regardless of any ordering assumption, and a
  System whose creation is in flight always has such a row.
- **No teardown job is in flight** for that System — unchanged guard (b).

No new state, table, column, or time predicate. The change is one resolution line plus the
removal of the unconditional `system_id is None` skip; every existing guard is reused
verbatim against the resolved id, so a name-resolved orphan and a tag-resolved leak take the
identical safe path.

### 3. Wire a libvirt-backed `InfraReaper` and widen the enumeration

The end-to-end fix has three parts:

1. **Adapter** — a `LibvirtInfraReaper` (the established reconciler→provider seam, the same
   `InfraReaper` shape the fault-inject reaper realizes) whose `list_owned()` calls the
   local-libvirt discovery's `list_owned` and adapts each `OwnedInfra` (`{system_id: str,
   domain_name: str}`) into the reconciler's `OwnedDomain` (`{name, system_id: UUID | None}`):
   `domain_name → name`; a `system_id` that is empty or not a valid UUID → `None` (never
   `UUID("")`, which raises). `destroy(name)` routes to the provisioning teardown
   (destroy+undefine+overlay reclaim), idempotent over an already-absent domain. Both libvirt
   calls are offloaded with `asyncio.to_thread` (the discovery/provisioning ports are sync).
2. **Enumeration** — local-libvirt `list_owned` is widened to also surface a convention-named
   (`kdive-<uuid>`) domain whose metadata tag is absent/empty/unparseable, reported with
   `system_id=""` so the adapter maps it to `None` and the reconciler falls back to the name.
   A non-convention untagged domain is still skipped (not ours).
3. **Assembly** — `build_reconciler_reaper` composes the `LibvirtInfraReaper` (local-libvirt
   is always-on: `KDIVE_LIBVIRT_URI` defaults to `qemu:///system` and the local runtime is
   always registered) with the fault-inject reaper when enabled, via the existing
   `_CompositeReaper`. The adapter is constructed lazily (`from_env` opens no connection), so
   unconditional assembly is safe; a unit test injects a fake connect factory.

Remote-libvirt `list_owned` remains deferred (its reaper is `NullReaper`-backed today); when
it lands it adopts the same enumeration + adapter rule.

## Consequences

- A genuinely orphaned `kdive-<uuid>` domain with no DB row is reaped by the existing
  `leaked_domains` sweep and counted in `leaked_domains`; no out-of-band `virsh undefine`.
- A foreign/unmanaged domain (any name not matching `kdive-<uuid>`) is never reaped — the
  resolution returns `None` and the sweep `continue`s, identical to today.
- A domain mid-creation is preserved: a live `systems` row for the resolved id keeps the
  `has_live_row` guard skipping it — no new race is opened (pinned by a `provisioning`-row test).
- The end-to-end path is real, not a phantom predicate: the libvirt-backed reaper is now
  assembled into `build_reconciler_reaper`, so a real deployment's orphan reaches the sweep.
- Idempotent: the first pass destroys the orphan; a second pass finds it gone (the provider
  no longer lists it) and reaps nothing.
- The build-VM domain form `kdive-build-<uuid>` is **not** matched by the System pattern, so
  this sweep never double-acts with the build-VM reaper.
