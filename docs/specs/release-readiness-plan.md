# Production-release readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build KDIVE's first-public-release surface — audience-tiered docs, two CI doc-integrity guards, host preflight scripts, deployment + systemd recipes, Apache-2.0 + OSS governance, and agent onboarding — without changing any provider/runtime code.

**Architecture:** One foundational phase restructures `docs/` (audience tiers + archived history) and adds two CI guards that keep the structure honest; later phases author scripts/docs/governance into the clean tree. Spec: [release-readiness.md](release-readiness.md) · ADR: [ADR-0114](../adr/0114-production-release-readiness.md).

**Tech Stack:** `just` (task runner), bash (`set -euo pipefail`, shellcheck/shfmt), Python 3.13 + pytest (script tests via subprocess + PATH/env stubs), GitHub Actions, systemd units, Helm/docker-compose (referenced, not authored).

**Conventions every task follows:**
- Work happens on branch `feat/release-readiness` (already created off `origin/main`).
- Commit messages: Conventional Commits, ≤72-char subject, imperative, no squash. End each with the `Co-Authored-By` trailer this repo uses.
- `KDIVE_*` env reads must live in `kdive.config` — none of these scripts are Python service code, so the shell scripts read `KDIVE_*` directly; do **not** add `os.environ` reads to `src/`.
- This repo's CI **invokes justfile recipes individually** (`.github/workflows/ci.yml`), so adding a recipe to the `ci` aggregate is necessary but **not sufficient** — every new gate also needs an explicit `ci.yml` step.
- Whole-tree `just type` and `just lint` must stay green; new tests live under `tests/` mirroring source paths.

---

## File structure

**Phase 0 — restructure + guards**
- Move (git mv): `docs/specs/`→`docs/design/`, `docs/runbooks/`→`docs/operating/runbooks/`, `docs/admin/`→`docs/operating/` content, `docs/RELEASING.md`→`docs/development/releasing.md`, `docs/plans/ docs/reports/ docs/test-cases/ docs/solutions/ docs/superpowers/`→`docs/archive/…`.
- Create: `scripts/check-doc-links.sh`, `scripts/check-doc-paths.sh`, `tests/scripts/test_check_doc_links.py`, `tests/scripts/test_check_doc_paths.py`.
- Modify: `justfile` (add `docs-links`, `docs-paths`; extend `ci`; retarget `m2-report`), `.github/workflows/ci.yml` (two steps), `AGENTS.md`, `README.md`, `scripts/m2_portability_gate.py`.

**Phase 1 — preflight scripts**
- Create: `scripts/check-local-libvirt.sh`, `scripts/check-remote-libvirt.sh`, `tests/scripts/test_check_local_libvirt.py`, `tests/scripts/test_check_remote_libvirt.py`.
- Modify: `justfile` (add `check-local-libvirt`, `check-remote-libvirt`).

**Phase 2 — deployment & systemd**
- Create: `docs/operating/install.md`, `docker-compose.md`, `kubernetes.md`, `systemd.md`, `providers/local-libvirt.md`, `providers/remote-libvirt.md`; `deploy/systemd/system/kdive-{server,worker,reconciler}.service`, `deploy/systemd/user/kdive-{server,worker,reconciler}.service`, `deploy/systemd/kdive.env.example`, `deploy/systemd/README.md`; `tests/deploy/test_systemd_units.py`.

**Phase 3 — governance & metadata**
- Create: `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `ARCHITECTURE.md`, `.github/ISSUE_TEMPLATE/bug_report.yml`, `.github/ISSUE_TEMPLATE/feature_request.yml`, `.github/ISSUE_TEMPLATE/config.yml`, `.github/PULL_REQUEST_TEMPLATE.md`, `tests/test_project_metadata.py`.
- Modify: `pyproject.toml`.

**Phase 4 — agent onboarding**
- Create: `docs/guide/agents/index.md`, `docs/guide/agents/mcp.json` (example), `docs/guide/agents/claude_desktop_config.json` (example), `tests/docs/test_agent_config_examples.py`.

**Phase 5 — fit & finish**
- Create: `docs/README.md`, `docs/operating/index.md`.
- Modify: root `README.md`, `.gitignore`, `CHANGELOG.md`.

---

## Phase 0 — Restructure + doc-integrity guards (lands first)

> Phase 0 creates only the directory skeleton and moves; index pages that route to later-phase content are authored in Phase 5. Build the guards **first** (Tasks 0.1–0.2) so they can validate the moves (Task 0.3).

### Task 0.1: `docs-links` markdown link-checker

**Files:**
- Create: `scripts/check-doc-links.sh`
- Test: `tests/scripts/test_check_doc_links.py`
- Modify: `justfile`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_check_doc_links.py
"""Behavioral tests for scripts/check-doc-links.sh.

The checker resolves relative markdown links in tracked *.md files against the
filesystem. Tests build a tiny tree with a good and a broken link and assert the
exit status and that the broken target is named.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-doc-links.sh"
BASH = shutil.which("bash")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolvable_links_pass(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("see [b](b.md)\n")
    (tmp_path / "b.md").write_text("hi\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_broken_link_fails_and_names_target(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("see [gone](missing.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "missing.md" in result.stderr
    assert "a.md" in result.stderr


def test_external_and_anchor_only_links_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("[x](https://example.com) [y](#section)\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_links_inside_code_fences_ignored(tmp_path: Path) -> None:
    # A doc may show an example markdown link inside a code sample; that is not a real
    # cross-reference and must not be resolved. \x60 is the backtick byte; the three of
    # them form a fence without putting a literal fence marker in this test file.
    fence = "\x60\x60\x60"
    (tmp_path / "a.md").write_text(f"{fence}\nsee [gone](does-not-exist.md)\n{fence}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_check_doc_links.py -q`
Expected: FAIL (script does not exist / non-zero from missing file).

- [ ] **Step 3: Write the checker**

