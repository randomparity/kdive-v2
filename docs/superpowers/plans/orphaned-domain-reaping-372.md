# Plan — Reap name-orphaned libvirt domains (#372)

- **Spec:** [`../specs/2026-06-13-orphaned-domain-reaping-design.md`](../specs/2026-06-13-orphaned-domain-reaping-design.md)
- **ADR:** [`../../adr/0105-orphaned-domain-name-fallback-reaping.md`](../../adr/0105-orphaned-domain-name-fallback-reaping.md)
- **Branch:** `feat/reap-orphaned-domain-372`

TDD throughout: write the failing test, then the minimal code. Each step ends green before
the next. Collision group R (#371): keep commits small/rebasable; shared files are
`reconciler/provider_reaping.py`, `reconciler/loop.py`, `providers/composition.py`,
`mcp/tools/ops/reconcile.py`.

## Step 1 — `system_id_from_domain_name` resolver (`providers/runtime_paths.py`)

- Test (`tests/providers/test_runtime_paths.py`, new or existing): valid `kdive-<uuid>`→UUID;
  `kdive-build-<uuid>`→None; `kdive-foo`/`vm-leak`/bare-uuid/`kdive-`→None; round-trip with
  `domain_name_for`.
- Code: anchored regex `^kdive-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$`,
  case-insensitive on the hex; `UUID(match)` in a try/except returning None. Mirror
  `run_id_from_build_vm_name`.

## Step 2 — predicate fallback (`reconciler/provider_reaping.py`)

- Test (`tests/reconciler/test_loop.py`, DB-gated): orphan `_FakeDomain(name="kdive-<uuid>",
  system_id=None)` no row → count 1, destroyed. Foreign `_FakeDomain(name="vm-untagged",
  system_id=None)` → count 0. Mid-creation: seed `provisioning` row for `sid`,
  `_FakeDomain(name="kdive-<sid>", system_id=None)` → count 0. Tag-wins:
  `name="kdive-<sidA>", system_id=sidB` → guards apply to sidB. Idempotent second pass.
- Code: replace `if domain.system_id is None: continue` with
  `system_id = domain.system_id or system_id_from_domain_name(domain.name)` then
  `if system_id is None: continue`. Use `system_id` (not `domain.system_id`) in the lock +
  queries + log. Update the docstring.

## Step 3 — widen local `list_owned` (`providers/local_libvirt/discovery.py`)

- Test (`tests/providers/local_libvirt/test_discovery.py`): a convention-named
  (`kdive-<valid-uuid>`) domain whose `metadata` raises `VIR_ERR_NO_DOMAIN_METADATA` is
  surfaced as `{"system_id": "", "domain_name": "kdive-<uuid>"}`; a tagged domain keeps its
  tag; a non-convention untagged domain (`other-vm`) is still skipped. (The existing
  `test_list_owned_returns_only_tagged_domains` keeps passing: `kdive-1` is tagged; `other-vm`
  is non-convention untagged → skipped.)
- Code: in the `system_id is None` branch (no/empty metadata), instead of unconditional
  `continue`, check `system_id_from_domain_name(domain.name())`; if it matches, append
  `OwnedInfra(system_id="", domain_name=domain.name())`; else `continue`. Keep the
  non-metadata libvirt error re-raise unchanged.

## Step 4 — `LibvirtInfraReaper` adapter (`providers/local_libvirt/reaping.py`, new)

- Test (`tests/providers/local_libvirt/test_reaping.py`, new): with a fake discovery returning
  `OwnedInfra` rows and a fake provisioning recording teardown calls — `list_owned` adapts
  `domain_name→name`, `system_id=""`→None, `"not-a-uuid"`→None, valid uuid→UUID;
  `destroy(name)` calls `teardown(name)` once.
- Code: `LibvirtInfraReaper(discovery, provisioning)` taking **injected** discovery +
  provisioning ports (so tests never connect); `from_env()` is the default constructor that
  builds both via their `from_env`. `async list_owned()` = `to_thread(discovery.list_owned)`
  then map via `_to_owned_domain` (module-level `_OwnedDomain` dataclass with `name`,
  `system_id: UUID | None`). `async destroy(name)` = `to_thread(provisioning.teardown, name)`.
  `_uuid_or_none(s)`: `UUID(s)` in try/except for empty/invalid → None.

## Step 5 — assemble in `build_reconciler_reaper` (`providers/composition.py`)

**Injection seam (review finding 1):** `build_reconciler_reaper`'s reaper connects to libvirt
on `list_owned()` (not at construction). To keep the composition unit tests hermetic,
`build_reconciler_reaper` takes an injectable `libvirt_reaper: InfraReaper | None = None`
(default `local_libvirt.composition.build_reaper()` → `LibvirtInfraReaper.from_env()`). Tests
pass a fake-discovery-backed reaper; production passes nothing.

- Code: `reapers = [libvirt_reaper or local_libvirt_composition.build_reaper()]`; append the
  fault-inject reaper when enabled; the existing `len==1`/`>1` collapse composes. Add
  `build_reaper` to `local_libvirt/composition.py` mirroring `fault_inject_composition.build_reaper`.
- **Test rewrites (review findings 1 & 2) — both are deliberate RED→GREEN flips:**
  - Rewrite `test_reconciler_reaper_defaults_to_null_when_fault_inject_is_disabled`
    (`test_composition.py:401`): it currently asserts the stock reaper **is** `NullReaper` —
    that pins the #372 bug. New assertion: the stock reaper is the libvirt-backed reaper, and
    its `list_owned` reaches an injected fake discovery (no live connection). Rename to
    `test_reconciler_reaper_is_libvirt_backed_without_fault_inject`.
  - Update `test_configured_fault_inject_runtime_is_visible_to_reconciler_reaper`
    (`test_composition.py:301`): it now composes the libvirt reaper too and calls
    `list_owned()` → would open a real `qemu:///system`. Pass an injected fake-discovery
    libvirt reaper so the composite stays hermetic; keep the fault-inject visibility assertion.
- New test: stock + fault-inject → `_CompositeReaper` of both; `list_owned` unions the (fake)
  libvirt discovery rows and the fault-inject rows.

## Step 6 — guardrails + docs

- `just lint`, `just type`, `just test` green after each step; full `just ci` at the end.
- Confirm no generated-doc/meta-test drift (tool docs unaffected; no new MCP tool).

## Risk notes

- Existing `test_reconcile_once_counts_a_mixed_pass` constructs `reconcile_once(pool, reaper)`
  with an explicit `FakeReaper` — unaffected by composition wiring (the reaper is injected).
- `build_reconciler_reaper` callers (`__main__.py`, `mcp/app.py`) pass no args → still valid
  (the injected `libvirt_reaper` defaults to the real one).
- **Hermeticity:** any composition test that calls `reaper.list_owned()` MUST inject a
  fake-discovery libvirt reaper — the real one opens `libvirt.open(host_uri)` on `list_owned`.
- Collision: Steps 2 & 5 touch files #371 also edits. Keep each step a separate commit.
