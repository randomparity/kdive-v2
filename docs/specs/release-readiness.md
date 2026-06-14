# Production-release readiness — design spec

Status: approved (brainstorming) · Date: 2026-06-14 · ADR: [ADR-0114](../adr/0114-production-release-readiness.md)

## Goal

Establish the documentation, host tooling, deployment recipes, and project governance for
KDIVE's first public, open-source release. The code is feature-complete (M2.x shipped); this
effort builds the *release surface* so a newcomer — human operator or coding agent — can adopt
KDIVE without reading the source.

## Scope

In scope:

1. Audience-tiered `docs/` restructure (incl. `specs/`→`design/` rename, archive of history)
2. Markdown link-check CI guardrail
3. Host preflight scripts for the local-libvirt and remote-libvirt providers
4. Deployment docs: docker-compose, kubernetes, systemd (incl. new systemd units)
5. Public-OSS governance + metadata: LICENSE (Apache-2.0), CONTRIBUTING, SECURITY,
   CODE_OF_CONDUCT, ARCHITECTURE, issue/PR templates, `pyproject.toml` metadata
6. Agent onboarding docs (`mcp_settings.json` / `.mcp.json`)
7. Fit & finish: README front door, `.gitignore` cruft sweep, CHANGELOG, `just ci` green

Out of scope: new product features; PyPI publishing (RELEASING.md lists it as a future
toggle); changing the release/versioning process (ADR-0041 stands); any provider code.

## Decisions (see ADR-0114)

- Tier `docs/` by reader; rename `specs/`→`design/`; archive working artifacts; preserve
  `adr/` and `guide/reference/` (generator + CI-gate dependencies).
- Link-check is a CI gate (`just docs-links` → `just ci` + `ci.yml`).
- Host preflight = standalone zero-state shell scripts (not a `doctor` subcommand).
- systemd: system-level units (dedicated user) default + `--user` variant.
- License Apache-2.0; full public-OSS file set.

## Target documentation tree

```
docs/
  README.md            # master index, audience-tiered (new)
  guide/               # USERS / AGENTS (kept in place)
    index.md  concepts.md  response-envelope.md  async-jobs.md
    safety-and-rbac.md  errors.md
    reference/         # generated tool + config reference (PATH PRESERVED)
    agents/            # NEW: agent onboarding (mcp config, smoke test)
  operating/           # OPERATORS (new home; absorbs runbooks/ + admin/)
    index.md
    install.md
    docker-compose.md
    kubernetes.md
    systemd.md
    providers/
      local-libvirt.md
      remote-libvirt.md
    runbooks/          # git mv from docs/runbooks/
  development/         # CONTRIBUTORS
    releasing.md       # git mv from docs/RELEASING.md
  design/              # CANONICAL design (git mv from docs/specs/)
    top-level-design.md  <milestone specs>  release-readiness.md
  adr/                 # CANONICAL decisions (name preserved)
  archive/             # NON-AUTHORITATIVE history (git mv)
    plans/  reports/  test-cases/  solutions/
    superpowers/{specs,plans}
```

### Rename blast radius (verified)

`docs/specs/` is referenced outside `docs/` by exactly three files —
`AGENTS.md`, `README.md`, `scripts/m2_portability_gate.py` (the error string at
`m2_portability_gate.py:382`). **Zero** ADRs reference `docs/specs/`. The generators
(`gen_tool_reference.py` `_REF_DIR`, `gen_config_reference.py` `_OUT`) and the
`docs-check`/`config-docs-check` recipes reference `docs/guide/reference/`, which is **not**
moving. Intra-`docs/` cross-links are verified by the new link-checker.

## Components

### Phase 0 — Restructure + link-check guardrail (lands first)

- `git mv` files into the target tree above; create `docs/README.md`, `docs/guide/index.md`
  updates, `docs/operating/index.md`.
- Update the three hardcoded `docs/specs/` refs to `docs/design/`.
- Add `scripts/check-doc-links.sh` (or a vendored checker) + `just docs-links`; wire into the
  `ci` recipe and `ci.yml`. The checker validates relative links in tracked `*.md` resolve to
  existing files/anchors.
- Verify `just docs-check`, `config-docs-check`, `check-mermaid` still pass (paths unchanged).

### Phase 1 — Host preflight scripts

