# Kdump kernel-config fragment provisioning

- **Date:** 2026-06-11
- **ADR:** [`../../adr/0096-kdump-config-fragment-build-input.md`](../../adr/0096-kdump-config-fragment-build-input.md)
- **Milestone:** kernel-build-config provisioning
- **Status:** Proposed

## Context

The build → install → boot → debug loop is validated end-to-end only for `host_dump`. The
`kdump`, `gdbstub`, and `console` methods run on a System whose kernel is built from source, and
that build cannot succeed today: the integration seed names a config ref `{kind: local, path:
"/configs/kdump.config"}`, a path nothing provisions into the worker. There is no canonical kdump
kernel config, no artifact the build resolves, and no file an operator or agent can retrieve.

Verified current state (branch `main`):

- **Build flow, both providers.** `local_libvirt/build.py` and `remote_libvirt/build.py` stage a
  complete `.config` by copying the resolved `local` ref to `workspace/.config` (`_stage_config`),
  then run `make olddefconfig`, read it back, run the `_missing_config_groups` preflight, then
  `make`. No `make defconfig` is run; a complete `.config` is expected.
- **Config-ref resolution.** Both `_resolve_config_ref` functions reject any ref that is not
  `LocalComponentRef`. `composition.py` declares `CONFIG_COMPONENT: {"local"}` for both providers.
  `CatalogComponentRef` is wired only for rootfs (`materialize.py`), never for config.
- **Preflight.** `_REQUIRED_CONFIG` is two OR-groups: `(CONFIG_CRASH_DUMP,)` and
  `(CONFIG_DEBUG_INFO_DWARF4, CONFIG_DEBUG_INFO_DWARF5, CONFIG_DEBUG_INFO_BTF)`. Nothing supplies
  these, so the check can only fail.
- **Catalog precedents.** `image_catalog` (ADR-0092) is the object-store + DB-backed rootfs
  catalog seeded at bootstrap (`_seed_baseline_rootfs`). The fixture catalog (ADR-0065) is
  filesystem-backed YAML and has no config entry type.

## Non-goals

- Per-arch fragments. One shared, arch-agnostic `kdump` fragment; `merge_config.sh` tolerates a
  base missing a symbol. An arch axis is deferred until a genuinely arch-specific kdump option
  appears.
- Full version-pinned `.config` mode. The build always merges a fragment onto `make defconfig`.
- gdbstub/console-specific fragments. The three from-source methods share the kernel; the kdump
  fragment configures it for all of them. (gdbstub is a host-/QEMU-side attach; console is a boot
  cmdline concern — neither needs distinct build config.)
- Uploading custom fragments via a new MCP write path. The existing `component-upload` ref already
  covers a caller supplying its own config.

## Decision

One repo-committed kdump config **fragment**, published once to the object store as a seeded
**build-config catalog** entry, resolved by a stable `catalog` ref (or an implicit default) from
the local build, the remote build, and an inline MCP read tool. The build merges the fragment
onto the kernel tree's own `make defconfig`.

### 1. The fragment

`provisioning/configs/kdump.config` — generic `CONFIG_*` lines that make a kernel kdump-capable
and symbolizable. Indicative set (final set pinned in the plan against a real `make olddefconfig`):

```
CONFIG_KEXEC=y
CONFIG_KEXEC_CORE=y
CONFIG_CRASH_DUMP=y
CONFIG_PROC_VMCORE=y
CONFIG_RELOCATABLE=y
CONFIG_RANDOMIZE_BASE=y
CONFIG_DEBUG_INFO=y
CONFIG_DEBUG_INFO_DWARF5=y
CONFIG_DEBUG_KERNEL=y
CONFIG_MAGIC_SYSRQ=y
```

### 2. Storage & seed

New table **`build_config_catalog`**:

| column        | type        | notes                                  |
|---------------|-------------|----------------------------------------|
| `name`        | text PK     | e.g. `kdump`                           |
| `object_key`  | text        | object-store key of the published bytes |
| `sha256`      | text        | content hash (binds repo ↔ object store) |
| `description` | text        | human label                            |

`_seed_build_configs(conn, store)`, called from `admin/bootstrap.py` alongside
`_seed_baseline_rootfs`: read `provisioning/configs/kdump.config`, compute sha256, and if the
stored row's `sha256` differs (or no row exists), write the bytes to a **fixed reserved key per
name** (`build-configs/kdump`) via the object-store client — not the project-scoped `artifacts`
table, whose owner-scoping and required TTL (ADR-0093) are the wrong lifecycle for a global,
non-expiring seeded input — then upsert the `name="kdump"` row with that `object_key`/`sha256`.
The fixed per-name key means an edited fragment **overwrites in place**: no orphaned object is
left behind (a content-addressed key would dedup but strand the prior object, and there is no
reconciler prune sweep for build configs as there is for rootfs in ADR-0092/0093). Idempotent:
re-seeding unchanged bytes is a no-op (the `sha256` gate skips the write).

