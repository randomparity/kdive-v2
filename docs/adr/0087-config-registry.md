# ADR 0087 — Central typed configuration registry (M2.1)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0012](0012-secret-backend.md) (secrets are
  refs, never material — the registry marks secret settings and routes them through the
  existing `SecretRegistry`), [ADR-0014](0014-structured-logging.md) (logging is configured
  first at process start, ahead of full registry validation — see the bootstrap-ordering
  note in decision 4).
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
   `default`, `secret: bool`, `processes` (the subset of the runnable commands
   `{server, worker, reconciler, migrate}` that consumes it), `group` (a logical category
   such as `database`, `objectstore`, `build`, `remote-libvirt`), `help`, and a `suggest`
   string (the actionable "suggested fix" surfaced on a validation failure — a field, not a
   generated guess).

   **Requiredness is conditional, not a flat bool.** A `required_when` predicate decides
   whether a setting must be present *given the rest of the resolved environment*, so the
   registry can express the patterns that already exist in the tree:
   - **Opt-in provider settings** (ADR-0076): `KDIVE_REMOTE_LIBVIRT_CA_CERT_REF` is required
     **iff** `KDIVE_REMOTE_LIBVIRT_URI` is set. An operator running only local-libvirt must
     not fail startup on a remote provider they never enabled; a half-configured remote
     provider must still be caught. A flat `required` cannot express either side of this.
   - **Build-toolchain settings** are tagged `worker`/group `build` with no startup
     requirement; they are validated when a build job runs (see decision 4), not at worker
     startup.

2. **Aggregation via an explicit module manifest.** Core settings are declared in
   `kdive.config`. Provider and feature settings stay **co-located with their provider** —
   each setting-bearing module exposes a module-level `SETTINGS = [...]`. Aggregation is
   **not** "whatever happened to import": the registry holds an explicit **manifest** of
   setting-bearing module paths and force-loads them on demand, so the full set is available
   regardless of which provider a given process enabled. This is load-bearing because
   providers are opt-in and lazily imported (`composition.py` registers remote-libvirt /
   fault-inject only when their enabling var is set; `__main__.py` imports app/provider
   modules inside functions) — without the manifest a `server` process would never import
   remote-libvirt, and the generated reference (decision 5) and drift guard (decision 6)
   would be incomplete and import-order-dependent.

   **Import direction (no cycle).** Setting-bearing modules import the registry's `Setting`
   type and nothing more; the registry imports the manifest's modules via the manifest, not
   by eagerly importing the provider packages — so the dependency is one-way
   (provider → registry types), and only the generator / validator / guard force-load the
   manifest.

   **Where the manifest lives, and the portability gate.** The manifest is a single list in
   the `kdive.config` package. `kdive/config/` is **not** among the M2 portability gate's
   `CORE_PREFIXES` (`domain/db/jobs/reconciler/services/store/security/mcp`,
   `scripts/m2_portability_gate.py`), so a new provider adding its module path to the
   manifest is **not** a gated core touch. It is a one-line addition **per provider module**,
   never per variable, and the provider's `SETTINGS` themselves live in the provider
   package. So the portability hypothesis holds in the sense the gate enforces it (no edit to
   the agnostic core), with the manifest as the explicit, ungated registration seam.
   `providers/remote_libvirt/config.py` and the local-libvirt / fault-inject discovery
   modules migrate to this pattern.

3. **Read path and caching.** `config.get(SETTING)` parses the value and raises a
   `configuration_error` (the existing `ErrorCategory`) naming the variable and the expected
   shape on a parse failure. No new error category. Resolution is **scoped, not a permanent
   process-global cache**: the registry resolves against a snapshot taken at process startup
   (or per validation call), and exposes a reset/override seam for tests. This matters
   because ~19 test files set `KDIVE_*` per case via `monkeypatch.setenv`; a permanent
   import-time cache would freeze the first-read value and produce order-dependent test
   failures. The seam also preserves the existing **deferred** provider reads (the
   remote-libvirt config is read at discovery/connection time so the runtime stays buildable
   without it, ADR-0076) — declaring a setting must not force an eager import-time read.

