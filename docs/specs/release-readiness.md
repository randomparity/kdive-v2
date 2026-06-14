# Production-release readiness — design spec

Status: approved (brainstorming) · Date: 2026-06-14 · ADR: [ADR-0114](../adr/0114-production-release-readiness.md)

## Goal

Establish the documentation, host tooling, deployment recipes, and project governance for
KDIVE's first public, open-source release. The code is feature-complete (M2.x shipped); this
effort builds the *release surface* so a newcomer — human operator or coding agent — can adopt
KDIVE without reading the source.

**Falsifiable acceptance signal:** a new operator reaches a running local-libvirt deployment
using only `operating/install.md` plus `just check-local-libvirt` — no source reading, no
tribal knowledge. (Navigation quality is otherwise qualitative; this is the one end-to-end
check that makes the goal testable.)

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
- Two CI guards (markdown link-check `just docs-links` + `docs/…` path-existence
  `just docs-paths`) → `just ci` + `ci.yml`.
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

### Move blast radius (verified)

The restructure relocates several directories, not just `specs/`; each relocation has its own
reference blast radius, and the markdown link-checker catches only the markdown-link column.
The non-markdown / code-span column is an explicit implementation checklist (also guarded by
the `docs-paths` check, below).

| Move | Non-markdown / code-span refs to fix | Markdown-link refs |
|------|--------------------------------------|--------------------|
| `specs/` → `design/` | `scripts/m2_portability_gate.py:382`; `AGENTS.md:14,137` (code spans) | `README.md` |
| `runbooks/` → `operating/runbooks/` | — | `AGENTS.md:151`, `README.md:68` |
| `RELEASING.md` → `development/releasing.md` | — | `AGENTS.md:138`, `README.md:106`; its own `../adr/…` links gain a level |
| `plans/` → `archive/plans/` | `AGENTS.md:15-16,137` (code spans) | `README.md:7` |
| `reports/` → `archive/reports/` | **`justfile:140` `m2-report` output path** — retarget the recipe | — |
| `superpowers/`, `test-cases/`, `solutions/`, `admin/` → new homes | — | intra-`docs/` links |

**Zero** ADRs reference `docs/specs/`. The generators (`gen_tool_reference.py` `_REF_DIR`,
`gen_config_reference.py` `_OUT`) and the `docs-check`/`config-docs-check` recipes reference
`docs/guide/reference/`, which is **not** moving.

## Components

### Phase 0 — Restructure + link-check guardrail (lands first)

- `git mv` files into the target tree above and create the **directory skeleton** only.
  Index pages that route to later-phase content (`docs/README.md` master index,
  `docs/operating/index.md`) are **not** authored here — they would forward-reference pages
  authored in Phases 2–4 and dangle under the Phase-0 link-checker. They are written in
  Phase 5 once their targets exist; Phase 0 may leave a one-line placeholder that links to
  nothing. `docs/guide/index.md` (its targets already exist) is updated here.
- Work the full move map above as a checklist — every relocated directory, not just `specs/`.
  Retarget `justfile:140` (`m2-report`) to the new reports location.
- Add **two** CI guards (the failure modes split across two surfaces):
  - `just docs-links` — markdown link-checker over tracked `*.md` (markdown cross-links only).
  - `just docs-paths` — path-existence check over **concrete** `docs/<path>` references in
    `justfile`, `scripts/`, `*.yml`, and `*.md` code spans (anchored `docs/<segment>/…`
    patterns, excluding the illustrative `docs/…`/`docs/...` ellipses); fails when a
    referenced target is missing. Catches the greppable non-markdown refs (`m2-report` at
    `justfile:140`, `m2_portability_gate.py`, `AGENTS.md` code spans). The generators'
    `_REF_DIR`/`_OUT` are slash-joined literals (no `docs/…` substring) and are **not** in
    `docs-paths` scope — they are covered by `docs-check`/`config-docs-check`, which run the
    generators and diff output.
  Wire both into the `ci` recipe and `ci.yml`.
- Verify `just docs-check`, `config-docs-check`, `check-mermaid` still pass (paths unchanged).

### Phase 1 — Host preflight scripts

Both report-only, never install/escalate; same style as `scripts/check-setup-deps.sh`;
surfaced as `just check-local-libvirt` / `just check-remote-libvirt`. Unlike `check-setup-deps`
(which only probes command *presence*), these assert *runtime state* (`/dev/kvm` access, whether
`virsh` connects, group membership, network-active), which a PATH probe cannot fake. So each
runtime check is a small overridable probe function (e.g. `_has_kvm`, `_virsh_connects`,
`_in_group`) that tests stub via env/function override — this testability seam is a Phase 1
design constraint, not an afterthought.

- `scripts/check-local-libvirt.sh`: `/dev/kvm` present and accessible; `virtqemud` (or
  `libvirtd`) reachable; invoking user in the `libvirt` group; `virsh -c qemu:///system list`
  connects; default network active; `qemu-system-x86_64`/`virsh`/`qemu-img` present. Reports a
  per-distro remediation hint per failure.