### 3. Resolution & the build-flow change

- **Component sources.** `composition.py`: `CONFIG_COMPONENT: {"local", "catalog"}` for both
  providers. The MCP build tool's `reject_unsupported_component_source` then admits catalog config
  refs automatically.
- **Resolver.** Both `_resolve_config_ref` functions gain a `CatalogComponentRef` branch that
  fetches the entry's bytes via an injected fetch callback (parallel to the rootfs
  `catalog_fetch` in `materialize.py`), verifying sha256. Non-`local`/non-`catalog` kinds still
  raise `CONFIGURATION_ERROR`.
- **Implicit default — schema change.** `ServerBuildProfile.config` is today **required**
  (`config: ComponentRef`, `profiles/build.py:70`); an absent config is rejected at
  `BuildProfile.parse` time and never reaches a resolver. The default therefore makes the field
  optional (`ComponentRef | None`) and substitutes the `name="kdump"` catalog ref when it is
  `None`, at the build boundary. This touches the profile model, the MCP build-tool input schema,
  and profile (de)serialization — not only the provider resolver. Profiles that name a config are
  unaffected; this only admits omission. An explicit ref always overrides.
- **Build flow** (both providers, in checkout/staging):

  ```
  rsync warm tree
  make defconfig                          # base from the kernel tree
  merge_config.sh -m .config <fragment>   # MERGE ONLY — no internal olddefconfig
  make olddefconfig                       # the single resolution pass against this tree
  fragment-survival check                 # every requested fragment symbol present in FINAL .config
  _missing_config_groups(.config)         # existing preflight (CRASH_DUMP + debuginfo) on the merge
  make
  ```

  `_stage_config` is replaced by a `_merge_config` step that writes the fragment to a temp file and
  runs `defconfig` + `merge_config.sh -m`. The fragment bytes come from the resolved ref (catalog or
  local), not a fixed path.
- **Fragment-survival check.** `make olddefconfig` silently drops any fragment option whose
  dependency the base `defconfig` does not satisfy. The two-group preflight (`CONFIG_CRASH_DUMP` +
  debuginfo) does not cover the rest of the fragment, so a dropped `CONFIG_PROC_VMCORE` (etc.)
  would build a kernel that passes the build but cannot kdump. The check is therefore a **diff of
  the requested fragment symbols against the FINAL `.config`** (read after the single
  `olddefconfig`), not a parse of `merge_config.sh` output — `merge_config.sh` runs *before*
  `olddefconfig` here and **exits 0 even when a symbol is later dropped**, so its stdout warning is
  not authoritative and its exit code catches nothing. `merge_config.sh -m` skips its own internal
  `olddefconfig` so there is exactly one resolution pass and one authoritative final `.config`. Any
  requested symbol absent from that final `.config` fails the build. The preflight is a coarse gate;
  this check is what makes the *fragment* meaningful. Full kdump-correctness beyond symbol presence
  stays the live run's responsibility.

### 4. Agent retrieval

`buildconfig.get name="kdump"` read tool on the catalog MCP surface returns:

