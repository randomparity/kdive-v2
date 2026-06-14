# ADR 0096 — Kdump kernel-config fragment as a seeded build-config catalog input

- **Status:** Proposed
- **Date:** 2026-06-11
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0092](0092-image-rootfs-lifecycle.md)
  (the seed-at-bootstrap + object-store-backed catalog pattern this extends from rootfs to
  build configs), [ADR-0081](0081-remote-build-kernel-bundle.md) (the remote worker `make` build whose
  config-staging step this rewrites), and [ADR-0065](0065-provider-component-references.md) (the
  component-reference kinds whose `local` config ref this replaces with a `catalog` ref).
- **Spec:** [`../superpowers/specs/2026-06-11-kdump-config-provisioning-design.md`](../archive/superpowers/specs/2026-06-11-kdump-config-provisioning-design.md)
- **Milestone:** kernel-build-config provisioning

## Context

The build → install → boot → debug loop is exercised end-to-end only for the `host_dump`
capture method (M2.5). The other three methods — `kdump`, `gdbstub`, `console` — run on a
System whose kernel is built from source, and that build has never succeeded outside a unit
fixture: the integration seed points its config ref at `/configs/kdump.config`, a `kind: local`
path that nothing provisions into the worker. There is no canonical kdump kernel config, no
artifact a build can resolve, and no file an operator or agent can retrieve.

Both providers' build code (`local_libvirt/build.py`, `remote_libvirt/build.py`) today stage a
**complete** `.config` by copying the resolved `local` ref to `workspace/.config`, then run
`make olddefconfig`. A complete `.config` is frozen to one kernel version — `olddefconfig` on a
newer tree silently drops or renames symbols — so it rots. The config-ref resolvers in both
providers hard-reject any ref that is not `LocalComponentRef`; `composition.py` declares
`CONFIG_COMPONENT: {"local"}`. The build's existing preflight (`_missing_config_groups`)
already checks for `CONFIG_CRASH_DUMP` plus DWARF/BTF debuginfo, but nothing supplies those
options — the check can only fail.

"Correctly configured for kdump" is a small, mostly arch-neutral set of `CONFIG_*` options. The
same set is correct for a locally built kernel and a remotely built one. The requirement is one
canonical fragment, one source of truth, resolved identically by the local build, the remote
build, and an agent that wants to apply kdive's kdump options to a kernel it builds itself.

## Decision

1. **Artifact form — fragment, not full `.config`.** Commit one `provisioning/configs/kdump.config`
   fragment (generic `CONFIG_*` lines only). The build merges it onto the kernel tree's own
   `make defconfig` via `scripts/kconfig/merge_config.sh`, then runs `make olddefconfig`. A
   fragment is version-portable (merges onto any base the tree produces), small, and reviewable.

2. **Distribution — one seeded object-store artifact.** A new `build_config_catalog` DB table
   (`name` PK, `object_key`, `sha256`, `description`) records published configs. At bootstrap,
   `_seed_build_configs()` content-hashes `provisioning/configs/kdump.config` and, if the stored
   `sha256` differs, writes the bytes to a **fixed reserved key per name** (e.g.
   `build-configs/kdump`) via the object-store client — not the project-scoped `artifacts` table,
   whose TTL/owner-scoping (ADR-0093) is the wrong lifecycle for a global seeded input — then
   upserts the `name="kdump"` row. A fixed per-name key means an edited fragment **overwrites in
   place**, leaving no orphaned object (the alternative, a content-addressed key, would dedup but
   strand the prior object with no prune path — and unlike rootfs in ADR-0092/0093 there is no
   reconciler sweep for these). The stored `sha256` still gates the write (unchanged bytes = no-op)
   and binds the row to the object. The table is the single runtime source of truth; the repo file
   is the build-time source of truth. A deployed cluster can serve the fragment over MCP without a
   filesystem copy that could drift.

3. **Resolution — catalog config refs, with an implicit default.** Add `"catalog"` to
   `CONFIG_COMPONENT` in both providers' component-source declarations and teach both
   `_resolve_config_ref` functions to fetch a `CatalogComponentRef` config from the catalog
   (mirroring the rootfs catalog fetch already wired in `materialize.py`). When a build profile
   names **no** config ref, the build resolves the canonical `name="kdump"` entry automatically;
   an explicit ref overrides. A kdump-capable kernel is the zero-config path.

   **In-scope schema change.** `ServerBuildProfile.config` is today a **required** field
   (`config: ComponentRef`, `profiles/build.py`), so an absent config is rejected at
   `BuildProfile.parse` time, never reaching a resolver. The implicit default therefore requires
   making the field optional (`ComponentRef | None`) and adding default-resolution at the build
   boundary that substitutes the `name="kdump"` catalog ref when the field is `None`. This touches
   the profile model, the MCP build-tool input schema, and profile (de)serialization — not just the
   provider resolver. Existing server profiles that name a config are unaffected (the field stays
   accepted); this only *admits* its omission. That blast radius is the price of the zero-config
   default; the rejected "explicit ref" alternative avoids it.