Both report-only, never install/escalate; same style and `KDIVE_OS_RELEASE`-style test
harness as `scripts/check-setup-deps.sh`; surfaced as `just check-local-libvirt` /
`just check-remote-libvirt`.

- `scripts/check-local-libvirt.sh`: `/dev/kvm` present and accessible; `virtqemud` (or
  `libvirtd`) reachable; invoking user in the `libvirt` group; `virsh -c qemu:///system list`
  connects; default network active; `qemu-system-x86_64`/`virsh`/`qemu-img` present. Reports a
  per-distro remediation hint per failure.
- `scripts/check-remote-libvirt.sh`: generalizes `scripts/check-ssh-reachable.sh` — SSH
  reachability to the build/target host; remote `virsh -c <uri> list` over TLS; TLS PKI
  material present; guest-helper allowlist install state; port reachability. Inputs via flags
  or `KDIVE_*` env.

Cross-referenced from the service `doctor` docs (preflight = pre-deploy, doctor = post-deploy).

### Phase 2 — Deployment & systemd

- `docs/operating/docker-compose.md` — run via root `docker-compose.yml`; links existing
  `deploy/compose/README.md`.
- `docs/operating/kubernetes.md` — Helm install; links `deploy/helm/kdive/README.md` and the
  moved k8s runbook.
- `deploy/systemd/system/kdive-{server,worker,reconciler}.service` — dedicated `kdive` system
  user, `EnvironmentFile=/etc/kdive/kdive.env`, ordering/after on network + backends.
- `deploy/systemd/user/` — `--user` variant.
- `docs/operating/systemd.md` — install/enable/start, env file, logs (`journalctl`),
  validated with `systemd-analyze verify`.

### Phase 3 — Governance & metadata

- `LICENSE` (Apache-2.0, current text, correct copyright line).
- `pyproject.toml`: `license = "Apache-2.0"`, `authors`, `[project.urls]`
  (Homepage/Repository/Issues/Changelog).
- Root `CONTRIBUTING.md` (dev loop via `just`, branch/commit conventions, PR + CI gate,
  links to `development/releasing.md`), `SECURITY.md` (coordinated disclosure, supported
  versions), `CODE_OF_CONDUCT.md` (Contributor Covenant), `ARCHITECTURE.md` (concise; links
  `docs/design/top-level-design.md`).
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.yml`, `.github/PULL_REQUEST_TEMPLATE.md`.

### Phase 4 — Agent onboarding

`docs/guide/agents/index.md` + example config files: `mcp_settings.json` (Claude Code) and
`claude_desktop_config.json` / `.mcp.json` pointing at the streamable-HTTP endpoint, with
auth/OIDC token notes and a first-call smoke sequence (`investigations.create` →
`allocations.*` → `jobs.wait`).

### Phase 5 — Fit & finish

Rewrite root `README.md` as a concise front door routing to the three tiers; sweep `.live-*`
runtime cruft into `.gitignore`; add a CHANGELOG `[Unreleased]` entry; confirm `just ci`
(with the new link-check) is green.

## Error handling

- Preflight scripts: `set -euo pipefail`; nonzero exit only on *required* failures (mirror
  `check-setup-deps.sh` tiering: required vs recommended vs optional). Each failure prints
  what failed, why it matters, and a distro-specific fix.
- Link-checker: nonzero exit listing each broken link with source file and target.

## Testing

- New shell scripts: `shellcheck` + `shfmt -d`; behavior tests with synthetic
  `KDIVE_OS_RELEASE` / faked command probes, mirroring the existing `check-setup-deps`
  test approach.
- systemd units: `systemd-analyze verify`.
- Docs: `just docs-links`, `just docs-check`, `just config-docs-check`, `check-mermaid`.
- Whole effort: `just ci` green before any commit to the branch.

## Sequencing

Phase 0 is the hard prerequisite (new docs must be authored into the final tree). Phases 1–4
are independent and can fan out (each in its own external worktree if parallelized). Phase 5
closes out after 1–4 land. Each phase is a small, logically-scoped commit set (no squash).

## Risks

- A path move missed in a generator/recipe → CI breaks. Mitigated: `guide/reference/` is not
  moved; the link-checker + `just ci` run after Phase 0.
- License copyright line / SPDX correctness → reviewed in Phase 3.
- systemd unit assumptions about user/paths → validated with `systemd-analyze verify` and the
  install doc states prerequisites explicitly.