4. **Validation has two defined times.** (a) **Process startup** — before opening the pool
   or binding a port, each process validates the settings whose `required_when` holds for
   its role and the resolved environment, failing fast with the variable, the expected
   shape, and the setting's `suggest` fix. (b) **Provider/registration and job time** —
   opt-in provider settings are validated when the provider registers (it is registered only
   when its enabling variable is set), and build-toolchain settings when a build job runs.
   This keeps startup validation honest for opt-in providers without failing an operator on
   a provider they do not run. This is the direct remedy for the band's "undiagnosed
   environment fault" pain.

   **Configuration errors are not swallowed.** Provider registration is intentionally
   best-effort today — `__main__.py._register_provider_resources` catches and logs so a
   registration failure does not crash the reconciler. That catch is for *reachability*
   failures (the provider host is down), which stay best-effort. A *configuration* error (a
   `required_when` setting that is missing or malformed for a provider the operator **did**
   enable) is a distinct `configuration_error` that must surface loudly, not be logged and
   swallowed — otherwise this ADR reproduces the very "provider registration that quietly
   does not happen" it exists to fix. The best-effort catch is narrowed to re-raise (or
   separately report) `configuration_error`; the process that enabled the provider owns
   surfacing it.

   **Bootstrap ordering.** `KDIVE_LOG_LEVEL` is a registry `Setting` like any other, but
   logging is configured before full validation runs (ADR-0014), so it is resolved in an
   explicit early bootstrap phase through `config.get` — not a raw `os.environ` read — so the
   drift guard's allowlist (decision 6) need not carve out a logging exception.

5. **Generated reference.** `scripts/gen_config_reference.py` renders the registry to
   `docs/guide/reference/config.md` (alongside the generated tool reference), grouped by
   process and group, secret settings shown as ref-only. A drift test asserts the committed file matches generated output — the same
   pure-registry → markdown pattern as `scripts/gen_tool_reference.py`.

6. **Drift guard.** A meta-test fails if any process-environment read of a `KDIVE_*`
   variable exists outside `kdive.config` and the manifest's setting-bearing modules
   (decision 2).
   The guard is an **ast-grep rule over the access form**, not a string match on one literal:
   it catches `os.environ.get(...)`, the subscript form `os.environ[...]` (which exists today
   at `admin/bootstrap.py` for `KDIVE_DATABASE_URL`), and `os.getenv(...)`. It carries an
   explicit allowlist of the registry's own internal resolution site (which is also how the
   logging-bootstrap read in decision 4 stays inside the guard). The current tree has no
   dynamic prefix scans or env iteration; if one is ever
   introduced it must go through a registered `Setting`, which the guard enforces.

   **Activation is atomic with the migration.** The guard cannot be enabled while any read is
   still un-migrated, and "no shim, no dual path" means there is no transitional state to lean
   on. So either the guard lands in the same change as the last migrated read, or it lands
   early with a **shrinking allowlist** of not-yet-migrated files that must reach empty before
   issue 1 closes — never a half-migrated tree with the guard silently off.

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

- Adding a `KDIVE_*` variable means declaring a `Setting` (in core or a setting-bearing
  module already on the manifest; a brand-new module is added to the manifest); the drift
  guard rejects a raw `os.environ` read, and the reference regenerates.
- A process that is misconfigured fails at start with a named error rather than a downstream
  symptom — the operability gain the band targets.
- The registry is a shared-infra surface touched by many modules. This is platform work,
  not provider work. The M2 portability gate watches the agnostic-core prefixes
  (`domain/db/jobs/reconciler/services/store/security/mcp`); `kdive/config/` is not among
  them, so neither the manifest nor a provider's `SETTINGS` migration trips the gate. A new
  provider's only registry-side change is one manifest line (per module, not per variable)
  plus its own co-located `SETTINGS` — no edit to the agnostic core.
- The redaction path reads the registry's `secret` metadata, removing the separate
  per-site notion of which variables are secret.
