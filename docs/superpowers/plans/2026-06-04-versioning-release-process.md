# Versioning & Release Process Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a SemVer versioning policy and a tag-driven release process to kdive — runtime version info that carries a commit SHA and a `-dev` marker, a generated changelog, release tooling, and the `0.2.0` in-development bump.

**Architecture:** A lazy, cached `version.py` resolves `(version, commit, is_release)` from a baked `_buildinfo.py` (in built artifacts) → live git (dev checkout) → unknown. `justfile` recipes drive version bumps (`uv version`, which keeps `pyproject.toml` + `uv.lock` in sync), changelog generation (git-cliff), build-info baking, and tag-only releases. A tag-triggered `release.yml` builds artifacts and cuts an internal GitHub Release. Decisions are recorded in **ADR-0041** (already on this branch); this plan realizes them.

**Tech Stack:** Python 3.13, `uv` (incl. `uv version`/`uv build`/`uv lock --check`), `just`, `git-cliff@2.13.1` (via `uvx`), `gh` CLI, GitHub Actions, pytest.

**Prerequisites already on this branch (do not recreate):** `docs/adr/0041-versioning-release-process.md` and `docs/superpowers/specs/2026-06-04-versioning-release-process-design.md`.

---

### Task 1: `version.py` — the version-info resolver

**Files:**
- Create: `src/kdive/version.py`
- Modify: `src/kdive/__init__.py` (currently empty)
- Test: `tests/test_version.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_version.py`:

```python
"""Unit tests for the version-info resolver (ADR-0041 decision 5).

Each case mocks one resolution layer at the boundary (the baked import, the `_git`
subprocess wrapper, and `package_version`) and asserts the exact `full_version()` string.
An autouse fixture clears the `version_info` memo between cases so one case's cached
result never masks the next.
"""

from __future__ import annotations

import pytest

from kdive import version
from kdive.version import VersionInfo, full_version, version_info


@pytest.fixture(autouse=True)
def _clear_version_cache():
    version_info.cache_clear()
    yield
    version_info.cache_clear()


def _no_baked(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: None)


def test_baked_release(monkeypatch):
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    monkeypatch.setattr(version, "_from_baked", lambda: VersionInfo("0.2.0", "1a2b3c4", True))
    assert full_version() == "0.2.0+g1a2b3c4"


def test_baked_dev(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: VersionInfo("0.2.0", "1a2b3c4", False))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_live_git_clean_exact_tag_is_release(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): "v0.2.0",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0+g1a2b3c4"


def test_live_git_on_tag_but_dirty_is_dev(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): "v0.2.0",
        ("status", "--porcelain"): " M src/kdive/x.py",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_live_git_untagged_is_dev(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): None,
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_unknown_no_baked_no_git(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    monkeypatch.setattr(version, "_git", lambda *a: None)
    assert full_version() == "0.2.0-dev"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/test_version.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.version'` (or `ImportError`).

- [ ] **Step 3: Write `src/kdive/version.py`**

