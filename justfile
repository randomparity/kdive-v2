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

# Run the test suite, excluding the gated live_vm suite.
test:
    uv run python -m pytest -m "not live_vm" -q

# Run the live_vm suite (needs a KVM/libvirt host with a kdump-enabled guest).
test-live:
    uv run python -m pytest -m live_vm -q

# Bring up the live-stack backing services healthy, then print the host-process env + steps.
# Reuses the compose backends; the operator starts server/worker/reconciler (see the runbook
# docs/runbooks/live-stack.md). Idempotent and re-runnable.
stack-up:
    docker compose up -d --wait
    @echo "Backends healthy. Export this env, then start the host processes:"
    @echo "  export KDIVE_DATABASE_URL=postgresql://kdive:kdive@localhost:5432/kdive" # pragma: allowlist secret — local dev only
    @echo "  export KDIVE_OIDC_ISSUER=http://localhost:8090/default"
    @echo "  export KDIVE_OIDC_JWKS_URI=http://localhost:8090/default/jwks"
    @echo "  export KDIVE_OIDC_AUDIENCE=kdive"
    @echo "  export KDIVE_S3_ENDPOINT_URL=http://localhost:9000"
    @echo "  export KDIVE_S3_BUCKET=kdive-artifacts KDIVE_S3_REGION=us-east-1"
    @echo "  export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin"
    @echo "Then in three terminals: python -m kdive server | worker | reconciler"
    @echo "Full runbook: docs/runbooks/live-stack.md"

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

# Lint and format-check the shell scripts (recursively, including scripts/live-vm).
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

# Run the full gate that PR CI runs, reproducible locally.
ci: lint type lock-check lint-shell lint-workflows check-mermaid test
