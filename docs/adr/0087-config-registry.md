# ADR 0087 — Central typed configuration registry (M2.1)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0012](0012-secret-backend.md) (secrets are
  refs, never material — the registry marks secret settings and routes them through the
  existing `SecretRegistry`), [ADR-0014](0014-structured-logging.md) (logging is configured
  first at process start, ahead of registry validation).
- **Spec:** [`../superpowers/specs/2026-06-10-m21-deployment-packaging-design.md`](../superpowers/specs/2026-06-10-m21-deployment-packaging-design.md)
- **Milestone:** #13 (M2.1)

## Context

The `KDIVE_*` configuration surface is not discoverable. Roughly 25 modules each call
`os.environ.get("KDIVE_…")` at their own point of use. There is no single source of truth,
no validation, and no generated reference. Consequences seen driving M2 on real hardware:

- A missing or malformed variable surfaces as a confusing downstream failure (a pool
  connect error, a provider registration that quietly does not happen), not a named
  configuration error pointing at the variable.
- There is no way to enumerate the contract — an operator cannot see which variables a given
  process needs, which are required, and which are secret references.
- Secret handling is decided per read site rather than once.

The M2.1 band scope calls for "one documented configuration surface (the `KDIVE_*` env
contract) with a generated config reference." A *generated* reference requires a single
declared source of truth, which does not exist yet.

## Decision

Introduce a central typed configuration registry, `kdive.config`, as the single declared
source of truth for the `KDIVE_*` contract. Point-of-use code reads from it instead of
`os.environ`. Startup validation and the generated reference both derive from it.

1. **`Setting` descriptor.** One declaration per variable: `name`, a `parse` callable,
   `default`, `required`, `secret: bool`, `processes` (the subset of the runnable commands
   `{server, worker, reconciler, migrate}` that consumes it), `group` (a logical category
   such as `database`, `objectstore`, `build`, `remote-libvirt`), and `help`.
   Build-toolchain settings are tagged `worker`/group `build` and validated when a build
   job runs, not at worker startup.

2. **Aggregation with provider co-location.** Core settings are declared in `kdive.config`.
   Provider and feature settings stay **co-located with their provider** — each module
   exposes a module-level `SETTINGS = [...]` that the registry aggregates at import. Core
   does not enumerate provider variables, so the portability hypothesis (a new provider
   needs no core-surface change) holds: a new provider declares its own settings and the
   registry picks them up. `providers/remote_libvirt/config.py` and the local-libvirt /
   fault-inject discovery modules migrate to this pattern.

3. **Read path.** `config.get(SETTING)` parses and caches once. An unparseable value raises
   a `configuration_error` (the existing `ErrorCategory`) naming the variable and the
   expected shape. No new error category.

4. **Startup validation.** Each process validates the settings required for its role before
   opening the pool or binding a port, and fails fast with the variable, the expected
   shape, and a suggested fix. This is the direct remedy for the band's "undiagnosed
   environment fault" pain.

5. **Generated reference.** `scripts/gen_config_reference.py` renders the registry to
   `docs/guide/reference/config.md` (alongside the generated tool reference), grouped by
   process and group, secret settings shown as ref-only. A drift test asserts the committed file matches generated output — the same
   pure-registry → markdown pattern as `scripts/gen_tool_reference.py`.

6. **Drift guard.** A meta-test fails if any `os.environ.get("KDIVE_…")` read exists
   outside `kdive.config` and the registered provider `SETTINGS` modules, so the source of
   truth cannot silently rot.

7. **Secret + redaction reuse.** `secret` settings feed the existing `SecretRegistry` /
   redaction path, so which variables are secret has one source of truth. The generated
   reference (decision 5) replaces the removed `print-local-env` crutch.

The registry **replaces** the scattered reads (replace-don't-deprecate): no shim, no dual
path. Once a variable is declared, its old `os.environ.get` site reads from the registry.

## Alternatives considered

- **Declarative catalog, lazy migration.** Keep the `os.environ` reads, add a separate
  catalog only for the reference and validation. Rejected: the catalog and the reads drift,
  and validation cannot be trusted to match what the code actually reads.
- **Doc-only generated reference.** Scan the source and emit a doc, no code change.
  Rejected: satisfies the literal wording but gives no startup validation and drifts the
  moment a variable is added.
- **A `pydantic-settings`-style monolithic settings object.** A single model holding every
  variable. Rejected: it re-couples core to every provider's variables, breaking the
  co-location that keeps the portability hypothesis intact, and forces all settings to load
  even for a process that does not use them.

## Consequences

- Adding a `KDIVE_*` variable means declaring a `Setting` (core or a provider `SETTINGS`
  list); the drift guard rejects a raw `os.environ` read, and the reference regenerates.
- A process that is misconfigured fails at start with a named error rather than a downstream
  symptom — the operability gain the band targets.
- The registry is a core surface touched by many modules. This is platform work, not
  provider work, so the M2 portability gate (zero core touches *by a provider*) is not
  implicated; provider modules change only to move their own settings into their `SETTINGS`
  list.
- The redaction path reads the registry's `secret` metadata, removing the separate
  per-site notion of which variables are secret.
