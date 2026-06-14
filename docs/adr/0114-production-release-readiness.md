# ADR-0114: Production-release readiness — docs structure, host preflight, packaging

- Status: Proposed
- Date: 2026-06-14
- Deciders: maintainers
- Supersedes: none
- Related: [ADR-0041](0041-versioning-release-process.md) (versioning/release),
  [ADR-0047](0047-agent-facing-tool-guide-generation.md) (generated tool reference),
  [ADR-0087](0087-config-registry.md) (config registry / generated config reference),
  [ADR-0090](0090-opentelemetry-adoption-service-health.md) (service health / doctor)

## Context

KDIVE has reached feature completeness for a first public, open-source release (M2.x
shipped). The code, tests, CI gates, Helm chart, and a generated tool/config reference
already exist. What is missing is the *release surface*: the documentation, host tooling,
deployment recipes, and project governance a newcomer (human operator or coding agent)
needs to adopt the project without reading the source.

The documentation that does exist has accreted by milestone rather than by reader. The
`docs/` tree mixes reader-facing material (`guide/`), canonical design (`specs/`, `adr/`),
operator runbooks (`runbooks/`, `admin/`), and a large volume of historical working
artifacts (`superpowers/specs` ~73, `superpowers/plans` ~93, `plans/`, `reports/`,
`test-cases/`, `solutions/`). A first-time reader cannot tell authoritative from historical.

Several concerns are coupled to code, not just prose:

- The two doc generators hardcode `docs/guide/reference/` (`gen_tool_reference.py` `_REF_DIR`,
  `gen_config_reference.py` `_OUT`), and `just docs-check` / `config-docs-check` are PR gates
  that `diff` against that exact path. Moving it breaks CI silently.
- `AGENTS.md`, `README.md`, and `scripts/m2_portability_gate.py` hardcode `docs/specs/` paths.
- Nothing in CI checks intra-doc links, so any reorganization rots cross-references with no
  signal.

## Decision

### 1. Audience-tiered documentation tree

Re-tier `docs/` by reader, archive historical working artifacts, and add top-level entry
points. Rename `docs/specs/` → `docs/design/` (the canonical design tier). Preserve
`docs/adr/` and `docs/guide/reference/` names: `adr/` is cross-referenced by ~100 sibling
ADRs, and `guide/reference/` is hardcoded in the doc generators and the `docs-check` CI
gate. Target tree:

```
docs/
  README.md            # master index, audience-tiered (new)
  guide/               # users / agents (kept; generators depend on guide/reference/)
    reference/         # generated tool + config reference (path preserved)
    agents/            # agent onboarding (mcp config) (new)
  operating/           # operators: install, compose, k8s, systemd, providers, runbooks
  development/          # contributors: releasing.md (moved from docs/RELEASING.md)
  design/              # canonical design (renamed from specs/)
  adr/                 # canonical decisions (name preserved)
  archive/             # non-authoritative history (git mv; plans, reports, test-cases,
                       #   solutions, superpowers)
```

All moves use `git mv` to preserve history. The `specs/`→`design/` rename updates exactly
three non-doc files (`AGENTS.md`, `README.md`, `scripts/m2_portability_gate.py`) plus
intra-doc links, which the link-checker (decision 2) verifies.

### 2. Markdown link-check is a CI gate

Add `just docs-links` (a link-checker over tracked `*.md`) and wire it into `just ci` and
`ci.yml`. Rationale: a one-time restructure without an enforcement guardrail begins rotting
immediately. The checker is the durable mechanism that keeps the new structure honest and
makes future moves safe.

### 3. Host provider readiness is delivered as standalone zero-state shell scripts

Host preflight (`scripts/check-local-libvirt.sh`, `scripts/check-remote-libvirt.sh`) is
shell, not a `kdivectl`/`doctor` subcommand. Preflight runs *before* deployment — often
before the Python environment exists — so it must not depend on an installed venv. This is
the same lifecycle phase and report-only contract as the existing `scripts/check-setup-deps.sh`
(report, never install, never escalate). It is distinct from the service `doctor`
(ADR-0090), which diagnoses an *already-running* deployment. The two are cross-referenced so
operators know which to run when.

### 4. systemd packaging: system units default, user units variant

Ship `deploy/systemd/system/kdive-{server,worker,reconciler}.service` running as a dedicated
`kdive` system user, configured via `EnvironmentFile`, as the documented default for a
single-host operator. Also ship a `systemctl --user` variant for a developer/single-user
host. Both are validated with `systemd-analyze verify`.

### 5. License and public-OSS governance

License under **Apache-2.0** (permissive with an explicit patent grant — appropriate for an
infrastructure/MCP tool others embed). Add `LICENSE`, populate `pyproject.toml`
`license`/`authors`/`[project.urls]`, and add the public-OSS governance set: `CONTRIBUTING.md`,
`SECURITY.md` (coordinated-disclosure policy), `CODE_OF_CONDUCT.md` (Contributor Covenant),
`ARCHITECTURE.md` (concise, links `docs/design/top-level-design.md`), and
`.github/ISSUE_TEMPLATE/` + `PULL_REQUEST_TEMPLATE.md`.

## Consequences

- A newcomer can find the right doc by role; authoritative vs historical is unambiguous.
- The restructure is a partly-code change (generators, recipes, three hardcoded refs); it
  must land as one foundational phase before new docs are authored, or new content is written
  into a tree that then moves.
- CI gains a link-check gate; the new structure cannot silently rot.
- Host preflight closes the gap between "packages installed" (`check-setup-deps.sh`) and
  "provider can actually run," reducing first-run failures.
- The project carries the standard public-OSS file set and a clear license.

## Alternatives considered

- **Nav layer only / leave artifacts in place** — lowest risk, but leaves authoritative and
  historical docs intermixed; rejected in favor of a clean tier the release can stand on.
- **Preflight as a `doctor` subcommand** — richer output, but cannot run at true zero-state
  (no venv yet); rejected for the pre-deploy phase.
- **User-only or system-only systemd** — each excludes a real deployment shape; both are
  shipped.
- **MIT / AGPL-3.0** — MIT lacks an explicit patent grant; AGPL's network copyleft is
  heavier than wanted for an embeddable tool. Apache-2.0 chosen.