```bash
# scripts/check-doc-links.sh
#!/usr/bin/env bash
# Resolve relative markdown links in tracked *.md files against the filesystem.
# Reports only; exits 1 if any relative link target is missing. External (scheme://,
# mailto:) and pure-anchor (#...) links are ignored — only on-disk targets are checked.
# Usage: check-doc-links.sh [ROOT]   (ROOT defaults to the repo root / cwd)
set -euo pipefail

readonly ROOT="${1:-.}"
cd "${ROOT}"

# Collect markdown files: tracked files when in a git tree, else every *.md under ROOT
# (the test harness passes a non-git tmp dir).
mapfile -t files < <(git ls-files '*.md' 2>/dev/null || true)
if ((${#files[@]} == 0)); then
  mapfile -t files < <(find . -type f -name '*.md' -printf '%P\n')
fi

broken=0
for f in "${files[@]}"; do
  dir="$(dirname "$f")"
  # Extract [text](target) targets; drop the #fragment; one per line. Fenced code blocks
  # are stripped first (the awk toggles on triple-backtick fence lines; \140 is the octal
  # for a backtick, used so this script contains no literal fence marker) so illustrative
  # example links inside code samples are not treated as real cross-references.
  while IFS= read -r target; do
    [[ -z "$target" ]] && continue
    case "$target" in
    *"://"* | mailto:* | "#"*) continue ;;
    esac
    target="${target%%#*}"
    [[ -z "$target" ]] && continue
    if [[ ! -e "${dir}/${target}" ]]; then
      printf "broken link: %s -> %s\n" "$f" "$target" >&2
      broken=1
    fi
  done < <(awk 'BEGIN { fence = 0 } /^\140\140\140/ { fence = !fence; next } !fence' "$f" |
    grep -oE '\]\([^)]+\)' | sed -E 's/^\]\(//; s/\)$//')
done

if ((broken)); then
  printf "\nmarkdown link check failed\n" >&2
  exit 1
fi
printf "markdown links resolve\n"
```

- [ ] **Step 4: Run tests + lint the script**

Run: `uv run pytest tests/scripts/test_check_doc_links.py -q && shellcheck scripts/check-doc-links.sh && shfmt -i 2 -d scripts/check-doc-links.sh`
Expected: tests PASS; shellcheck/shfmt clean.

- [ ] **Step 5: Add the `docs-links` recipe**

In `justfile`, after the `check-mermaid` recipe (around line 131), add:

```make
# Resolve relative markdown links in tracked *.md against the filesystem.
docs-links:
    ./scripts/check-doc-links.sh
```

- [ ] **Step 6: Commit**

```bash
git add scripts/check-doc-links.sh tests/scripts/test_check_doc_links.py justfile
git commit -m "feat: add docs-links markdown link checker"
```

### Task 0.2: `docs-paths` path-existence checker

