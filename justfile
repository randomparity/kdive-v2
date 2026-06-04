set shell := ["bash", "-euo", "pipefail", "-c"]

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

# Type-check the package.
type:
    uv run ty check src

# Run the test suite, excluding the gated live_vm suite.
test:
    uv run python -m pytest -m "not live_vm" -q

# Run the live_vm suite (needs a KVM/libvirt host with a kdump-enabled guest).
test-live:
    uv run python -m pytest -m live_vm -q

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

# Run the full gate that PR CI runs, reproducible locally.
ci: lint type lint-shell lint-workflows check-mermaid test