- `scripts/check-remote-libvirt.sh`: generalizes `scripts/check-ssh-reachable.sh` — SSH
  reachability to the build/target host; remote `virsh -c <uri> list` over TLS; TLS PKI
  material present; port reachability; and that the guest-helper files
  (`deploy/remote-libvirt-guest-helpers/*`) are **staged on the host for injection**. It does
  **not** inspect a provisioned guest — at host-preflight time (pre-deploy) no System exists,
  so in-guest helper verification belongs to runtime/`doctor`, not here. Inputs via flags or
  `KDIVE_*` env.

Cross-referenced from the service `doctor` docs (preflight = pre-deploy, doctor = post-deploy).

### Phase 2 — Deployment & systemd

- `docs/operating/install.md` — install paths (PyPI-future/source/container), host
  prerequisites (cites the Phase 1 preflight by recipe name, `just check-local-libvirt` /
  `just check-remote-libvirt`, to keep Phase 2 decoupled from Phase 1's files), and the three
  run modes below.
- `docs/operating/docker-compose.md` — run via root `docker-compose.yml`; links existing
  `deploy/compose/README.md`.
- `docs/operating/kubernetes.md` — Helm install; links `deploy/helm/kdive/README.md` and the
  moved k8s runbook.
- `deploy/systemd/system/kdive-{server,worker,reconciler}.service` — dedicated `kdive` system
  user, `EnvironmentFile=/etc/kdive/kdive.env`. The units **assume external, already-reachable
  backends** (Postgres, MinIO/S3, OIDC) via the env file; KDIVE does not manage them and they
  are commonly compose/k8s/managed, so the units cannot `Requires=` them. Contract instead:
  `After=network-online.target`, `Restart=on-failure` with a bounded `RestartSec`, so a process
  that starts before its backend is reachable retries rather than failing terminally.
- `deploy/systemd/user/` — `--user` variant.
- `docs/operating/systemd.md` — install/enable/start, the env file and **backend prerequisite**,
  that ordering against co-located backends is the operator's responsibility, logs
  (`journalctl`); units validated with `systemd-analyze verify`.

### Phase 3 — Governance & metadata

- `LICENSE` (Apache-2.0, current text, correct copyright line). Before it lands, confirm
  outbound Apache-2.0 is compatible with the resolved dependency tree and invoked tooling
  (`uv export` + a license scan): LGPL deps (`libvirt-python`, `psycopg`) are dynamically
  linked and GPL tools (`crash`, `gdb`, `drgn`) are invoked as separate processes — neither
  imposes copyleft on KDIVE's own source.
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

Author the index pages deferred from Phase 0 now that every target exists: `docs/README.md`
(master index, audience-tiered) and `docs/operating/index.md`. Rewrite root `README.md` as a
concise front door routing to the three tiers; sweep `.live-*` runtime cruft into `.gitignore`;
add a CHANGELOG `[Unreleased]` entry; confirm `just ci` (with both new doc gates) is green.

## Error handling

- Preflight scripts: `set -euo pipefail`; nonzero exit only on *required* failures (mirror
  `check-setup-deps.sh` tiering: required vs recommended vs optional). Each failure prints
  what failed, why it matters, and a distro-specific fix.
- Link-checker: nonzero exit listing each broken link with source file and target.

## Testing

- New shell scripts: `shellcheck` + `shfmt -d`; behavior tests that stub the per-check probe
  functions (and `KDIVE_OS_RELEASE` for distro hints) to exercise pass/fail/degraded paths
  without a real libvirt host — extending, not just mirroring, the `check-setup-deps` approach.
- systemd units: `systemd-analyze verify`.
- Docs: `just docs-links`, `just docs-paths`, `just docs-check`, `just config-docs-check`,
  `check-mermaid`.
- Whole effort: `just ci` green before each push / PR (not necessarily every intermediate
  commit, since a phase's first commit may precede its tests).

## Sequencing

Phase 0 is the hard prerequisite (new docs must be authored into the final tree). The
remaining dependency graph is **not** fully flat: Phase 2's `install.md` documents the
Phase 1 preflight, so **Phase 1 → Phase 2**. Phases 3 (governance) and 4 (agent onboarding)
are independent of 1/2 and each other, so they can fan out into their own external worktrees;
Phase 1 and Phase 2 either run in order or, to decouple them, `install.md` cites the preflight
by `just` recipe name (`just check-local-libvirt`) rather than linking the script files — a
recipe name is not a file the `docs-links` gate must resolve. Phase 5 closes out after 1–4
land. Each phase is a small, logically-scoped commit set (no squash).

## Risks

- A path move missed in a generator/recipe/code-span → silent rot. Mitigated: `guide/reference/`
  is not moved; the move map above is worked as a checklist; `just docs-paths` (non-markdown +
  code-span paths) and `just docs-links` (markdown links) both run in `just ci` after Phase 0.
- License copyright line / SPDX correctness → reviewed in Phase 3.
- systemd unit assumptions about user/paths → validated with `systemd-analyze verify` and the
  install doc states prerequisites explicitly.