**Files:**
- Create: `scripts/check-doc-paths.sh`
- Test: `tests/scripts/test_check_doc_paths.py`
- Modify: `justfile`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_check_doc_paths.py
"""Behavioral tests for scripts/check-doc-paths.sh.

The checker greps justfile / scripts / *.yml / *.md code spans for concrete
docs/<seg>/... references and fails when the target is missing. Illustrative
ellipses (docs/... , docs/…) must NOT be flagged.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-doc-paths.sh"
BASH = shutil.which("bash")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), str(root)], capture_output=True, text=True, check=False
    )


def test_existing_path_passes(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide").mkdir()
    (tmp_path / "docs" / "guide" / "index.md").write_text("hi\n")
    (tmp_path / "justfile").write_text("x:\n\techo docs/guide/index.md\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_missing_path_fails(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("x:\n\techo docs/reports/m2-portability.md\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "docs/reports/m2-portability.md" in result.stderr


def test_illustrative_ellipsis_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("references `docs/<seg>/…` and `docs/...` are fine\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_design_and_archive_markdown_not_scanned(tmp_path: Path) -> None:
    # Design specs narrate path moves and archive is frozen history: their docs/...
    # mentions of missing/old paths must not fail the check.
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "archive").mkdir(parents=True)
    (tmp_path / "docs" / "design" / "spec.md").write_text("we move docs/specs to docs/design\n")
    (tmp_path / "docs" / "archive" / "old.md").write_text("see docs/plans/m0-implementation.md\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_paths_inside_code_fences_ignored(tmp_path: Path) -> None:
    # An operational doc may show an example command referencing a path in a code block;
    # that is a sample, not a live reference. \x60 is the backtick byte.
    fence = "\x60\x60\x60"
    (tmp_path / "a.md").write_text(f"{fence}\ncat docs/operating/not-yet.md\n{fence}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_check_doc_paths.py -q`
Expected: FAIL (script missing).

- [ ] **Step 3: Write the checker**

```bash
# scripts/check-doc-paths.sh
#!/usr/bin/env bash
# Fail when a concrete docs/<path> reference in justfile / scripts / *.yml / operational
# *.md points at a target that does not exist. Illustrative ellipses (docs/... and the
# unicode docs/…) and angle-bracket placeholders (docs/<seg>) are excluded. Catches
# non-markdown rot (e.g. justfile m2-report output, AGENTS.md code spans). NOT scanned:
#   - docs/design/** — design specs narrate path moves (docs/specs -> docs/design etc.),
#     so their docs/... mentions are intentional and must not be policed here;
#   - docs/archive/** — frozen history references paths as they were when written.
# Generator constants built from slash-joined string literals are also out of scope
# (covered by `just docs-check`/`config-docs-check`).
# Usage: check-doc-paths.sh [ROOT]
set -euo pipefail

readonly ROOT="${1:-.}"
cd "${ROOT}"

mapfile -t files < <(
  { git ls-files 'justfile' 'scripts/*' '*.yml' '*.yaml' '*.md' 2>/dev/null || true; } |
    grep -vE '^docs/(design|archive)/'
)
if ((${#files[@]} == 0)); then
  mapfile -t files < <(
    find . -type f \( -name justfile -o -path './scripts/*' -o -name '*.yml' \
      -o -name '*.yaml' -o -name '*.md' \) \
      -not -path './docs/design/*' -not -path './docs/archive/*' -printf '%P\n'
  )
fi

missing=0
for f in "${files[@]}"; do
  [[ -e "$f" ]] || continue
  # docs/ followed by path chars. Fenced code blocks are stripped first (the awk toggles on
  # triple-backtick fence lines; \140 is the octal for a backtick, so this script holds no
  # literal fence marker) so example paths in code samples are not policed; design/archive
  # trees are already excluded from the file set above.
  while IFS= read -r ref; do
    [[ -z "$ref" ]] && continue
    # Skip illustrative ellipses (ASCII ... or unicode …) and <placeholders>.
    case "$ref" in
    *"..."* | *"…"* | *"<"*) continue ;;
    esac
    ref="${ref%.}" # drop a trailing sentence period
    if [[ ! -e "$ref" ]]; then
      printf "missing doc path: %s references %s\n" "$f" "$ref" >&2
      missing=1
    fi
  done < <(awk 'BEGIN { fence = 0 } /^\140\140\140/ { fence = !fence; next } !fence' "$f" |
    grep -oE 'docs/[A-Za-z0-9._/-]+' | sort -u)
done

if ((missing)); then
  printf "\ndoc path-existence check failed\n" >&2
  exit 1
fi
printf "doc paths resolve\n"
```

- [ ] **Step 4: Run tests + lint**

Run: `uv run pytest tests/scripts/test_check_doc_paths.py -q && shellcheck scripts/check-doc-paths.sh && shfmt -i 2 -d scripts/check-doc-paths.sh`
Expected: tests PASS; lint clean.

- [ ] **Step 5: Add the `docs-paths` recipe**

In `justfile`, directly after the `docs-links` recipe, add:

```make
# Fail when a concrete docs/<path> reference in code/recipes/markdown is missing.
docs-paths:
    ./scripts/check-doc-paths.sh
```

- [ ] **Step 6: Commit**

```bash
git add scripts/check-doc-paths.sh tests/scripts/test_check_doc_paths.py justfile
git commit -m "feat: add docs-paths path-existence checker"
```

### Task 0.3: Move the tree and update references

**Files:**
- Move: see File structure (Phase 0)
- Modify: `AGENTS.md`, `README.md`, `scripts/m2_portability_gate.py`, `justfile`

- [ ] **Step 1: Create tiers and move with history preserved**

```bash
mkdir -p docs/operating docs/development docs/archive
git mv docs/specs docs/design
git mv docs/runbooks docs/operating/runbooks
git mv docs/admin/local-stack.md docs/operating/local-stack.md && git rm -r --quiet docs/admin 2>/dev/null || rmdir docs/admin 2>/dev/null || true
git mv docs/RELEASING.md docs/development/releasing.md
git mv docs/plans docs/archive/plans
git mv docs/reports docs/archive/reports
git mv docs/test-cases docs/archive/test-cases
git mv docs/solutions docs/archive/solutions
git mv docs/superpowers docs/archive/superpowers
```

- [ ] **Step 2: Fix `releasing.md` internal links (depth changed by one level)**

`docs/development/releasing.md` previously sat at `docs/`; its `adr/0041-...` links must become `../adr/0041-...`.
Run: `sed -i 's#(adr/#(../adr/#g' docs/development/releasing.md`
Then verify no double-prefix: `grep -n 'adr/' docs/development/releasing.md` — every link should be `../adr/...` exactly once.

- [ ] **Step 3: Update the three non-markdown / code-span references**

- `scripts/m2_portability_gate.py:382`: change `docs/specs/m2-remote-libvirt.md` → `docs/design/m2-remote-libvirt.md`.
- `AGENTS.md`: `docs/specs/top-level-design.md`→`docs/design/top-level-design.md`; `docs/plans/m0-implementation.md`→`docs/archive/plans/m0-implementation.md`; `docs/plans/m1-implementation.md`→`docs/archive/plans/m1-implementation.md`; the `docs/specs/, docs/plans/, and docs/superpowers/` enumeration → `docs/design/, docs/archive/plans/, and docs/archive/superpowers/`; `docs/runbooks/live-stack.md`→`docs/operating/runbooks/live-stack.md`; `docs/RELEASING.md`→`docs/development/releasing.md`.
- `README.md`: `docs/specs/top-level-design.md`→`docs/design/top-level-design.md`; `docs/plans/m0-implementation.md`/`m1-implementation.md`→`docs/archive/plans/...`; `docs/runbooks/live-stack.md`→`docs/operating/runbooks/live-stack.md`; `docs/RELEASING.md`→`docs/development/releasing.md`.

- [ ] **Step 4: Retarget the `m2-report` recipe**

In `justfile`, the `m2-report` recipe output path `docs/reports/m2-portability.md` → `docs/archive/reports/m2-portability.md`.

- [ ] **Step 5: Confirm generators still resolve (paths unchanged)**

Run: `just docs-check && just config-docs-check`
Expected: PASS (the generators write to `docs/guide/reference/`, which did not move).

- [ ] **Step 6: Run both new guards over the real tree**

Run: `just docs-links && just docs-paths`
Expected: PASS. If `docs-links` flags a moved intra-doc link, fix the link at the named source. If `docs-paths` flags a missed code-span/recipe ref, fix it. Re-run until both pass.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: re-tier docs by audience and archive working history"
```

### Task 0.4: Wire both guards into CI

**Files:**
- Modify: `justfile`, `.github/workflows/ci.yml`

- [ ] **Step 1: Add to the `ci` aggregate recipe**

In `justfile`, edit the `ci:` line to insert `docs-links docs-paths` after `check-mermaid`:

```make
ci: lint type lock-check lint-shell lint-workflows check-mermaid docs-links docs-paths docs-check config-docs-check config-guard chart-version-check test
```

- [ ] **Step 2: Add explicit CI steps (recipes run individually in CI)**

In `.github/workflows/ci.yml`, in the `lint-type-test` job, after the `Type check` step (line 64-65) and before `Docs reference up to date`, add:

```yaml
      - name: Doc links resolve
        # CI invokes recipes individually, so list this explicitly to gate PRs.
        run: just docs-links

      - name: Doc paths resolve
        run: just docs-paths
```

- [ ] **Step 3: Verify locally**

Run: `just docs-links && just docs-paths && actionlint .github/workflows/ci.yml`
Expected: both guards PASS; actionlint clean.

- [ ] **Step 4: Commit**

```bash
git add justfile .github/workflows/ci.yml
git commit -m "ci: gate PRs on docs-links and docs-paths"
```

---

## Phase 1 — Host preflight scripts

> Phase 1 → Phase 2 (Phase 2's `install.md` cites these by recipe name). Both scripts are report-only and route each runtime probe through a small override seam so tests can drive pass/fail without a real libvirt host.

### Task 1.1: `check-local-libvirt.sh`

**Files:**
- Create: `scripts/check-local-libvirt.sh`
- Test: `tests/scripts/test_check_local_libvirt.py`
- Modify: `justfile`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_check_local_libvirt.py
"""Behavioral tests for scripts/check-local-libvirt.sh.