```python
"""Runtime version reporting: package version + commit SHA + release/dev flag (ADR-0041).

`full_version()` is the display string used by `--version` and the startup log line:
`X.Y.Z+g<sha>` for a release build, `X.Y.Z-dev+g<sha>` otherwise. Commit and release
status resolve, first hit wins, from: a baked `kdive._buildinfo` module (present only in a
built artifact), live git (a dev checkout), or unknown. No git subprocess runs at import
time — resolution is lazy and memoized for the process lifetime, with `cache_clear()`
exposed for tests.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

_GIT_TIMEOUT = 3.0


def package_version() -> str:
    """Return the installed distribution version (`[project].version`), or ``0.0.0``."""
    try:
        return _dist_version("kdive")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Resolved version facts for the running process."""

    version: str
    commit: str | None
    is_release: bool


def _git(*args: str) -> str | None:
    """Run a read-only ``git`` command; return stripped stdout, or ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _from_baked() -> VersionInfo | None:
    """Read `(COMMIT, RELEASE)` from the baked `_buildinfo` module, if present and valid."""
    try:
        from kdive import _buildinfo
    except ImportError:
        return None
    commit = getattr(_buildinfo, "COMMIT", None)
    release = getattr(_buildinfo, "RELEASE", None)
    if not isinstance(commit, str) or not isinstance(release, bool):
        return None
    return VersionInfo(package_version(), commit, release)


def _from_git() -> VersionInfo | None:
    """Resolve from live git, or ``None`` when not in a usable checkout."""
    commit = _git("rev-parse", "--short", "HEAD")
    if commit is None:
        return None
    version = package_version()
    exact = _git("describe", "--tags", "--exact-match", "HEAD")
    clean = _git("status", "--porcelain") == ""
    return VersionInfo(version, commit, exact == f"v{version}" and clean)


@lru_cache(maxsize=1)
def version_info() -> VersionInfo:
    """Resolve `(version, commit, is_release)` once per process: baked → git → unknown."""
    return _from_baked() or _from_git() or VersionInfo(package_version(), None, False)


def full_version() -> str:
    """Return the display string, e.g. ``0.2.0+g1a2b3c4`` or ``0.2.0-dev+g1a2b3c4``."""
    info = version_info()
    suffix = "" if info.is_release else "-dev"
    commit = f"+g{info.commit}" if info.commit else ""
    return f"{info.version}{suffix}{commit}"
```

- [ ] **Step 4: Set `__version__` in `src/kdive/__init__.py`**

Replace the empty file's contents with:

```python
"""kdive — Kernel Debug, Inspect, Validate, Explore."""

from kdive.version import package_version

__version__ = package_version()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/test_version.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/version.py src/kdive/__init__.py tests/test_version.py
uv run ruff format src/kdive/version.py src/kdive/__init__.py tests/test_version.py
uv run ty check
git add src/kdive/version.py src/kdive/__init__.py tests/test_version.py
git commit -m "feat(version): resolve version + commit SHA + release/dev flag"
```

---

### Task 2: `pyproject` ⇆ installed-metadata drift guard

**Files:**
- Test: `tests/test_version_pyproject_consistency.py`

This is the drift guard from the spec's Testing section. It asserts `[project].version` equals the installed metadata version. The `uv.lock` staleness check is **not** a test (a test runs under `uv run`, which silently re-locks first); that is a `uv lock --check` gate, added in Task 6.

- [ ] **Step 1: Write the failing test**

Create `tests/test_version_pyproject_consistency.py`:

```python
"""Guard that `[project].version` and the installed distribution metadata agree.

The test runs under `uv run`, which resyncs the editable install to the current
`pyproject.toml` first, so a divergence means a genuine packaging problem (ADR-0041
decision 4). `uv.lock` staleness is guarded separately by `uv lock --check` (Task 6).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from kdive.version import package_version

_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def test_pyproject_matches_installed_metadata():
    assert _pyproject_version() == package_version()
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run python -m pytest tests/test_version_pyproject_consistency.py -v`
Expected: PASS (`0.1.0 == 0.1.0` — the bump to `0.2.0` happens in Task 8 and keeps both sides equal via `uv version`).

> Note: this test passes immediately (no red phase) — it is a guardrail, not new behavior. To confirm it can fail, temporarily edit `pyproject.toml`'s version to `9.9.9`, re-run (expect FAIL), then revert.

- [ ] **Step 3: Commit**

```bash
git add tests/test_version_pyproject_consistency.py
git commit -m "test(version): guard pyproject version == installed metadata"
```

---

### Task 3: `--version` flag and startup log line

**Files:**
- Modify: `src/kdive/__main__.py` (`build_parser` ~line 23-35, `main` ~line 84-95)
- Test: `tests/test_main_version.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_main_version.py`:

```python
"""`--version` prints and exits; every command logs the version at startup (ADR-0041)."""

from __future__ import annotations

import pytest

from kdive.__main__ import main


def test_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("kdive ")


def test_startup_logs_version(monkeypatch, caplog):
    # Don't actually run the async loop; just confirm main logs before dispatching.
    monkeypatch.setattr("kdive.__main__.asyncio.run", lambda coro: coro.close())
    # Capture on the emitting logger directly, so this does not depend on whether
    # configure_logging() leaves propagation to root enabled (ADR-0014 may set
    # propagate=False / replace root handlers — caplog at root would then miss it).
    with caplog.at_level("INFO", logger="kdive.__main__"):
        main(["reconciler"])
    assert any("starting kdive" in r.getMessage() for r in caplog.records)
```

