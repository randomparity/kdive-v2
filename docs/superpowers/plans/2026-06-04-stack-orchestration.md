# Stack orchestration + runbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `just stack-up` / `just test-live-stack` recipes, register the `live_stack` pytest marker with clean-skip plumbing, and write the operator runbook + `AGENTS.md` pointer for the M1.2 live stack.

**Architecture:** Ops/docs only — no `src/` changes. Reuse the existing `docker-compose.yml` backends; `stack-up` brings them up healthy (`docker compose up -d --wait`); `test-live-stack` runs `pytest -m live_stack` and treats pytest exit code 5 ("no tests collected") as a clean skip so the recipe is green before #100's marked driver lands. Env contract and skip mechanism are settled in `docs/superpowers/specs/2026-06-04-stack-orchestration-design.md`.

**Tech Stack:** `just` (rust-just), `bash -euo pipefail`, pytest 9, docker compose v2, shellcheck/shfmt.

---

## File structure

| file | change | responsibility |
|------|--------|----------------|
| `pyproject.toml` | modify | register the `live_stack` marker |
| `justfile` | modify | add `stack-up` + `test-live-stack` recipes |
| `docs/runbooks/live-stack.md` | create | operator bring-up + teardown runbook |
| `AGENTS.md` | modify | one-line pointer to the runbook + recipe-table rows |

The `test-live-stack` recipe holds the only non-trivial logic (exit-5 tolerance); it is a multi-line `bash` recipe linted by `shellcheck`/`shfmt`.

---

## Task 1: Register the `live_stack` marker

**Files:**
- Modify: `pyproject.toml:48-50` (the `[tool.pytest.ini_options]` `markers` list)

- [ ] **Step 1: Add the marker entry**

In `pyproject.toml`, extend the `markers` list so it reads:

```toml
markers = [
  "live_vm: requires an operator-provided libvirt/KVM environment (KVM/nested-virt host, libvirt, kdump-enabled guest image)",
  "live_stack: requires a running kdive stack (host server/worker/reconciler + Postgres/MinIO/OIDC backends) and a reachable OIDC issuer, beyond the live_vm KVM host",
]
```

- [ ] **Step 2: Verify the marker is registered (no warning)**

Run: `uv run python -m pytest -m live_stack --strict-markers -q`
Expected: exit 5, summary line `1298 deselected in …s`, **no** `PytestUnknownMarkWarning`. (`--strict-markers` would error on an unregistered marker referenced by a test; here it confirms the marker is known.)

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "test: register the live_stack pytest marker"
```

---

## Task 2: Add the `stack-up` and `test-live-stack` recipes

**Files:**
- Modify: `justfile` (insert after the `test-live` recipe, ~line 49, keeping recipe grouping)

- [ ] **Step 1: Add the recipes**

Insert after the `test-live` recipe in `justfile`:

```just
# Bring up the live-stack backing services (Postgres + MinIO + bucket + mock OIDC) and
# wait for them to be healthy, then print the host server/worker/reconciler env + next
# steps. Reuses the compose backends; the three host processes are started by the operator
# (see docs/runbooks/live-stack.md). Idempotent and re-runnable.
stack-up:
    docker compose up -d --wait
    @echo "Backends healthy. Export this env, then start the host processes:"
    @echo "  export KDIVE_DATABASE_URL=postgresql://kdive:kdive@localhost:5432/kdive"
    @echo "  export KDIVE_OIDC_ISSUER=http://localhost:8090/default"
    @echo "  export KDIVE_OIDC_JWKS_URI=http://localhost:8090/default/jwks"
    @echo "  export KDIVE_OIDC_AUDIENCE=kdive"
    @echo "  export KDIVE_S3_ENDPOINT_URL=http://localhost:9000"
    @echo "  export KDIVE_S3_BUCKET=kdive-artifacts KDIVE_S3_REGION=us-east-1"
    @echo "  export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin"
    @echo "Then in three terminals: python -m kdive server | worker | reconciler"
    @echo "Full runbook: docs/runbooks/live-stack.md"