Runtime state is faked via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override,
so the script's pass/fail paths run without a real libvirt host.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-local-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_all_healthy_exits_zero(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # virsh: any subcommand succeeds; `net-info default` reports Active: yes.
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "KDIVE_KVM_NODE": str(kvm)}
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout.lower()


def test_missing_kvm_node_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(tmp_path / "nope"),
    }
    result = _run(env)
    assert result.returncode == 1
    assert "kvm" in result.stderr.lower()


def test_user_not_in_libvirt_group_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo kvm wheel")  # no 'libvirt'
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "KDIVE_KVM_NODE": str(kvm)}
    result = _run(env)
    assert result.returncode == 1
    assert "libvirt" in result.stderr.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_check_local_libvirt.py -q`
Expected: FAIL (script missing).

- [ ] **Step 3: Write the script**

```bash
# scripts/check-local-libvirt.sh
#!/usr/bin/env bash
# Report whether this host can run the local-libvirt provider. Report-only: never
# installs, never escalates. Each runtime probe is a small function so tests can drive
# pass/fail via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override. Exit 1 if any
# required check fails. Run before deploying; the service `doctor` covers post-deploy.
set -euo pipefail

readonly KVM_NODE="${KDIVE_KVM_NODE:-/dev/kvm}"
fail=0

note_fail() {
  printf "FAIL: %s\n" "$1" >&2
  printf "  fix: %s\n" "$2" >&2
  fail=1
}