> Note: `caplog.at_level(..., logger="kdive.__main__")` attaches to the emitting logger,
> so capture does not depend on `configure_logging`'s propagation behavior. If `log.py`
> turns out to remove the logger's handlers (not just set propagation), assert on the
> structured-log output stream instead (`capsys`/the configured handler) rather than
> `caplog`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/test_main_version.py -v`
Expected: FAIL — `--version` is unrecognized (SystemExit code 2), and no "starting kdive" log.

- [ ] **Step 3: Implement in `src/kdive/__main__.py`**

Add to the imports near the top (after the existing `from kdive.log import configure_logging`):

```python
import logging

from kdive.version import full_version
```

Add a module logger after the constants (`_DEFAULT_HOST`/`_DEFAULT_PORT`):

```python
_log = logging.getLogger(__name__)
```

In `build_parser`, add a `--version` argument immediately after the `--log-level` argument:

```python
    parser.add_argument(
        "--version",
        action="version",
        version=f"kdive {full_version()}",
    )
```

In `main`, add the startup log immediately after `configure_logging(args.log_level)`:

```python
    _log.info("starting kdive %s (%s)", full_version(), args.command)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/test_main_version.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Manually confirm the CLI**

Run: `uv run python -m kdive --version`
Expected: prints `kdive 0.1.0-dev+g<sha>` (dev, because HEAD is not on a clean `v0.1.0` tag) and exits 0.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src/kdive/__main__.py tests/test_main_version.py
uv run ruff format src/kdive/__main__.py tests/test_main_version.py
uv run ty check
git add src/kdive/__main__.py tests/test_main_version.py
git commit -m "feat(cli): add --version and a startup version log line"
```

---

### Task 4: Build-info baking — stamp script, `.gitignore`, `build` recipe

**Files:**
- Create: `scripts/stamp-buildinfo.sh`
- Modify: `.gitignore`
- Modify: `justfile` (add the `build` recipe)

- [ ] **Step 1: Write `scripts/stamp-buildinfo.sh`**

```bash
#!/usr/bin/env bash
# Generate src/kdive/_buildinfo.py for baking into a built artifact (ADR-0041 decision 5).
# RELEASE is taken from $1 ("true"/"false") when given — release.yml passes "true", since
# the tag-triggered workflow is authoritative — otherwise derived from a live-git
# clean-exact-tag test. The commit + release are computed BEFORE the file is written so the
# (gitignored) file cannot affect the porcelain check.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="$repo_root/src/kdive/_buildinfo.py"

commit="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo unknown)"

release="${1:-}"
if [[ -z "$release" ]]; then
  version="$(cd "$repo_root" && uv version --short)"
  exact="$(git -C "$repo_root" describe --tags --exact-match HEAD 2>/dev/null || true)"
  if [[ "$exact" == "v$version" && -z "$(git -C "$repo_root" status --porcelain)" ]]; then
    release="true"
  else
    release="false"
  fi
fi

if [[ "$release" == "true" ]]; then py_release="True"; else py_release="False"; fi

cat >"$target" <<EOF
# Generated by scripts/stamp-buildinfo.sh — do not edit, do not commit (gitignored).
COMMIT = "$commit"
RELEASE = $py_release
EOF
```

- [ ] **Step 2: Make it executable and lint it**

```bash
chmod +x scripts/stamp-buildinfo.sh
shellcheck scripts/stamp-buildinfo.sh
shfmt -i 2 -d scripts/stamp-buildinfo.sh
```
Expected: shellcheck clean; `shfmt -d` prints no diff (if it does, run `shfmt -i 2 -w scripts/stamp-buildinfo.sh`).

- [ ] **Step 3: Add the ignore entries to `.gitignore`**

Append:

```gitignore
# Generated build info (baked into artifacts only; never committed)
/src/kdive/_buildinfo.py