4. **Agent retrieval — inline MCP read tool.** A `buildconfig.get name="kdump"` read tool on the
   catalog surface returns the fragment bytes inline (≈500 B), its `sha256`, and a one-line
   `merge_config.sh` recipe. The same object-store artifact backs this tool and the build
   resolver, so the bytes an agent downloads are provably identical (matching sha256) to the
   bytes a build merges.

5. **Scope — one shared, arch-agnostic fragment.** A single `kdump` entry serves both providers
   and all arches. `merge_config.sh` tolerates a base lacking a symbol, so generic kdump options
   merge cleanly onto any arch's `defconfig`. An arch axis is added later only if a genuinely
   arch-specific kdump option appears.

## Consequences

- The build-flow change (`defconfig` → `merge_config.sh` → `olddefconfig`) lands symmetrically in
  both providers and is the one behavioral change to an existing seam. The preflight
  (`_missing_config_groups`) now validates a *merged* result instead of always failing — but it
  gates only its two OR-groups (`CONFIG_CRASH_DUMP` + DWARF/BTF debuginfo), **not** full
  kdump-correctness. The fragment carries more options (`KEXEC`, `PROC_VMCORE`, …) than the
  preflight checks, and `make olddefconfig` silently **drops** any fragment option whose dependency
  the base `defconfig` does not satisfy (the same tolerance noted in decision 5). A dropped
  non-preflight option (e.g. `PROC_VMCORE`) yields a kernel that passes the build but cannot kdump.
  The build therefore adds a **fragment-survival check** after the merge: every requested fragment
  symbol must appear in the final `.config` (`merge_config.sh` emits a "Value requested for X not in
  final .config" warning on a drop — the build fails on it). Full kdump-correctness beyond symbol
  presence remains the live run's responsibility (the acceptance gate below).
- The dead `/configs/kdump.config` references in the integration seed and fixtures are replaced
  with the catalog ref (or omission, relying on the default). This removes a phantom path.
- One new DB table and one migration; one new seed function alongside `_seed_baseline_rootfs`;
  one new MCP read tool. No change to `BuildOutput`, the `Builder` port, or the `runs` ledger.
- The implicit default changes the build contract: omitting a config ref was a
  `CONFIGURATION_ERROR`, and now resolves the `kdump` entry. Callers that relied on the error to
  catch a missing config no longer get it; this is intentional — the default is kdump-capable.
- Unblocks the from-source build itself (today it dies on the missing `/configs` path), which all
  three remaining methods need to produce a kernel at all. The kdump fragment is what lets that
  build pass its own kdump/debuginfo preflight; `gdbstub` additionally relies on the fragment's
  DWARF debuginfo options for symbolization, and `console` needs neither (it is a boot-cmdline
  concern). The four-method live run on real hardware is the milestone's acceptance gate (operator
  runbook, not CI).

## Alternatives considered

- **Full version-pinned `.config`.** Self-sufficient and no merge step, but frozen to one kernel
  version and large/unreviewable. Rejected: rots on a kernel bump, the exact failure mode the
  fragment avoids.
- **Filesystem fixture catalog (ADR-0065) baked into the worker image.** Simpler — no object
  store, no table — but a deployed cluster cannot serve a baked-in file to a remote agent over
  MCP, and the build copy and the served copy can drift. Rejected: violates the single-source
  requirement.
- **Cram configs into `image_catalog` or the `artifacts` table.** `image_catalog` is rootfs-shaped
  (`arch`, `format`, `root_device`); `artifacts` are project-private with a required TTL (ADR-0093).
  Both are the wrong lifecycle for a global, durable, seeded build input. Rejected in favor of a
  small purpose-built table.
- **Require an explicit config ref (no default).** Keeps the caller always seeing the applied
  config, but every build profile must name the kdump ref, and the common case (build a
  kdump-capable kernel) carries boilerplate. Rejected: the default makes the common case zero-config
  and an explicit ref still overrides.
- **Presigned-URL download instead of inline.** Consistent with vmcore/artifact retrieval, but a
  ≈500 B text fragment does not warrant a second round-trip; inline delivers bytes plus the merge
  recipe in one call. Rejected for this artifact size.
