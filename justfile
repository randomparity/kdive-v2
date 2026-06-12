set shell := ["bash", "-euo", "pipefail", "-c"]

# Pinned git-cliff version — referenced by the changelog recipe and release.yml (one place).
GIT_CLIFF := "git-cliff@2.13.1"

# List available recipes.
default:
    @just --list

# One-command first-time setup: check host deps, sync the venv, install hooks.
setup: check-deps sync install-hooks
    @echo "Development environment is ready."

# Report missing host packages with distro-specific install hints (never installs).
check-deps:
    ./scripts/check-setup-deps.sh

# Create the venv and install pinned dependencies from the lockfile.
sync:
    uv sync --locked

# Install the git pre-commit hooks and run them across the tree once.
install-hooks:
    prek install
    prek run -a

# Lint and check formatting (read-only; mirrors CI).
lint:
    uv run ruff check .
    uv run ruff format --check .

# Apply lint fixes and reformat in place.
format:
    uv run ruff check --fix .
    uv run ruff format .

# Type-check the whole tree (src + tests). Whole-tree, not `src`: this is the single
# definition CI and the pre-commit ty hook both invoke, and the only place tests/ is
# type-checked (scoping to src once let a test-tree type error merge green).
type:
    uv run ty check

# Run the test suite, excluding the gated live_vm and live_stack suites.
# (oidc_issuer-marked tests stay selected; they skip cleanly without the issuer container.)
test:
    uv run python -m pytest -m "not live_vm and not live_stack" -q

# Run the live_vm suite (needs a KVM/libvirt host with a kdump-enabled guest).
test-live:
    uv run python -m pytest -m live_vm -q

# Apply database migrations using the live-stack default environment.
stack-migrate:
    ./scripts/live-stack/apply-migrations.sh

# Bring up the live-stack backing services healthy, then migrate the schema and print the
# host-process startup step. Reuses the compose backends; host processes stay outside compose.
#
# `--wait` is scoped to the three long-running backends: it treats ANY container exit as a wait
# failure, so the one-shot `minio-init` (creates the bucket, then exits 0) would make a healthy
# stack report exit 1. Run that init separately to completion — its exit code still propagates,
# so a real bucket-creation failure fails the recipe.
stack-up:
    docker compose up -d --wait postgres minio oidc
    docker compose run --rm minio-init
    ./scripts/live-stack/apply-migrations.sh
    @echo "Backends healthy and schema migrated."
    @echo "Start the app tier with: docker compose up -d migrate server worker reconciler"
    @echo "(or, for a source checkout of the local-libvirt host path: just stack-start)"
    @echo "MCP URL: http://127.0.0.1:8000/mcp"
    @echo "Full runbook: docs/runbooks/live-stack.md"

stack-start:
    ./scripts/live-stack/start.sh

stack-start-daemon:
    ./scripts/live-stack/start.sh --daemon

stack-stop:
    ./scripts/live-stack/stop.sh

# Run the live_stack suite (needs `just stack-up` + VM fixtures). --strict-markers fails a
# mis-marked test instead of silently deselecting; pytest exit 5 ("no tests collected", e.g.
# the marked driver not yet present) is tolerated as a clean skip, other codes propagate.
test-live-stack:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    uv run python -m pytest -m live_stack --strict-markers -q || rc=$?
    if [[ "$rc" -eq 5 ]]; then
      echo "no live_stack tests collected — skipping cleanly (stack/fixtures or marked suite absent)"
      exit 0
    fi
    exit "$rc"

# Build wheel + sdist with build info baked in, then remove the stamp so it never lingers
# in the editable checkout (a leftover would shadow live-git version reporting). Pass
# release=true only when building from a release tag.
build release="false":
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'rm -f src/kdive/_buildinfo.py' EXIT
    ./scripts/stamp-buildinfo.sh "{{release}}"
    uv build

# Regenerate CHANGELOG.md from conventional-commit history (Keep a Changelog).
changelog:
    uvx {{GIT_CLIFF}} --output CHANGELOG.md

# Start the operator backing services (Postgres + MinIO + mock OIDC) for a live run.
compose-up:
    docker compose up -d

# Stop the operator backing services and remove their volumes.
compose-down:
    docker compose down -v

# Lint and format-check the shell scripts (recursively under scripts/).
lint-shell:
    shfmt -f scripts | xargs shellcheck
    shfmt -i 2 -d scripts

# Lint and security-scan the GitHub Actions workflows.
lint-workflows:
    uv run --with 'zizmor==1.25.2' zizmor .github/workflows
    uv run --with 'actionlint-py==1.7.12.24' actionlint

# Browserless syntax check of every mermaid block in tracked Markdown.
# -z/-0 keeps paths with spaces intact; -r skips the run when nothing matches.
check-mermaid:
    git ls-files -z '*.md' | xargs -0 -r node .github/scripts/mermaid-check/mermaid-check.mjs

# M2 portability gate: cumulative core-touch measurement vs the pre-M2 tag (ADR-0076).
# Stdlib-only (plain python3, no uv sync); needs the pre-M2 tag fetched.
m2-gate:
    python3 scripts/m2_portability_gate.py