- `content`: the fragment bytes inline (≈500 B)
- `sha256`: the published hash
- `merge_recipe`: the same sequence the build uses, including the survival check, so an agent
  building on its own base gets the same protection against a silently dropped symbol:
  `make defconfig && scripts/kconfig/merge_config.sh -m .config kdump.config && make olddefconfig`,
  then **verify every `CONFIG_*` line in `kdump.config` is present in the final `.config`** (a
  dropped symbol means a dependency the agent's base does not satisfy — the kernel will not kdump).

Backed by the same object-store artifact as the build resolver, so a downloaded fragment and a
built-with fragment share a sha256. The recipe deliberately mirrors the build's `-m` + single
`olddefconfig` + survival-check sequence rather than the naive `merge_config.sh` that exits 0 on a
drop.

`buildconfig.get` is a **read-only** tool — `annotations=_docmeta.read_only()`, matching the other
catalog reads (`mcp/tools/catalog/artifacts.py`, `images.py`) — gated by the standard catalog-read
authorization. The fragment is non-sensitive (public `CONFIG_*` options), so no redaction applies
to its inline `content`.

## Components & seams

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `provisioning/configs/kdump.config` | Source-of-truth fragment | — |
| `build_config_catalog` table + migration | Durable name → object_key/sha256 | object store |
| `_seed_build_configs` (bootstrap) | Publish + upsert, idempotent | repo file, object store, DB |
| build-config catalog repository | `name` → bytes (sha256-verified) | DB, object store |
| `_resolve_config_ref` catalog branch (both providers) | Admit `catalog` refs + implicit default | catalog repository |
| `_merge_config` (both providers) | `defconfig` + `merge_config.sh -m` + single `olddefconfig` + fragment-survival check | kernel tree |
| `buildconfig.get` MCP tool | Inline agent download (recipe with survival check) | catalog repository |

Each unit has one purpose and a narrow interface; the **catalog repository** is the single seam
shared by the two build providers' `_resolve_config_ref` branches and the read tool, which is what
keeps "same bytes everywhere" true.

## Error handling

- Resolver: unknown `name` → `CONFIGURATION_ERROR` (`details` names the missing entry, never its
  bytes). sha256 mismatch on fetch → `INFRASTRUCTURE_FAILURE` (object store drifted from the row).
- Build: a non-zero `make defconfig` / `merge_config.sh -m` / `make olddefconfig` → `BUILD_FAILURE`.
  Note `merge_config.sh` exits 0 even when it drops a symbol, so a drop is **not** a non-zero exit —
  it is caught only by the fragment-survival check, which compares the requested symbols to the
  final `.config` and raises `CONFIGURATION_ERROR` whose `details` names the dropped symbol(s),
  never the fragment bytes. Merged result still missing a required preflight group → existing
  `CONFIGURATION_ERROR` with `missing_any_of`.
- Seed: object-store put failure → fail bootstrap loudly (a half-seeded catalog is worse than an
  absent one); the content-hash check makes a retried bootstrap safe.

## Testing strategy

Unit:
- `_missing_config_groups` on a real merged `defconfig`+fragment result (committed fixture).
- Fragment-survival check: a fragment symbol present in the final `.config` passes; a fragment
  symbol dropped by `olddefconfig` (simulated via a fixture where a dependency is absent) →
  `CONFIGURATION_ERROR` naming the dropped symbol.
- Resolver: catalog fetch returns bytes, sha256 verified; mismatch → `INFRASTRUCTURE_FAILURE`;
  unknown name → `CONFIGURATION_ERROR`.
- Seed idempotency: re-seed identical bytes = no put, no row change; edited bytes = new
  object_key + updated row.
- Both providers' `_resolve_config_ref`: accept `catalog`, accept `local`, reject other kinds;
  implicit default resolves `kdump` when config omitted.
- `buildconfig.get`: inline content + sha256 match the seeded artifact.

Integration (gated `live_vm`):
- The existing build fixtures switch from `/configs/kdump.config` to the catalog ref (or omission)
  and exercise `defconfig` + `merge_config.sh -m` against a real tree.

Acceptance gate (operator runbook, not CI): the four-method live run on the from-source System B —
`kdump`, `gdbstub`, `console`, plus `host_dump` — consistent with prior milestones whose real
hardware run is a runbook step.

## Decomposition

Suggested issue split (each independently shippable, guardrails green per commit):

1. **Fragment + seed + table + catalog repository.** `provisioning/configs/kdump.config`, migration
   for `build_config_catalog`, `_seed_build_configs`, the **build-config catalog repository**
   (`name` → sha256-verified bytes), unit tests. No build change yet; the repository is exercised by
   its own tests and the `buildconfig.get` tool (issue 3) until issue 2 wires it into the providers.
2. **Build-flow change, both providers.** `_merge_config` (`defconfig` + `merge_config.sh -m` +
   single `olddefconfig` + fragment-survival check), the **`_resolve_config_ref` catalog branch**
   (ref-kind dispatch calling the issue-1 repository), `composition.py` source sets, the
   implicit-default schema change (`ServerBuildProfile.config` → optional + default resolution;
   updates the MCP build-tool input schema), **and the integration-seed/unit-fixture config-ref
   update** (`tests/integration/_seed.py:71` and the build fixtures currently name
   `{kind: local, path: "/configs/kdump.config"}` — an explicit dead path that would override the
   new default and fail issue 2's own integration tests, so the switch to the catalog ref belongs
   here, not in issue 4). Replaces `_stage_config`. Preflight-on-merged tests.
3. **Agent download.** `buildconfig.get` MCP tool (recipe carries the survival check) +
   tool-doc/generated-doc regen.
4. **Runbook + residual cleanup.** Document the four-method live run on System B; remove any
   remaining `/configs/kdump.config` references outside the issue-2 build/seed paths (docs, plans).

Order: 1 → 2 (needs the issue-1 repository) → 3 (needs the seeded artifact) ‖ 4 (after 2). 3 and 4
are independent once 2 lands. The seed/fixture update lives in issue 2, so no window exists where
the integration build resolves a dead path.

## Open questions / follow-ups

- Final `CONFIG_*` set is pinned in issue 1 against a real `make olddefconfig` on the target
  kernel version (the spec list is indicative).
- Whether `gdbstub`/`console` need any cmdline (not build-config) provisioning is tracked
  separately; this milestone is build config only.
- An arch axis on `build_config_catalog` (composite `name`+`arch` key) is a clean future
  extension if an arch-specific kdump option ever appears.