_has_kvm() { [[ -r "${KVM_NODE}" && -w "${KVM_NODE}" ]]; }
_cmd() { command -v "$1" >/dev/null 2>&1; }
_in_libvirt_group() { id -nG 2>/dev/null | tr ' ' '\n' | grep -qx libvirt; }
_virsh_connects() { virsh -c qemu:///system list >/dev/null 2>&1; }
_default_net_active() { virsh -c qemu:///system net-info default 2>/dev/null | grep -qi 'Active: *yes'; }

_has_kvm || note_fail "${KVM_NODE} not readable/writable (KVM unavailable)" \
  "enable virtualization in BIOS and load kvm modules; ensure your user can access ${KVM_NODE}"
for c in virsh qemu-system-x86_64 qemu-img; do
  _cmd "$c" || note_fail "$c not found on PATH" "install it via your distribution (see scripts/check-setup-deps.sh hints)"
done
_in_libvirt_group || note_fail "invoking user is not in the 'libvirt' group" \
  "sudo usermod -aG libvirt \"\$USER\" and re-login"
if _cmd virsh; then
  _virsh_connects || note_fail "cannot connect to qemu:///system" \
    "start the libvirt daemon: systemctl enable --now virtqemud.socket (or libvirtd)"
  _default_net_active || note_fail "libvirt 'default' network is not active" \
    "virsh -c qemu:///system net-start default && virsh -c qemu:///system net-autostart default"
fi

if ((fail)); then
  printf "\nlocal-libvirt host is NOT ready (see failures above)\n" >&2
  exit 1
fi
printf "local-libvirt host is ready\n"
```

- [ ] **Step 4: Run tests + lint**

Run: `uv run pytest tests/scripts/test_check_local_libvirt.py -q && shellcheck scripts/check-local-libvirt.sh && shfmt -i 2 -d scripts/check-local-libvirt.sh`
Expected: tests PASS; lint clean.

- [ ] **Step 5: Add the recipe**

In `justfile`, after `check-deps` (line ~16), add:

```make
# Preflight: can this host run the local-libvirt provider? (report-only)
check-local-libvirt:
    ./scripts/check-local-libvirt.sh
```

- [ ] **Step 6: Commit**

```bash
git add scripts/check-local-libvirt.sh tests/scripts/test_check_local_libvirt.py justfile
git commit -m "feat: add local-libvirt host preflight script"
```

### Task 1.2: `check-remote-libvirt.sh`

**Files:**
- Create: `scripts/check-remote-libvirt.sh`
- Test: `tests/scripts/test_check_remote_libvirt.py`
- Modify: `justfile`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_check_remote_libvirt.py
"""Behavioral tests for scripts/check-remote-libvirt.sh.

ssh / virsh are PATH-stubbed; TLS PKI and staged guest-helper checks use directory
overrides so the pre-deploy (no provisioned guest) path runs without real infra.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-remote-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def _healthy_env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "ssh", "exit 0")
    _stub(bindir, "virsh", "exit 0")
    pki = tmp_path / "pki"
    pki.mkdir()
    (pki / "clientcert.pem").write_text("x")
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "kdive-drgn").write_text("x")
    return {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_REMOTE_PKI_DIR": str(pki),
        "KDIVE_GUEST_HELPERS_DIR": str(helpers),
    }


def test_healthy_remote_exits_zero(tmp_path: Path) -> None:
    env = _healthy_env(tmp_path)
    result = _run(["host.example", "kdive", "qemu+tls://host.example/system"], env)
    assert result.returncode == 0, result.stderr


def test_unreachable_ssh_fails(tmp_path: Path) -> None:
    env = _healthy_env(tmp_path)
    _stub(Path(env["PATH"]), "ssh", "exit 255")
    result = _run(["host.example", "kdive", "qemu+tls://host.example/system"], env)
    assert result.returncode == 1
    assert "ssh" in result.stderr.lower()


def test_missing_pki_fails(tmp_path: Path) -> None:
    env = _healthy_env(tmp_path)
    env["KDIVE_REMOTE_PKI_DIR"] = str(tmp_path / "absent")
    result = _run(["host.example", "kdive", "qemu+tls://host.example/system"], env)
    assert result.returncode == 1
    assert "pki" in result.stderr.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_check_remote_libvirt.py -q`
Expected: FAIL (script missing).

- [ ] **Step 3: Write the script**

```bash
# scripts/check-remote-libvirt.sh
#!/usr/bin/env bash
# Report whether the remote-libvirt provider can reach a target host. Report-only:
# never installs, never escalates, opens no transport beyond a single ssh probe and a
# read-only `virsh list`. Pre-deploy: no System exists yet, so it checks only that the
# guest-helper FILES are staged on this host for injection — in-guest verification is
# the service `doctor`'s job, not this script's.
# Usage: check-remote-libvirt.sh HOST [USER] [URI]
# Env: KDIVE_REMOTE_SSH_PORT (default 22), KDIVE_REMOTE_PKI_DIR, KDIVE_GUEST_HELPERS_DIR
set -euo pipefail

readonly DEFAULT_USER="root"
readonly PORT="${KDIVE_REMOTE_SSH_PORT:-22}"
readonly PKI_DIR="${KDIVE_REMOTE_PKI_DIR:-/etc/pki/libvirt}"
readonly HELPERS_DIR="${KDIVE_GUEST_HELPERS_DIR:-deploy/remote-libvirt-guest-helpers}"

usage() {
  echo "usage: check-remote-libvirt.sh HOST [USER] [URI]" >&2
}

fail=0
note_fail() {
  printf "FAIL: %s\n" "$1" >&2
  printf "  fix: %s\n" "$2" >&2
  fail=1
}

main() {
  if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    return 1
  fi
  local host="$1" user="${2:-$DEFAULT_USER}"
  local uri="${3:-qemu+tls://${host}/system}"

  command -v ssh >/dev/null 2>&1 || note_fail "ssh not found" "install your distro's openssh client"
  command -v virsh >/dev/null 2>&1 || note_fail "virsh not found" "install libvirt-client (see check-setup-deps.sh)"

  if command -v ssh >/dev/null 2>&1; then
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
      -p "${PORT}" "${user}@${host}" true 2>/dev/null ||
      note_fail "ssh to ${user}@${host}:${PORT} failed" "ensure the host is up and your key is authorized"
  fi

  [[ -d "${PKI_DIR}" ]] && compgen -G "${PKI_DIR}/*.pem" >/dev/null 2>&1 ||
    note_fail "no TLS PKI material in ${PKI_DIR}" "provision client cert/key per docs/operating/providers/remote-libvirt.md"

  if command -v virsh >/dev/null 2>&1; then
    virsh -c "${uri}" list >/dev/null 2>&1 ||
      note_fail "cannot connect to ${uri}" "verify virtproxyd/TLS on the host and the URI"
  fi

  compgen -G "${HELPERS_DIR}/kdive-*" >/dev/null 2>&1 ||
    note_fail "guest-helper files not staged in ${HELPERS_DIR}" "ship deploy/remote-libvirt-guest-helpers/ to this host"

  if ((fail)); then
    printf "\nremote-libvirt target is NOT ready (see failures above)\n" >&2
    return 1
  fi
  printf "remote-libvirt target is ready\n"
}

main "$@"
```

- [ ] **Step 4: Run tests + lint**

Run: `uv run pytest tests/scripts/test_check_remote_libvirt.py -q && shellcheck scripts/check-remote-libvirt.sh && shfmt -i 2 -d scripts/check-remote-libvirt.sh`
Expected: tests PASS; lint clean.

- [ ] **Step 5: Add the recipe**

In `justfile`, after `check-local-libvirt`, add:

```make
# Preflight: can the remote-libvirt provider reach a target host? (report-only)
check-remote-libvirt host user="root" uri="":
    ./scripts/check-remote-libvirt.sh {{host}} {{user}} {{uri}}
```

- [ ] **Step 6: Commit**

```bash
git add scripts/check-remote-libvirt.sh tests/scripts/test_check_remote_libvirt.py justfile
git commit -m "feat: add remote-libvirt host preflight script"
```

---

## Phase 2 — Deployment & systemd

> Depends on Phase 0 (target tree) and Phase 1 (preflight recipe names). Authoring docs into `docs/operating/`; `install.md` cites preflight by recipe name (no file link across the phase boundary).

### Task 2.1: systemd units + env example

**Files:**
- Create: `deploy/systemd/system/kdive-server.service`, `kdive-worker.service`, `kdive-reconciler.service`; `deploy/systemd/user/` variants; `deploy/systemd/kdive.env.example`; `deploy/systemd/README.md`
- Test: `tests/deploy/test_systemd_units.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/deploy/test_systemd_units.py
"""Structural checks on the shipped systemd units.

`systemd-analyze verify` needs systemd and is environment-gated; these unit-file
assertions run everywhere and lock in the backend-retry contract (ADR-0114 §4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SYSTEM = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "system"
SERVICES = ("kdive-server", "kdive-worker", "kdive-reconciler")


@pytest.mark.parametrize("name", SERVICES)
def test_system_unit_has_retry_contract(name: str) -> None:
    text = (SYSTEM / f"{name}.service").read_text()
    assert "Restart=on-failure" in text
    assert "RestartSec=" in text
    assert "After=network-online.target" in text
    assert "EnvironmentFile=" in text
    assert "User=kdive" in text


@pytest.mark.parametrize("name", SERVICES)
def test_system_unit_exec_matches_process(name: str) -> None:
    text = (SYSTEM / f"{name}.service").read_text()
    process = name.removeprefix("kdive-")
    assert f"-m kdive {process}" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/deploy/test_systemd_units.py -q`
Expected: FAIL (units missing).

- [ ] **Step 3: Write the system units**

Create `deploy/systemd/system/kdive-server.service` (repeat for `worker`/`reconciler`, swapping the last arg and `Description`):

```ini
[Unit]
Description=KDIVE server
Documentation=https://github.com/randomparity/kdive
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kdive
Group=kdive
EnvironmentFile=/etc/kdive/kdive.env
ExecStart=/opt/kdive/.venv/bin/python -m kdive server
Restart=on-failure
RestartSec=5
# Backends (Postgres/MinIO/OIDC) are external and not systemd-ordered here; the unit
# retries until they are reachable rather than failing terminally (ADR-0114 §4).

[Install]
WantedBy=multi-user.target
```

`kdive-worker.service` → `ExecStart=… -m kdive worker`, `Description=KDIVE worker`.
`kdive-reconciler.service` → `ExecStart=… -m kdive reconciler`, `Description=KDIVE reconciler`.

- [ ] **Step 4: Write the `--user` variants**

Create `deploy/systemd/user/kdive-server.service` (and worker/reconciler) — same as system units but **remove** `User=`/`Group=`, set `WantedBy=default.target`, and keep `EnvironmentFile=%h/.config/kdive/kdive.env`:

```ini
[Unit]
Description=KDIVE server (user)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/kdive/kdive.env
ExecStart=%h/.local/share/kdive/.venv/bin/python -m kdive server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 5: Write `deploy/systemd/kdive.env.example`**

```bash
# KDIVE service environment (copy to /etc/kdive/kdive.env, chmod 0640, owner kdive).
# Backends are external and must be reachable; see docs/operating/systemd.md.
# Fill in credentials from your secret store; never commit real secrets. The DSN below
# is intentionally credential-less — supply the user/password via your secret manager or
# a separate, non-committed env file.
KDIVE_DATABASE_URL=postgresql://db.internal:5432/kdive
KDIVE_S3_ENDPOINT=https://minio.internal:9000
KDIVE_S3_ACCESS_KEY=replace-me
KDIVE_S3_SECRET_KEY=replace-me
KDIVE_OIDC_ISSUER=https://oidc.internal/realms/kdive
```

> Two constraints for the real `kdive.env.example`: (1) confirm each `KDIVE_*` name against `docs/guide/reference/config.md` (the generated config reference) and use the exact registered names; (2) keep the example free of any `user:password@host` literal — the `detect-secrets` pre-commit hook flags that pattern, and an inline `# pragma: allowlist secret` cannot be used because systemd `EnvironmentFile` would fold the comment into the value.

- [ ] **Step 6: Run tests + verify units if systemd present**

Run: `uv run pytest tests/deploy/test_systemd_units.py -q`
Expected: PASS.
If `systemd-analyze` exists: `systemd-analyze verify deploy/systemd/system/*.service` (expect no errors; "Unknown user 'kdive'" warnings are acceptable on a build host).

- [ ] **Step 7: Commit**

```bash
git add deploy/systemd tests/deploy/test_systemd_units.py
git commit -m "feat: add systemd units (system + user) for the three processes"
```

### Task 2.2: Operating docs (install, compose, k8s, systemd, providers)

**Files:**
- Create: `docs/operating/{install,docker-compose,kubernetes,systemd}.md`, `docs/operating/providers/{local-libvirt,remote-libvirt}.md`, `deploy/systemd/README.md`

- [ ] **Step 1: Author `docs/operating/install.md`**

Required sections: (a) install paths — source (`uv sync`), container (`ghcr.io/randomparity/kdive`, link `docs/development/releasing.md`), PyPI marked future; (b) host prerequisites — link `../guide/reference/config.md` for env, and cite preflight **by recipe name**: "run `just check-local-libvirt` / `just check-remote-libvirt` before first start"; (c) the three run modes, each linking its page below. Every relative link must resolve (Phase-0 `docs-links` gate).

- [ ] **Step 2: Author `docker-compose.md`, `kubernetes.md`, `systemd.md`**

- `docker-compose.md`: bring-up via root `docker-compose.yml`; link `../../deploy/compose/README.md`; the backend + migrate-one-shot ordering note; how to point an agent at the endpoint.
- `kubernetes.md`: `helm install` against `deploy/helm/kdive`; link `../../deploy/helm/kdive/README.md` and `runbooks/kubernetes-deploy.md`; secrets/values notes.
- `systemd.md`: install units from `../../deploy/systemd/`, the `kdive` user + `/etc/kdive/kdive.env` (0640) prerequisite, the **external-backend prerequisite** and that ordering against co-located backends is the operator's responsibility, `systemctl enable --now`, `journalctl -u kdive-server`, and the `--user` variant.

- [ ] **Step 3: Author the provider pages + `deploy/systemd/README.md`**

- `providers/local-libvirt.md`: what the provider needs; "run `just check-local-libvirt`"; link `runbooks/live-stack.md`.
- `providers/remote-libvirt.md`: TLS PKI, virtproxyd, guest helpers; "run `just check-remote-libvirt HOST USER URI`"; link `runbooks/remote-libvirt-host-setup.md`.
- `deploy/systemd/README.md`: one-screen install/enable summary pointing at `docs/operating/systemd.md`.

- [ ] **Step 4: Verify gates**

Run: `just docs-links && just docs-paths`
Expected: PASS (fix any dangling link/path the gates name).

- [ ] **Step 5: Commit**

```bash
git add docs/operating deploy/systemd/README.md
git commit -m "docs: add operator install + deployment guides"
```

---

## Phase 3 — Governance & metadata (public OSS)

> Independent of Phases 1/2/4 (depends only on Phase 0 paths). Root files + `.github` templates + `pyproject` metadata.

### Task 3.1: LICENSE + pyproject metadata + guard test

**Files:**
- Create: `LICENSE`, `tests/test_project_metadata.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Confirm license compatibility (acceptance gate)**

Run: `uv export --no-emit-project --no-dev --no-default-groups --format requirements-txt | sort`
Confirm the runtime set is LGPL/permissive (libvirt-python LGPL, psycopg LGPL, others MIT/BSD/Apache). Record the one-line conclusion in the commit body. (GPL tools `crash`/`gdb`/`drgn` are invoked as separate processes — no linkage.)

- [ ] **Step 2: Write the failing metadata test**

```python
# tests/test_project_metadata.py
"""The public release must declare its license and project URLs."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_license_is_apache() -> None:
    assert _pyproject()["project"]["license"] == "Apache-2.0"


def test_project_urls_present() -> None:
    urls = _pyproject()["project"]["urls"]
    for key in ("Homepage", "Repository", "Issues", "Changelog"):
        assert key in urls and urls[key].startswith("https://")


def test_license_file_exists() -> None:
    assert (ROOT / "LICENSE").is_file()
    assert "Apache License" in (ROOT / "LICENSE").read_text()[:200]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_project_metadata.py -q`
Expected: FAIL (no license/urls/LICENSE).

- [ ] **Step 4: Add the LICENSE file**

Fetch the canonical Apache-2.0 text into `LICENSE`:
Run: `curl -fsSL https://www.apache.org/licenses/LICENSE-2.0.txt -o LICENSE`
Then verify it begins with `Apache License` and `Version 2.0`.

- [ ] **Step 5: Add pyproject metadata**

In `pyproject.toml` `[project]`, after the `description` line, add `license = "Apache-2.0"` and `authors = [{ name = "David Christensen" }]`; and add a new table:

```toml
[project.urls]
Homepage = "https://github.com/randomparity/kdive"
Repository = "https://github.com/randomparity/kdive"
Issues = "https://github.com/randomparity/kdive/issues"
Changelog = "https://github.com/randomparity/kdive/blob/main/CHANGELOG.md"
```

- [ ] **Step 6: Run tests + lock-check**

Run: `uv run pytest tests/test_project_metadata.py -q && uv lock --check`
Expected: tests PASS; lock still current (metadata-only change does not alter the resolved set; if `uv lock --check` complains, run `uv lock` and stage `uv.lock`).

- [ ] **Step 7: Commit**

```bash
git add LICENSE pyproject.toml uv.lock tests/test_project_metadata.py
git commit -m "chore: license under Apache-2.0 and declare project metadata"
```

### Task 3.2: CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, ARCHITECTURE, .github templates

**Files:**
- Create: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `ARCHITECTURE.md`, `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.yml`, `.github/ISSUE_TEMPLATE/config.yml`, `.github/PULL_REQUEST_TEMPLATE.md`

- [ ] **Step 1: Author the governance files**

- `CONTRIBUTING.md`: dev loop (`just setup`, `just ci`), branch/Conventional-Commits/no-squash rules, the PR + CI gate, link `docs/development/releasing.md` and `CODE_OF_CONDUCT.md`.
- `SECURITY.md`: coordinated disclosure (private report channel/email), supported-versions table (`0.y`), response expectations.
- `CODE_OF_CONDUCT.md`: Contributor Covenant 2.1 text with the project contact filled in.
- `ARCHITECTURE.md`: ~1 page — three processes, six objects, provider seam; link `docs/design/top-level-design.md` as authoritative.

- [ ] **Step 2: Author the `.github` templates**

- `bug_report.yml` / `feature_request.yml`: GitHub issue-form YAML (labels, required fields).
- `config.yml`: `blank_issues_enabled: false` + a contact link to Discussions/Security.
- `PULL_REQUEST_TEMPLATE.md`: checklist (tests, `just ci`, Conventional Commits, no-squash, docs updated).

- [ ] **Step 3: Verify gates**

Run: `just docs-links && just docs-paths && actionlint .github/workflows/*.yml`
(Issue-form YAML isn't a workflow; actionlint only the workflows. Confirm the YAML parses: `uv run python -c "import yaml,glob;[yaml.safe_load(open(f)) for f in glob.glob('.github/ISSUE_TEMPLATE/*.yml')]"`.)
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md ARCHITECTURE.md .github/ISSUE_TEMPLATE .github/PULL_REQUEST_TEMPLATE.md
git commit -m "docs: add public-OSS governance and GitHub templates"
```

---

## Phase 4 — Agent onboarding

> Independent of Phases 1/2/3 (depends only on Phase 0). Example configs must be valid JSON.

### Task 4.1: Agent onboarding guide + example configs

**Files:**
- Create: `docs/guide/agents/index.md`, `docs/guide/agents/mcp.json`, `docs/guide/agents/claude_desktop_config.json`
- Test: `tests/docs/test_agent_config_examples.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/docs/test_agent_config_examples.py
"""The shipped agent config examples must be valid JSON with an mcpServers.kdive entry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[2] / "docs" / "guide" / "agents"


@pytest.mark.parametrize("name", ["mcp.json", "claude_desktop_config.json"])
def test_example_is_valid_json_with_kdive_server(name: str) -> None:
    data = json.loads((AGENTS / name).read_text())
    assert "kdive" in data["mcpServers"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/docs/test_agent_config_examples.py -q`
Expected: FAIL (files missing).

- [ ] **Step 3: Write the example configs**

`docs/guide/agents/mcp.json` (Claude Code project `.mcp.json` shape — streamable HTTP):

```json
{
  "mcpServers": {
    "kdive": {
      "type": "http",
      "url": "https://kdive.internal/mcp",
      "headers": { "Authorization": "Bearer ${KDIVE_TOKEN}" }
    }
  }
}
```

`docs/guide/agents/claude_desktop_config.json` (same server, Desktop file name):

```json
{
  "mcpServers": {
    "kdive": {
      "type": "http",
      "url": "https://kdive.internal/mcp",
      "headers": { "Authorization": "Bearer ${KDIVE_TOKEN}" }
    }
  }
}
```

> Verify the transport/field names against the current MCP client config docs (context7 / official docs) before finalizing; the test only asserts JSON validity and the `mcpServers.kdive` key, so correct the surrounding fields to match the real schema.

- [ ] **Step 4: Author `docs/guide/agents/index.md`**

Sections: where each config file goes (Claude Code project root `.mcp.json` vs Desktop config dir), the OIDC bearer-token note, and a first-call smoke sequence linking the tool reference: `investigations.create` → `allocations.*` → `jobs.wait`. Verify the tool names against `../reference/index.md`.

- [ ] **Step 5: Run tests + gates**

Run: `uv run pytest tests/docs/test_agent_config_examples.py -q && just docs-links && just docs-paths`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/guide/agents tests/docs/test_agent_config_examples.py
git commit -m "docs: add agent onboarding guide and MCP config examples"
```

---

## Phase 5 — Fit & finish (closes after 1–4)

### Task 5.1: Index pages + README front door

**Files:**
- Create: `docs/README.md`, `docs/operating/index.md`
- Modify: root `README.md`

- [ ] **Step 1: Author `docs/README.md` (master index)**

Audience-tiered table of contents linking: users/agents → `guide/index.md`, `guide/agents/index.md`; operators → `operating/install.md` (+ compose/k8s/systemd/providers); contributors → `../CONTRIBUTING.md`, `development/releasing.md`; canonical → `design/top-level-design.md`, `adr/`. Every target now exists.

- [ ] **Step 2: Author `docs/operating/index.md`**

Links every page authored in Phase 2 (install, docker-compose, kubernetes, systemd, providers/*, runbooks/).

- [ ] **Step 3: Rewrite root `README.md`**

Concise front door: one-paragraph what-it-is, the three entry points (use KDIVE → `docs/guide/index.md`; run KDIVE → `docs/operating/install.md`; develop KDIVE → `CONTRIBUTING.md`), a quickstart pointer, license badge line. Preserve the existing requirements/setup content or move it behind `docs/operating/install.md` with a link — do not drop it.

- [ ] **Step 4: Verify gates**

Run: `just docs-links && just docs-paths`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/README.md docs/operating/index.md README.md
git commit -m "docs: add doc indexes and rewrite README as a front door"
```

### Task 5.2: `.gitignore` sweep + CHANGELOG + full CI

**Files:**
- Modify: `.gitignore`, `CHANGELOG.md`

- [ ] **Step 1: Generalize the `.live-*` ignores**

In `.gitignore`, under the existing live-stack section, add:

```gitignore
# Local live-run scratch (logs, pids, scratch build/components dirs)
.live-*/
.live-*.pid
```

- [ ] **Step 2: Add the `[Unreleased]` changelog section**

Regenerate from commits (git-cliff is configured): `just changelog`
Then confirm an `[Unreleased]` section now lists the release-surface work. If `just changelog` requires a tag context that is absent, manually insert under the top header:

```markdown
## [Unreleased]

### Added

- Audience-tiered documentation tree, doc-integrity CI guards, host preflight
  scripts, systemd units, Apache-2.0 license + OSS governance, and agent onboarding.
```

- [ ] **Step 3: Run the full gate**

Run: `just ci`
Expected: green (lint, type, both doc guards, docs-check, config-docs-check, tests, etc.). Fix anything that fails before continuing.

- [ ] **Step 4: Commit**

```bash
git add .gitignore CHANGELOG.md
git commit -m "chore: ignore live-run scratch and record release-surface changelog"
```

---

## Self-review notes (for the executor)

- **Spec coverage:** scope items 1–7 map to Phases 0–5 respectively (restructure→P0; guards→P0; preflight→P1; deployment/systemd→P2; governance/metadata→P3; agent onboarding→P4; fit&finish→P5). The falsifiable acceptance signal (operator reaches a running local-libvirt deploy from `install.md` + `just check-local-libvirt`) is exercised manually after P2.
- **CI individual-recipe gotcha:** every new gate is added to BOTH `just ci` and `.github/workflows/ci.yml` (Task 0.4) — adding to the aggregate alone does not gate PRs in this repo.
- **Config-name accuracy:** wherever a step writes a literal `KDIVE_*` name (systemd env example, agent token), it must be reconciled against `docs/guide/reference/config.md`; the steps say so explicitly.
- **No source/runtime code is touched** — `just type` whole-tree must stay green; the only `src/` impact is none.
- **`docs-paths` exemption ordering:** `docs-paths` exempts `docs/design/**` and `docs/archive/**` (Task 0.2), and this plan + the spec live in `docs/specs/` until Task 0.3 Step 1 moves them into `docs/design/`. The first real-tree `docs-paths` run is Task 0.3 Step 6 — *after* that move — so the spec/plan (full of intentional `docs/...` move references) are already exempt when the guard first runs. Do not reorder the move after the first guard run.
