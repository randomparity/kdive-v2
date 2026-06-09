# Plan — M2 issue 4: remote build (worker make + publish vmlinuz+modules bundle)

Issue: #203 · Spec: `docs/specs/m2-remote-libvirt.md` §issue 4 · Decision: ADR-0081.
Branch: `feat/remote-build-203`. Execution: direct TDD in-session (one tightly-coupled unit).

## Goal

Realize the `remote_libvirt` Build plane: run `make` on the worker (independently of
`local_libvirt`, ADR-0076), then publish a single gzip-compressed vmlinuz+modules install
bundle as `kernel_ref` and the `vmlinux` debuginfo as `debuginfo_ref`, recording the GNU
build-id — leaving `BuildOutput`, the `Builder` port, and the `runs` ledger unchanged.

## Acceptance (from the issue)

A build produces vmlinuz+modules and a referenced object-store key; build-id recorded for
later vmcore matching. Concretely: `RemoteLibvirtBuild.build(run_id, profile)` returns
`BuildOutput(kernel_ref=<bundle key>, debuginfo_ref=<vmlinux key>, build_id=<hex>)`, the
bundle object holds `boot/vmlinuz` + `lib/modules/<ver>/…` gzip-compressed, and both objects
are stored under the run-keyed layout.

## Guardrails (run before every commit)

`just lint` · `just type` · `just test` · `just m2-gate` · `just docs-check` · `just check-mermaid`.
Touch only `providers/` + `providers/composition.py` (allowlisted) + docs + tests; never core.

## Constraints carried from ADR-0081 / spec / review

- **No import from `local_libvirt`** (ADR-0076). Reuse only the neutral `provider_components.*`
  and `providers.build_validation` helpers. Duplicate the checkout/make/build-id seams.
- **Injected seams** (Callables defaulting to real impls), so unit tests run without a
  toolchain; real subprocess/ELF seams are `# pragma: no cover - live_vm`.
- **`make modules_install` non-zero → `BUILD_FAILURE`** (a fakeable seam, unit-tested).
- **Staging dir cleanup on every exit path** (the `INSTALL_MOD_PATH` tree).
- **kdump/debuginfo `.config` preflight** stays (remote does kdump too) → `CONFIGURATION_ERROR`.
- **Bundle is `.tar.gz`**; whole-object in-memory PUT (same model local uses).

## Tasks (TDD: failing test first, minimal impl, refactor green)

### T1 — `RemoteLibvirtBuild` orchestration + bundle (`providers/remote_libvirt/build.py`, new)

Mirror `LocalLibvirtBuild`'s injected-seam structure, independent. Seams:
`checkout`, `run_olddefconfig`, `read_config`, `run_make`, `run_modules_install(workspace, mod_root)`,
`build_bundle(workspace, mod_root) -> bytes` (gzip tar of vmlinuz + modules), `read_vmlinux`,
`read_build_id`. `build()` order: checkout → olddefconfig (≠0 → BUILD_FAILURE) → read_config →
preflight (kdump/debuginfo OR-groups → CONFIGURATION_ERROR) → profile_requirements validate →
make (≠0 → BUILD_FAILURE) → modules_install into a temp staging root (≠0 → BUILD_FAILURE) →
read_build_id → bundle = build_bundle(...) → put("kernel", bundle) → put("vmlinux", read_vmlinux)
→ return BuildOutput. Staging root created/removed in a `try/finally`.
`from_env(secret_registry)` wires real seams + `object_store_from_env`; must not spawn make
or connect S3.

Tests (`tests/providers/remote_libvirt/test_build.py`, new), fakes local to the file (no
local-libvirt import):
- happy path → kernel_ref/debuginfo_ref keys + build_id; both artifacts stored SENSITIVE,
  owner_kind="runs"; bundle bytes stored under "kernel".
- call order: checkout, olddefconfig, read_config, …, make, modules_install, bundle.
- olddefconfig ≠0 → BUILD_FAILURE, nothing stored.
- config missing kdump/debuginfo prereq (parametrized) → CONFIGURATION_ERROR, make not called.
- make ≠0 → BUILD_FAILURE.
- **modules_install ≠0 → BUILD_FAILURE**, nothing stored (the new edge).
- store failure on "kernel" → INFRASTRUCTURE_FAILURE propagates.
- **staging dir removed on success AND on a mid-build failure** (finally path).
- from_env does not spawn make / connect S3.
- real-seam argv: `_real_run_modules_install` passes `INSTALL_MOD_PATH=<mod_root>` and
  `-C <workspace>`; timeout → BUILD_FAILURE; missing make → MISSING_DEPENDENCY.
- `_real_build_bundle` produces a gzip tar whose members include `boot/vmlinuz` and a
  `lib/modules/...` entry (host-free: write a fake workspace+mod_root, assert tar membership
  via `tarfile`).

### T2 — Wire into composition (`providers/composition.py`)

Replace `UnimplementedBuilder()` in `build_remote_runtime` with
`RemoteLibvirtBuild.from_env(secret_registry=secret_registry)`. Drop the now-unused
`UnimplementedBuilder` import. Update `build.py`'s module docstring count if it enumerates planes.

### T3 — Remove the builder stub (`providers/remote_libvirt/planes.py`)

Delete `UnimplementedBuilder` (replace, don't deprecate). Update the module docstring that
lists which planes are still stubbed. Update `tests/providers/remote_libvirt/test_planes.py`
if it asserts the builder stub.

### T4 — Component sources + config validator (`providers/composition.py`)

**Required, not optional:** `runs.build` calls `reject_unsupported_component_source(
component_sources, CONFIG_COMPONENT, parsed.config)` and an empty accepted-set rejects every
source (`provider_components/validation.py:25-27`). With today's empty `_remote_component_sources`,
every remote server build would fail `CONFIGURATION_ERROR` before building. So:
- `_remote_component_sources` advertises `CONFIG_COMPONENT: {"local"}` and `PATCH_COMPONENT:
  {"local"}` (the remote build's local build-profile inputs; the warm-tree make stages a local
  `.config` and applies a local patch, exactly as local-libvirt's server build does).
- `build_remote_runtime` wires `build_config_validator=builder.validate_config_ref` so a config
  ref is validated within the remote build's component roots (mirror local; reuse the neutral
  `validate_local_component_path`). `RemoteLibvirtBuild` carries `allowed_component_roots`
  (from `KDIVE_BUILD_COMPONENT_ROOTS`, default `/var/lib/kdive/build/components`).

Test (`tests/providers/test_composition.py`): the remote runtime's `builder` is a
`RemoteLibvirtBuild`, its `build_config_validator` is not None, and `component_sources`
accepts `CONFIG`/`PATCH` as `{"local"}`. Confirm provisioning validation is unaffected (those
keys only gate build-profile components, which provisioning profiles never carry).

## Rollback / cleanup

Pure addition behind the opt-in remote runtime (no remote host configured in CI ⇒ runtime
not composed ⇒ no behavior change to default deployments). Revert = drop the build module,
restore `UnimplementedBuilder`. No migration, no data.

## Out of scope (later issues)

Presigned GET minting + in-guest pull/extract/install (issue 5); modules consumed there.