# Regenerate the committed milestone-end M2 portability report (ADR-0076).
m2-report:
    python3 scripts/m2_portability_gate.py --report > docs/reports/m2-portability.md

# Audit runtime dependencies for known vulnerabilities.
audit:
    reqs="$(mktemp)" && trap 'rm -f "$reqs"' EXIT && uv export --no-emit-project --no-dev --no-default-groups --format requirements-txt > "$reqs" && uv run --with 'pip-audit==2.10.0' pip-audit --no-deps --strict -r "$reqs"

# Set the project version in pyproject.toml AND uv.lock together. `--no-sync` re-locks
# (updates uv.lock) WITHOUT rebuilding the virtual environment — so a version bump does not
# require libvirt-dev to compile libvirt-python; the editable install refreshes on the next
# `uv run`. Used at a Milestone start and for the post-release "begin <next>-dev" bump.
# Commit the result on a branch — never directly on main.
set-version VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{VERSION}}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
      echo "VERSION must be MAJOR.MINOR.PATCH, got '{{VERSION}}'" >&2
      exit 1
    fi
    uv version --no-sync "{{VERSION}}"
    # Keep the Helm chart's appVersion locked to the pyproject version (spec A3 /
    # chart-version-check). Done here so a version bump never trips the CI guard.
    sed -i.bak -E 's/^appVersion:.*/appVersion: "{{VERSION}}"/' deploy/helm/kdive/Chart.yaml
    rm -f deploy/helm/kdive/Chart.yaml.bak
    echo "Set version to {{VERSION}} (pyproject.toml + uv.lock). Commit on a branch."

# Fail if uv.lock is out of date relative to pyproject.toml (a forgotten re-lock).
lock-check:
    uv lock --check

# Cut a release: verify state, then push the annotated tag only (never a commit to main).
# The version must already equal VERSION (it was bumped at Milestone start / post-release).
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ "$(git branch --show-current)" == "main" ]] || { echo "not on main" >&2; exit 1; }
    [[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
    git fetch --quiet origin main
    [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || { echo "HEAD is not at origin/main (behind, ahead, or diverged) — sync first" >&2; exit 1; }
    current="$(uv version --short)"
    [[ "$current" == "{{VERSION}}" ]] || { echo "pyproject version $current != {{VERSION}}" >&2; exit 1; }
    git tag -a "v{{VERSION}}" -m "Release v{{VERSION}}"
    git push origin "v{{VERSION}}"
    echo "Pushed tag v{{VERSION}}. NEXT: open a 'chore(release): begin <next>-dev' PR"
    echo "(just set-version <next>; just changelog) — see docs/RELEASING.md."

# Regenerate the agent-facing tool reference from the live registry (mutating).
docs:
    uv run python scripts/gen_tool_reference.py

# Verify the committed tool reference matches a fresh generation (CI gate).
docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    uv run python -c "from scripts.gen_tool_reference import write_reference; from pathlib import Path; write_reference(Path('$tmp'))"
    # config.md is generated separately (just config-docs-check); exclude it from the
    # tool-reference directory diff so the two generators can share docs/guide/reference/.
    if ! diff -ru --exclude=config.md docs/guide/reference "$tmp"; then
        echo "tool reference is stale — run 'just docs' and commit" >&2
        exit 1
    fi

# Regenerate the committed config reference from the registry (mutating).
config-docs:
    uv run python scripts/gen_config_reference.py

# Verify the committed config reference matches a fresh generation (CI gate).
config-docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    uv run python -c "from pathlib import Path; from scripts.gen_config_reference import write_reference; write_reference(Path('$tmp'))"
    if ! diff -u docs/guide/reference/config.md "$tmp"; then
        echo "config reference is stale — run 'just config-docs' and commit" >&2
        exit 1
    fi

# Structural guard: no KDIVE_* env read outside kdive.config (ADR-0087). Stdlib-only.
config-guard:
    uv run python scripts/config_env_guard.py

# Assert the Helm chart's appVersion tracks the pyproject version (spec A3). A drift
# would let a cut release point the chart's default image tag at a tag that was never
# published. Run in CI and `just ci`.
chart-version-check:
    #!/usr/bin/env bash
    set -euo pipefail
    pyproject="$(uv version --short)"
    chart="$(grep -E '^appVersion:' deploy/helm/kdive/Chart.yaml | sed -E 's/^appVersion:[[:space:]]*"?([^"]+)"?[[:space:]]*$/\1/')"
    if [[ "$chart" != "$pyproject" ]]; then
        echo "::error::Chart.yaml appVersion ($chart) != pyproject version ($pyproject)." >&2
        echo "Run 'just set-version $pyproject' or align Chart.yaml appVersion." >&2
        exit 1
    fi
    echo "appVersion == pyproject == $pyproject"

# Run the full gate that PR CI runs, reproducible locally.
ci: lint type lock-check lint-shell lint-workflows check-mermaid docs-check config-docs-check config-guard chart-version-check test