# Build artifacts
/dist/
```

- [ ] **Step 4: Add the `build` recipe to `justfile`**

Add after the existing `test-live` recipe (or anywhere among the recipes):

```makefile
# Build wheel + sdist with build info baked in, then remove the stamp so it never lingers
# in the editable checkout (a leftover would shadow live-git version reporting). Pass
# release=true only when building from a release tag.
build release="false":
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'rm -f src/kdive/_buildinfo.py' EXIT
    ./scripts/stamp-buildinfo.sh "{{release}}"
    uv build
```

- [ ] **Step 5: Verify baking works and cleans up (the inclusion guard, run locally)**

```bash
test ! -f src/kdive/_buildinfo.py && echo "precondition: no stamp file"
just build
unzip -l dist/*.whl | grep 'kdive/_buildinfo.py' && echo "BAKED: _buildinfo.py is in the wheel"
test ! -f src/kdive/_buildinfo.py && echo "CLEANED: stamp removed from the working tree"
git status --porcelain | grep -q . && echo "tree dirty (unexpected)" || echo "tree clean"
```
Expected: all four echoes fire (`precondition`, `BAKED`, `CLEANED`, `tree clean`).

- [ ] **Step 6: Add a standing baking-inclusion guard to CI**

So a future `uv_build` change that stopped packaging the generated module fails loudly
(instead of silently reverting artifacts to `-dev`/no-SHA). In `.github/workflows/ci.yml`,
inside the `lint-type-test` job, add this step after the `Set up just` step:

```yaml
      - name: Build-info baking guard
        run: |
          just build
          unzip -l dist/*.whl | grep -q 'kdive/_buildinfo.py' \
            || { echo "::error::_buildinfo.py not packaged in the wheel"; exit 1; }
          test ! -f src/kdive/_buildinfo.py \
            || { echo "::error::_buildinfo.py left in the tree after build"; exit 1; }
```

- [ ] **Step 7: Lint the workflow and commit**

```bash
just lint-workflows
git add scripts/stamp-buildinfo.sh .gitignore justfile .github/workflows/ci.yml
git commit -m "feat(build): bake commit SHA + release flag into artifacts"
```

---

### Task 5: Changelog — `cliff.toml`, `CHANGELOG.md`, `GIT_CLIFF` var, `changelog` recipe

**Files:**
- Create: `cliff.toml`
- Create: `CHANGELOG.md` (generated)
- Modify: `justfile` (add `GIT_CLIFF` variable + `changelog` recipe)

- [ ] **Step 1: Scaffold a known-good Keep-a-Changelog config**

```bash
uvx git-cliff@2.13.1 --init keepachangelog
```
Expected: writes `cliff.toml` from git-cliff's bundled keepachangelog template.

- [ ] **Step 2: Edit the `[git]` commit parsers in `cliff.toml`**

Replace the `commit_parsers = [ ... ]` array (inside the `[git]` table) with our mapping, and ensure `conventional_commits = true` and `filter_unconventional = true` are set in `[git]`:

```toml
commit_parsers = [
  { message = "^feat", group = "Added" },
  { message = "^fix", group = "Fixed" },
  { message = "^perf", group = "Changed" },
  { message = "^refactor", group = "Changed" },
  { message = "^docs", group = "Documentation" },
  { body = ".*security", group = "Security" },
  { message = "^revert", group = "Changed" },
  { message = "^chore\\(release\\)", skip = true },
  { message = "^chore", skip = true },
  { message = "^test", skip = true },
  { message = "^ci", skip = true },
]
```

The keepachangelog template does **not** set `tag_pattern` (verified against
`git-cliff@2.13.1 --init keepachangelog`). Adding `tag_pattern = "v[0-9]*"` to `[git]` is
**optional** here — the repo's only tags are already `vX.Y.Z`, so git-cliff's default tag
handling works — but add it for explicitness if you like.

- [ ] **Step 3: Add the `GIT_CLIFF` variable and `changelog` recipe to `justfile`**

Near the top of `justfile`, after the `set shell := ...` line, add:

```makefile
# Pinned git-cliff version — referenced by the changelog recipe and release.yml (one place).
GIT_CLIFF := "git-cliff@2.13.1"
```

Add the recipe (near the other recipes):

```makefile
# Regenerate CHANGELOG.md from conventional-commit history (Keep a Changelog).
changelog:
    uvx {{GIT_CLIFF}} --output CHANGELOG.md
```

- [ ] **Step 4: Generate `CHANGELOG.md` and verify it is non-empty**

```bash
just changelog
test -s CHANGELOG.md && echo "CHANGELOG.md generated"
grep -q '## \[0.1.0\]' CHANGELOG.md && echo "has a [0.1.0] section"
head -20 CHANGELOG.md
```
Expected: `CHANGELOG.md generated`, a `[0.1.0]` section present (derived from history up to the `v0.1.0` tag), and an `[Unreleased]` section for commits since.

- [ ] **Step 5: Commit**

```bash
git add cliff.toml CHANGELOG.md justfile
git commit -m "feat(changelog): git-cliff config + generated CHANGELOG"
```

---

### Task 6: `justfile` release recipes + `uv lock --check` gate

**Files:**
- Modify: `justfile` (add `set-version`, `lock-check`, `release`; extend `ci`)
- Modify: `.github/workflows/ci.yml` (add a `lock-check` step)

- [ ] **Step 1: Add the recipes to `justfile`**

```makefile
# Set the project version in pyproject.toml AND uv.lock together. `--no-sync` re-locks
# (updates uv.lock) WITHOUT rebuilding the virtual environment — so a version bump does not
# require libvirt-dev to compile libvirt-python; the editable install refreshes on the next
# `uv run`. Used at a Milestone start and for the post-release "begin <next>-dev" bump.
# Commit the result on a branch — never directly on main.
set-version VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{VERSION}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
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
    [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || { echo "behind origin/main" >&2; exit 1; }
    current="$(uv version --short)"
    [[ "$current" == "{{VERSION}}" ]] || { echo "pyproject version $current != {{VERSION}}" >&2; exit 1; }
    git tag -a "v{{VERSION}}" -m "Release v{{VERSION}}"
    git push origin "v{{VERSION}}"
    echo "Pushed tag v{{VERSION}}. NEXT: open a 'chore(release): begin <next>-dev' PR"
    echo "(just set-version <next>; just changelog) — see docs/RELEASING.md."
```

- [ ] **Step 2: Extend the `ci` umbrella recipe to include `lock-check`**

Change the existing `ci:` recipe line from:

```makefile
ci: lint type lint-shell lint-workflows check-mermaid test
```

to:

```makefile
ci: lint type lock-check lint-shell lint-workflows check-mermaid test
```

- [ ] **Step 3: Add a `lock-check` step to `.github/workflows/ci.yml`**

In the `lint-type-test` job, immediately after the `Sync dependencies` step (the `run: uv sync --locked` step) and before `Set up just`, add:

```yaml
      - name: Lockfile up to date
        run: uv lock --check
```

- [ ] **Step 4: Verify the recipes locally**

```bash
just lock-check && echo "lock-check passes (lock is current)"
just set-version 1.2.3.4 ; echo "exit=$?"   # expect rejection (bad format), exit 1
git checkout -- pyproject.toml uv.lock 2>/dev/null || true
```
Expected: `lock-check passes`; `set-version 1.2.3.4` prints the format error and exits 1 (no files changed). Confirm `git status --porcelain` is clean afterward.

- [ ] **Step 5: Lint the workflow and commit**

```bash
just lint-workflows
git add justfile .github/workflows/ci.yml
git commit -m "feat(release): set-version/release/lock-check recipes + CI lock gate"
```

---

### Task 7: `release.yml` — tag-triggered build + internal GitHub Release

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Write `.github/workflows/release.yml`**

```yaml
name: Release

on:
  push:
    tags: ["v*"]

permissions:
  contents: write

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

env:
  GIT_CLIFF: git-cliff@2.13.1

jobs:
  release:
    name: build · notes · github release
    runs-on: ubuntu-latest
    steps:
      - name: Checkout (full history + tags)
        uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3
        with:
          fetch-depth: 0
          persist-credentials: false

      - name: Set up uv
        uses: astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39 # v8.2.0
        with:
          version: "0.11.18"
          enable-cache: true

      - name: Set up just
        uses: extractions/setup-just@53165ef7e734c5c07cb06b3c8e7b647c5aa16db3 # v4

      - name: Verify tag matches pyproject version
        run: |
          expected="v$(uv version --short)"
          if [[ "${GITHUB_REF_NAME}" != "$expected" ]]; then
            echo "::error::tag ${GITHUB_REF_NAME} != pyproject $expected"
            exit 1
          fi

      - name: Build artifacts (SHA baked, release flag set)
        run: just build release=true

      - name: Generate release notes
        run: |
          # Guard on the fetch having worked, not on the notes being non-empty: a thin
          # patch (only skipped chore/merge commits) is a legitimate release.
          prev="$(git describe --tags --abbrev=0 "${GITHUB_REF_NAME}^" 2>/dev/null || true)"
          if [[ -n "$prev" ]]; then
            count="$(git rev-list --count "${prev}..${GITHUB_REF_NAME}")"
            if [[ "$count" -eq 0 ]]; then
              echo "::error::no commits between $prev and ${GITHUB_REF_NAME} — shallow fetch?"
              exit 1
            fi
          fi
          uvx "${GIT_CLIFF}" --latest --strip header --output RELEASE_NOTES.md
          if [[ ! -s RELEASE_NOTES.md ]]; then
            echo "Maintenance release — see commit history." >RELEASE_NOTES.md
          fi

      - name: Create or update the GitHub Release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          tag="${GITHUB_REF_NAME}"
          if gh release view "$tag" >/dev/null 2>&1; then
            gh release edit "$tag" --notes-file RELEASE_NOTES.md
            gh release upload "$tag" dist/* --clobber
          else
            gh release create "$tag" --title "$tag" --notes-file RELEASE_NOTES.md dist/*
          fi
```

- [ ] **Step 2: Lint and security-scan the workflow**

```bash
just lint-workflows
```
Expected: `actionlint` and `zizmor` both pass (pinned SHAs, `persist-credentials: false`, explicit `permissions`, env-var refs not templated into shell).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): tag-triggered build + internal GitHub Release"
```

---

### Task 8: `RELEASING.md` runbook + README/AGENTS pointers

**Files:**
- Create: `docs/RELEASING.md`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write `docs/RELEASING.md`**

```markdown
# Releasing

This project follows [ADR-0041](adr/0041-versioning-release-process.md): SemVer in the
`0.y.z` phase, milestone→minor, with the **in-tree version always pointing at the next
unreleased version** so a `-dev` build is never ambiguous across a release boundary.

## Version bumps (each via `uv version`, which updates `pyproject.toml` + `uv.lock`)

- **At a Milestone's start** — `just set-version <next-minor>` (e.g. `0.2.0` for M1), on a
  branch → PR → merge.
- **Immediately after a release** — open a `chore(release): begin <next>-dev` PR:
  `just set-version <next-patch>` and `just changelog` (the new tag now exists, so the
  `[Unreleased]` section rolls into the dated released section). This is **required** — it
  is what keeps `X.Y.Z-dev` meaning "ahead of the last release."

Never hand-edit the version: editing `pyproject.toml` alone desyncs `uv.lock` and breaks
`uv sync --locked` in CI. `just lock-check` (and CI) catch a stale lock.

## Cutting a release

1. Ensure `main` is green and `[project].version` already equals the version to release
   (it was bumped at Milestone start or by the previous post-release bump — **the release
   itself does not bump the version**).
2. From an up-to-date, clean `main`: `just release <X.Y.Z>`. This verifies state and pushes
   the annotated `vX.Y.Z` **tag only** (pushing a tag is not a commit to the protected
   branch).
3. `release.yml` triggers on the tag: it verifies tag == version, builds the wheel + sdist
   (commit SHA baked, `RELEASE=true`), generates notes from git-cliff, and creates an
   internal GitHub Release with the artifacts attached.
4. Open the post-release "begin `<next>`-dev" bump PR (above).

## Version reporting

`python -m kdive --version` and the startup log show `X.Y.Z+g<sha>` for a release build and
`X.Y.Z-dev+g<sha>` otherwise. The SHA/flag come from a baked `_buildinfo.py` in artifacts,
or live git in a checkout.

## Future toggles (not yet enabled)

- **PyPI publish** — add a `uv publish` step to `release.yml` after the GitHub Release step.
- **Signed tags / artifact attestation** — sign `vX.Y.Z` tags and attach provenance.

## Rollback

A release is a tag + a GitHub Release; it changes no `main` history. To withdraw one, delete
the GitHub Release and the tag (`git push origin :vX.Y.Z`), fix forward, and re-tag.
```

- [ ] **Step 2: Add the README pointer**

In `README.md`, add a short "Releasing" line. Append this section after the "Test environments" section (end of file):

```markdown

Releasing
---------

See [`docs/RELEASING.md`](docs/RELEASING.md) for the versioning policy
([ADR-0041](docs/adr/0041-versioning-release-process.md)) and the release process.
```

- [ ] **Step 3: Add the AGENTS.md pointer**

First check where `AGENTS.md` references ADRs: `grep -n 'docs/adr\|ADR-\|Conventions' AGENTS.md`. If there is a "Conventions" section that already lists ADRs, add the pointer as a bullet there; otherwise **append a new section at the end of `AGENTS.md`** (mirroring the README change):

```markdown

## Releasing

- **Releasing & versioning:** see [`docs/RELEASING.md`](docs/RELEASING.md) and
  [ADR-0041](docs/adr/0041-versioning-release-process.md) (SemVer, milestone→minor,
  tag-driven release).
```

- [ ] **Step 4: Mermaid/doc checks and commit**

```bash
just check-mermaid
git add docs/RELEASING.md README.md AGENTS.md
git commit -m "docs(release): RELEASING runbook + README/AGENTS pointers"
```

---

### Task 9: Bump the in-tree version to `0.2.0`

**Files:**
- Modify: `pyproject.toml` + `uv.lock` (via `uv version`)

This is the immediate deliverable: move in-tree to `0.2.0` (M1 in progress). The `v0.2.0`
tag is **not** cut here — that happens later when M1 completes.

- [ ] **Step 1: Bump via the recipe**

```bash
just set-version 0.2.0
```
Expected: `pyproject.toml` `[project].version` and the `kdive` pin in `uv.lock` both become `0.2.0`.

- [ ] **Step 2: Verify everything stays consistent**

```bash
just lock-check && echo "lock current"
uv run python -m pytest tests/test_version_pyproject_consistency.py -v   # 0.2.0 == 0.2.0
uv run python -m kdive --version                                          # kdive 0.2.0-dev+g<sha>
```
Expected: lock current; consistency test passes; `--version` now reports `0.2.0-dev+g<sha>`.

- [ ] **Step 3: Run the full gate**

```bash
just ci
```
Expected: lint, type, lock-check, shell, workflows, mermaid, and tests all pass.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(release): begin 0.2.0 development (M1)"
```

---

## Final verification

- [ ] **Run the full local gate one more time**

```bash
just ci
```
Expected: all green.

- [ ] **Confirm the version surfaces end-to-end**

```bash
uv run python -m kdive --version          # kdive 0.2.0-dev+g<sha>
just build && unzip -l dist/*.whl | grep kdive/_buildinfo.py   # baked
test ! -f src/kdive/_buildinfo.py && echo "stamp cleaned up"
```

- [ ] **Push the branch and open the PR** (only when the user asks)

```bash
git push -u origin versioning-release-process
gh pr create --base main --title "feat: versioning policy & release process (ADR-0041)" \
  --body "Implements ADR-0041 and its design spec: version.py (SHA + -dev), build-info baking, git-cliff changelog, set-version/release/lock-check recipes, release.yml, RELEASING.md, and the 0.2.0 in-development bump."
```