# Run the live_stack suite (needs the stack from `just stack-up` + VM fixtures). Treats
# pytest exit 5 ("no tests collected" — e.g. the marked driver from #100 not yet present)
# as a clean skip; any other non-zero code propagates. --strict-markers makes a mis-marked
# test fail collection instead of silently deselecting.
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
```

- [ ] **Step 2: Verify the recipes are listed**

Run: `just --list`
Expected: `stack-up` and `test-live-stack` appear with their doc comments.

- [ ] **Step 3: Verify the clean skip with no stack/marked test**

Run: `just test-live-stack`
Expected: exit 0; output contains pytest's `… deselected` summary **and** the line `no live_stack tests collected — skipping cleanly …`.

- [ ] **Step 4: Lint the recipe shell**

Run: `just lint-shell` is scripts-only; lint the recipe body directly:
```bash
just --dump --dump-format just >/dev/null   # parse-check the justfile
```
Then extract-and-check the bash recipe is clean (shfmt 2-space, shellcheck SC clean). Since the recipe lives in the justfile, verify by reading it: `set -euo pipefail` present, `rc` quoted, no unquoted expansions.
Expected: justfile parses; no shellcheck-class issues in the recipe.

- [ ] **Step 5: Commit**

```bash
git add justfile
git commit -m "feat: add stack-up and test-live-stack recipes"
```

---

## Task 3: Write the operator runbook

**Files:**
- Create: `docs/runbooks/live-stack.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/live-stack.md` with: purpose; prerequisites (KVM host, libvirt, docker, pullable images — note the `mock-oauth2-server:3.1.4` tag may need re-pinning); the bring-up sequence (`just stack-up`, the env-export block, the VM fixture scripts `scripts/live-vm/build-guest-image.sh` + `fetch-kernel-tree.sh` setting `KDIVE_GUEST_IMAGE`/`KDIVE_KERNEL_SRC`, plus `KDIVE_STACK_BASE_URL`); starting the three host processes in separate terminals; running `just test-live-stack`; and teardown (`docker compose down -v`). Flag the `AWS_*`-credentials-for-MinIO step as the most error-prone. Use plain factual prose (no "robust"/"comprehensive"). Content per the spec's env-var contract table.

- [ ] **Step 2: Verify links and mermaid (if any) lint**

Run: `just check-mermaid` (no mermaid expected — confirms it still passes tree-wide).
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/live-stack.md
git commit -m "docs: add live-stack operator runbook"
```

---

## Task 4: Point AGENTS.md at the runbook and recipes

**Files:**
- Modify: `AGENTS.md` (the `just` recipe table ~line 25-32; the `live_vm` conventions bullet)

- [ ] **Step 1: Add recipe-table rows**

After the `just compose-up / compose-down` row in the `AGENTS.md` recipe table, add:

```markdown
| `just stack-up` | bring up the live-stack backends healthy + print host-process env (see runbook) |
| `just test-live-stack` | the `live_stack` suite; skips cleanly when the stack/fixtures are absent |
```

- [ ] **Step 2: Add the runbook pointer**

In the conventions section near the `live_vm` bullet, add a one-line pointer:

```markdown
- **Live-stack E2E** — operator bring-up and the `live_stack` suite are documented in
  [`docs/runbooks/live-stack.md`](docs/runbooks/live-stack.md) (ADR-0042).
```

- [ ] **Step 3: Verify markdown is intact (table not split)**

Run: `just check-mermaid`
Expected: PASS. Visually confirm the recipe table has no blank line inserted mid-table.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md
git commit -m "docs: point AGENTS.md at the live-stack runbook and recipes"
```

---

## Task 5: Full guardrail pass

**Files:** none (verification only)

- [ ] **Step 1: Run the guardrails**

Run:
```bash
just lint
just type
just test
```
Expected: all green. No `src/` changed, so `type`/`test` are unaffected but must stay green.

- [ ] **Step 2: Re-confirm the acceptance path**

Run: `just test-live-stack`
Expected: exit 0, `… deselected` + the clean-skip note.

- [ ] **Step 3: Commit (only if any fixup was needed)**

```bash
git add -A && git commit -m "chore: guardrail fixups"   # skip if nothing changed
```

---

## Self-review (spec coverage)

- One-command backend bring-up (`stack-up`, `--wait`) → Task 2. ✓
- `test-live-stack` runs `live_stack`, skips cleanly (exit-5 tolerance + output signal) → Task 2 Steps 1/3. ✓
- `live_stack` marker registered → Task 1. ✓
- `--strict-markers` guard against mis-marking → Tasks 1/2. ✓
- Env-var contract (incl. `AWS_*`-for-MinIO) → Task 2 echo block + Task 3 runbook. ✓
- Runbook (bring-up + fixtures + host processes + teardown) → Task 3. ✓
- `AGENTS.md` pointer → Task 4. ✓
- Stale-image risk flagged → Task 3 prerequisites. ✓
- Guardrails green at every commit → each task ends green; Task 5 is the final sweep. ✓

No spec section is unmapped. No `src/` task (none required).
