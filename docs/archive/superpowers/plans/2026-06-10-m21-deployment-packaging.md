# M2.1 Deployment & Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make kdive deployable by a non-author operator — a single declared `KDIVE_*` config surface with startup validation and a generated reference, an official multi-process container image, reference compose + Helm deployments, a signed GHCR publish pipeline, and retirement of the hand-rolled app bootstrap.

**Architecture:** A new `kdive.config` package becomes the single source of truth for every `KDIVE_*` variable (typed `Setting` declarations aggregated from an explicit module manifest); every `os.environ` read of a `KDIVE_*` var is migrated to it and a structural guard keeps it that way. The three existing entrypoints (`python -m kdive {server|worker|reconciler}`) are packaged in one image, brought up by compose and Helm against external backends, with migrations run as a dedicated one-shot. CI builds on every PR and signs+publishes on tag.

**Tech Stack:** Python 3.13 · uv · pytest · Docker (multi-stage) · docker-compose · Helm 3 · GitHub Actions · cosign/SBOM · stdlib `ast` (config guard).

**Source documents:**
- Spec: [`../specs/2026-06-10-m21-deployment-packaging-design.md`](../specs/2026-06-10-m21-deployment-packaging-design.md)
- ADR-0087 (config registry): [`../../adr/0087-config-registry.md`](../../adr/0087-config-registry.md)
- ADR-0088 (deployment & packaging): [`../../adr/0088-deployment-packaging.md`](../../adr/0088-deployment-packaging.md)
- Milestone: [#13](https://github.com/randomparity/kdive/milestone/13)

---

## How this plan is scoped

M2.1 decomposes into six sequenced phases (= the spec's six sub-issues). Each phase produces working, testable software on its own and, in the project's process, lands as its own `/work-issue` PR. **Phase 1 (config registry) is given full TDD task detail** because it carries the real logic and is the spine everything else reads from. **Phases 2–6 are infrastructure artifacts** (Dockerfile, compose, Helm, CI workflow, removals); each task gives the concrete artifact content plus the executable verification and the tests that genuinely apply, since red-green unit TDD does not map onto a Dockerfile or a workflow file.

**Sequencing (hard edges):** Phase 1 → all. Phase 2 → Phases 3, 4, 5. Phase 6 lands after Phases 2–4.

**Branch:** each phase on its own feature branch off `main` (e.g. `feat/m21-config-registry`), not on the docs branch this plan lives on.

---

## File Structure (whole milestone)

**Phase 1 — config registry**
- Create `src/kdive/config/__init__.py` — public surface: `Setting`, `get`, `load`, `reset`, `validate`, `all_settings`.
- Create `src/kdive/config/registry.py` — `Setting` dataclass + the `Registry` (snapshot, parse, validate).
- Create `src/kdive/config/manifest.py` — the explicit list of setting-bearing module paths.
- Create `src/kdive/config/core_settings.py` — `SETTINGS` for the core (database/objectstore/oidc/http/log/lease/upload/secrets/debug groups).
- Create `scripts/gen_config_reference.py` — pure registry → markdown, mirrors `gen_tool_reference.py`.
- Create `scripts/config_env_guard.py` — stdlib `ast` walk; fails on a `KDIVE_*` env read outside the registry.
- Create `tests/config/test_registry.py`, `tests/config/test_validation.py`, `tests/scripts/test_gen_config_reference.py`, `tests/scripts/test_config_env_guard.py`.
- Create `tests/conftest.py` addition (autouse `reset_config` fixture) — or extend the existing root conftest.
- Modify the ~25 reader modules (listed in Task 1.8) to call `config.get(...)`.
- Modify `src/kdive/providers/remote_libvirt/config.py`, `src/kdive/providers/local_libvirt/discovery.py`, `src/kdive/providers/fault_inject/discovery.py` to expose `SETTINGS`.
- Modify `src/kdive/__main__.py` — call `config.load()` + `config.validate(command)` before dispatch.
- Modify `justfile` — add `config-docs` / `config-docs-check` / `config-guard`; add the latter two to `ci`.
- Create `docs/guide/reference/config.md` (generated, committed).

**Phase 2 — container image**
- Create `Dockerfile`, `.dockerignore`.
- Create `tests/image/test_image_smoke.py` (gated, opt-in).
- Modify `.github/workflows/ci.yml` — add a build-only image job.

**Phase 3 — compose reference**
- Modify `docker-compose.yml` — add `migrate` one-shot + `server`/`worker`/`reconciler` services.
- Create `deploy/compose/README.md`.

**Phase 4 — Helm chart**
- Create `deploy/helm/kdive/` (`Chart.yaml`, `values.yaml`, `templates/*`, `templates/NOTES.txt`).
- Create `tests/helm/test_helm_render.py`.

**Phase 5 — CI publish + provenance**
- Create `.github/workflows/release-image.yml`.

**Phase 6 — retire bootstrap + caller sweep**
- Modify `src/kdive/__main__.py`, `src/kdive/admin/bootstrap.py` — remove `stack`/`run_stack`/`install-compose`/`print-local-env`.
- Modify `justfile`, `scripts/live-stack/start.sh`, `docs/runbooks/live-stack.md`.
- Create `tests/admin/test_bootstrap_retirement.py`.

---

## Phase 1 — Central configuration registry (ADR-0087)

### Task 1.1: `Setting` descriptor + minimal registry

**Files:**
- Create: `src/kdive/config/registry.py`
- Create: `src/kdive/config/__init__.py`
- Test: `tests/config/test_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_registry.py
from __future__ import annotations

import pytest

from kdive.config import Setting, Registry
from kdive.domain.errors import CategorizedError, ErrorCategory


def _int(raw: str) -> int:
    return int(raw)


def test_get_returns_parsed_value_from_snapshot() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000", group="http")
    reg = Registry([s])
    reg.load({"KDIVE_HTTP_PORT": "9001"})
    assert reg.get(s) == 9001


def test_get_returns_parsed_default_when_absent() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000", group="http")
    reg = Registry([s])
    reg.load({})
    assert reg.get(s) == 8000


def test_get_raises_configuration_error_on_unparseable_value() -> None:
    s = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000", group="http")
    reg = Registry([s])
    reg.load({"KDIVE_HTTP_PORT": "not-a-number"})
    with pytest.raises(CategorizedError) as ei:
        reg.get(s)
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["variable"] == "KDIVE_HTTP_PORT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_registry.py -q`
Expected: FAIL with `ImportError: cannot import name 'Setting' from 'kdive.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/config/registry.py
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from kdive.domain.errors import CategorizedError, ErrorCategory

_RUNNABLE = frozenset({"server", "worker", "reconciler", "migrate"})


@dataclass(frozen=True, slots=True)
class Setting:
    """One declared `KDIVE_*` variable. The registry's atom of truth."""

    name: str
    parse: Callable[[str], object]
    default: str | None = None
    secret: bool = False
    processes: frozenset[str] = field(default_factory=frozenset)
    group: str = "core"
    help: str = ""
    suggest: str = ""
    required_when: Callable[[Mapping[str, str]], bool] = lambda env: False

    def __post_init__(self) -> None:
        unknown = self.processes - _RUNNABLE
        if unknown:
            raise ValueError(f"{self.name}: unknown processes {sorted(unknown)}")


class Registry:
    """Holds the declared settings and resolves them against a snapshot."""

    def __init__(self, settings: Sequence[Setting]) -> None:
        self._settings: tuple[Setting, ...] = tuple(settings)
        by_name: dict[str, Setting] = {}
        for s in self._settings:
            if s.name in by_name:
                raise ValueError(f"duplicate setting {s.name}")
            by_name[s.name] = s
        self._by_name = by_name
        self._snapshot: dict[str, str] | None = None

    def load(self, env: Mapping[str, str]) -> None:
        self._snapshot = {k: v for k, v in env.items() if k.startswith("KDIVE_")}

    def reset(self) -> None:
        self._snapshot = None

    def _env(self) -> dict[str, str]:
        if self._snapshot is None:
            import os

            self.load(os.environ)
        assert self._snapshot is not None
        return self._snapshot

    def all_settings(self) -> tuple[Setting, ...]:
        return self._settings

    def get(self, setting: Setting) -> object:
        raw = self._env().get(setting.name, setting.default)
        if raw is None:
            return None
        try:
            return setting.parse(raw)
        except (ValueError, TypeError) as exc:
            raise CategorizedError(
                f"{setting.name}: cannot parse {raw!r} ({exc})",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"variable": setting.name, "suggest": setting.suggest},
            ) from exc
```

```python
# src/kdive/config/__init__.py
from __future__ import annotations

from kdive.config.registry import Registry, Setting

__all__ = ["Registry", "Setting"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_registry.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kdive/config/ tests/config/test_registry.py
git commit -m "feat(config): Setting descriptor + snapshot-resolving Registry"
```

### Task 1.2: `required_when` validation with the two-time contract

**Files:**
- Modify: `src/kdive/config/registry.py`
- Test: `tests/config/test_validation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_validation.py
from __future__ import annotations

import pytest

from kdive.config import Registry, Setting
from kdive.domain.errors import CategorizedError, ErrorCategory


def _str(raw: str) -> str:
    return raw


def _uri_set(env) -> bool:
    return bool(env.get("KDIVE_REMOTE_LIBVIRT_URI"))


URI = Setting(name="KDIVE_REMOTE_LIBVIRT_URI", parse=_str, group="remote-libvirt",
              processes=frozenset({"worker", "reconciler"}))
CA = Setting(name="KDIVE_REMOTE_LIBVIRT_CA_CERT_REF", parse=_str, secret=True,
             group="remote-libvirt", processes=frozenset({"worker", "reconciler"}),
             required_when=_uri_set, suggest="set the CA cert secret ref")


def test_required_when_false_does_not_require_optional_provider_setting() -> None:
    reg = Registry([URI, CA])
    reg.load({})  # remote-libvirt not enabled
    reg.validate("worker")  # must not raise


def test_required_when_true_requires_the_setting() -> None:
    reg = Registry([URI, CA])
    reg.load({"KDIVE_REMOTE_LIBVIRT_URI": "qemu+tls://host/system"})
    with pytest.raises(CategorizedError) as ei:
        reg.validate("worker")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF" in str(ei.value)


def test_validate_only_checks_settings_for_the_role() -> None:
    reg = Registry([URI, CA])
    reg.load({"KDIVE_REMOTE_LIBVIRT_URI": "qemu+tls://host/system"})
    reg.validate("server")  # server does not consume these → no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_validation.py -q`
Expected: FAIL with `AttributeError: 'Registry' object has no attribute 'validate'`

- [ ] **Step 3: Write minimal implementation** — add to `Registry`:

```python
    def validate(self, process: str) -> None:
        """Fail fast on settings this process requires that are missing/malformed.

        Startup time (a): settings whose `required_when` holds for the snapshot and
        whose `processes` include `process`. Parse errors surface here too.
        """
        env = self._env()
        missing: list[str] = []
        for s in self._settings:
            if process not in s.processes:
                continue
            present = s.name in env or s.default is not None
            if s.required_when(env) and not present:
                missing.append(s.name)
                continue
            if s.name in env:
                self.get(s)  # raises CONFIGURATION_ERROR on a malformed value
        if missing:
            lines = "\n".join(
                f"  - {n}: {self._by_name[n].suggest or 'required for this process'}"
                for n in missing
            )
            raise CategorizedError(
                f"missing required configuration for {process}:\n{lines}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"process": process, "missing": missing},
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_validation.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kdive/config/registry.py tests/config/test_validation.py
git commit -m "feat(config): required_when + per-process startup validation"
```

### Task 1.3: Module-public API + autouse test-reset fixture (cache isolation)

**Files:**
- Modify: `src/kdive/config/__init__.py`, `src/kdive/config/manifest.py` (create), `src/kdive/config/core_settings.py` (create)
- Modify: `tests/conftest.py` (create if absent)
- Test: `tests/config/test_cache_isolation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_cache_isolation.py
from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import HTTP_PORT


def test_setenv_is_honored_across_tests_first(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_HTTP_PORT", "9101")
    config.load()
    assert config.get(HTTP_PORT) == 9101


def test_setenv_is_honored_across_tests_second(monkeypatch) -> None:
    # Without a reset seam this would still see 9101 from the previous test.
    monkeypatch.setenv("KDIVE_HTTP_PORT", "9202")
    config.load()
    assert config.get(HTTP_PORT) == 9202
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_cache_isolation.py -q`
Expected: FAIL with `ImportError: cannot import name 'HTTP_PORT'` (and `config.get`/`config.load` undefined)

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/config/core_settings.py
from __future__ import annotations

from kdive.config.registry import Setting


def _int(raw: str) -> int:
    return int(raw)


def _str(raw: str) -> str:
    return raw


_ALL = frozenset({"server", "worker", "reconciler", "migrate"})

DATABASE_URL = Setting(
    name="KDIVE_DATABASE_URL", parse=_str, group="database", processes=_ALL,
    required_when=lambda env: True,
    help="Postgres DSN for the system-of-record.",
    suggest="e.g. postgresql://kdive:kdive@postgres:5432/kdive",  # pragma: allowlist secret — example DSN
)
HTTP_HOST = Setting(name="KDIVE_HTTP_HOST", parse=_str, default="127.0.0.1",
                    group="http", processes=frozenset({"server"}),
                    help="Bind host for the MCP server.")
HTTP_PORT = Setting(name="KDIVE_HTTP_PORT", parse=_int, default="8000",
                    group="http", processes=frozenset({"server"}),
                    help="Bind port for the MCP server.")
LOG_LEVEL = Setting(name="KDIVE_LOG_LEVEL", parse=_str, default="INFO",
                    group="logging", processes=_ALL,
                    help="Structured-logging level.")

SETTINGS = [DATABASE_URL, HTTP_HOST, HTTP_PORT, LOG_LEVEL]
```

```python
# src/kdive/config/manifest.py
from __future__ import annotations

# Explicit list of every module exposing a `SETTINGS` list. The registry
# force-loads these so the full set is available regardless of which provider a
# process enabled (ADR-0087). Provider modules are added here as Phase-1 migration
# reaches them; a new provider adds one line. This file is outside the M2
# portability gate's CORE_PREFIXES, so that addition is not a gated core touch.
SETTING_MODULES: tuple[str, ...] = (
    "kdive.config.core_settings",
    # "kdive.providers.remote_libvirt.config",   # added in Task 1.8
    # "kdive.providers.local_libvirt.discovery",
    # "kdive.providers.fault_inject.discovery",
)
```

```python
# src/kdive/config/__init__.py
from __future__ import annotations

import importlib
import os
from collections.abc import Mapping

from kdive.config.manifest import SETTING_MODULES
from kdive.config.registry import Registry, Setting

__all__ = ["Registry", "Setting", "get", "load", "reset", "validate", "all_settings"]


def _build_registry() -> Registry:
    settings: list[Setting] = []
    for path in SETTING_MODULES:
        mod = importlib.import_module(path)
        settings.extend(getattr(mod, "SETTINGS"))
    return Registry(settings)


_REGISTRY = _build_registry()


def load(env: Mapping[str, str] | None = None) -> None:
    _REGISTRY.load(os.environ if env is None else env)


def reset() -> None:
    _REGISTRY.reset()


def get(setting: Setting) -> object:
    return _REGISTRY.get(setting)


def validate(process: str) -> None:
    _REGISTRY.validate(process)


def all_settings() -> tuple[Setting, ...]:
    return _REGISTRY.all_settings()
```

```python
# tests/conftest.py  (create; if one exists, add only the fixture)
from __future__ import annotations

import pytest

import kdive.config as config


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Clear the config snapshot before each test so per-case monkeypatch.setenv
    is honored (ADR-0087 scoped-not-permanent cache)."""
    config.reset()
    yield
    config.reset()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_cache_isolation.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kdive/config/ tests/config/test_cache_isolation.py tests/conftest.py
git commit -m "feat(config): public API, core settings, manifest, test reset seam"
```

### Task 1.4: Generated reference + drift test

**Files:**
- Create: `scripts/gen_config_reference.py`
- Create: `docs/guide/reference/config.md` (generated)
- Test: `tests/scripts/test_gen_config_reference.py`
- Modify: `justfile`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_gen_config_reference.py
from __future__ import annotations

from kdive.config.registry import Setting
from scripts.gen_config_reference import render


def _str(raw: str) -> str:
    return raw


def test_render_groups_and_redacts_secrets() -> None:
    settings = [
        Setting(name="KDIVE_DATABASE_URL", parse=_str, group="database",
                processes=frozenset({"server"}), help="DSN."),
        Setting(name="KDIVE_REMOTE_LIBVIRT_CA_CERT_REF", parse=_str, secret=True,
                group="remote-libvirt", processes=frozenset({"worker"}), help="CA ref."),
    ]
    out = render(settings)
    assert "## database" in out
    assert "KDIVE_DATABASE_URL" in out
    assert "secret (ref only)" in out          # the secret marker
    assert "do not edit" in out                 # generated-file header
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_gen_config_reference.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.gen_config_reference'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/gen_config_reference.py
"""Generate docs/guide/reference/config.md from the config registry (ADR-0087).

Pure core (`render`) over the Setting list; the CLI writes the file. Mirrors
scripts/gen_tool_reference.py. Run via `just config-docs` / `just config-docs-check`.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from kdive.config import all_settings
from kdive.config.registry import Setting

_HEADER = "<!-- generated by scripts/gen_config_reference.py; do not edit. Regenerate: just config-docs -->"
_OUT = Path(__file__).resolve().parents[1] / "docs" / "guide" / "reference" / "config.md"


def render(settings: Sequence[Setting]) -> str:
    by_group: dict[str, list[Setting]] = {}
    for s in settings:
        by_group.setdefault(s.group, []).append(s)
    lines = [_HEADER, "", "# Configuration reference (`KDIVE_*`)", ""]
    for group in sorted(by_group):
        lines.append(f"## {group}")
        lines.append("")
        lines.append("| Variable | Processes | Default | Required | Value |")
        lines.append("|----------|-----------|---------|----------|-------|")
        for s in sorted(by_group[group], key=lambda x: x.name):
            procs = ", ".join(sorted(s.processes)) or "—"
            default = "—" if s.default is None else f"`{s.default}`"
            required = "conditional" if s.required_when({}) is False else "yes"
            value = "secret (ref only)" if s.secret else s.help or "—"
            lines.append(f"| `{s.name}` | {procs} | {default} | {required} | {value} |")
        lines.append("")
    return "\n".join(lines)


def write_reference(out: Path = _OUT) -> None:
    out.write_text(render(all_settings()) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_reference()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_gen_config_reference.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Generate the committed reference + add justfile recipes**

Run: `uv run python scripts/gen_config_reference.py`

Add to `justfile` (after the existing `docs-check` recipe):

```make
config-docs:
    uv run python scripts/gen_config_reference.py

config-docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp)"
    uv run python -c "from scripts.gen_config_reference import render; from kdive.config import all_settings; open('$tmp','w').write(render(all_settings())+'\n')"
    if ! diff -u docs/guide/reference/config.md "$tmp"; then
        echo "config reference is stale — run 'just config-docs' and commit" >&2
        exit 1
    fi
```

Add `config-docs-check` to the `ci` recipe's dependency list **and** add it as its own
step in `.github/workflows/ci.yml`, right after the existing `just docs-check` step:

```yaml
      - name: config reference up to date
        run: just config-docs-check
```

CI invokes recipes individually (the `ci.yml` has separate `just lint` / `just docs-check`
/ `just test` steps; `just ci` is not what runs in CI), so adding it only to the `ci` recipe
would **not** gate PRs — the committed `config.md` could go stale unnoticed.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_config_reference.py docs/guide/reference/config.md tests/scripts/test_gen_config_reference.py justfile
git commit -m "feat(config): generated config reference + drift check"
```

### Task 1.5: Structural drift guard (stdlib `ast`)

**Files:**
- Create: `scripts/config_env_guard.py`
- Test: `tests/scripts/test_config_env_guard.py`
- Modify: `justfile`

The guard realizes ADR-0087's "structural rule over the access form" with a stdlib `ast` walk (no external tool needed in CI, matching the `m2_portability_gate.py` pattern). It flags `os.environ.get("KDIVE_…")`, `os.environ["KDIVE_…"]`, and `os.getenv("KDIVE_…")` outside an allowlist (`src/kdive/config/`, plus a shrinking list of not-yet-migrated files during Task 1.8).

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_config_env_guard.py
from __future__ import annotations

from pathlib import Path

from scripts.config_env_guard import find_violations


def test_flags_each_access_form(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text(
        "import os\n"
        "a = os.environ.get('KDIVE_X')\n"
        "b = os.environ['KDIVE_Y']\n"
        "c = os.getenv('KDIVE_Z')\n"
    )
    hits = find_violations([f], allowlist=set())
    assert {v.variable for v in hits} == {"KDIVE_X", "KDIVE_Y", "KDIVE_Z"}


def test_allowlisted_file_is_skipped(tmp_path: Path) -> None:
    f = tmp_path / "ok.py"
    f.write_text("import os\nx = os.environ.get('KDIVE_X')\n")
    assert find_violations([f], allowlist={f}) == []


def test_non_kdive_reads_are_ignored(tmp_path: Path) -> None:
    f = tmp_path / "fine.py"
    f.write_text("import os\nx = os.environ.get('HOME')\n")
    assert find_violations([f], allowlist=set()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_config_env_guard.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.config_env_guard'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/config_env_guard.py
"""Structural guard: no KDIVE_* env read outside kdive.config (ADR-0087).

Stdlib-only `ast` walk over the source tree so CI runs it without a synced env
(`just config-guard`). Catches os.environ.get(...), os.environ[...], os.getenv(...).
Exit 0 clean, 1 on violations.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "kdive"
_ALLOW_DIR = _SRC / "config"
# Shrinking allowlist of files not yet migrated (Task 1.8). Must reach empty.
_NOT_YET_MIGRATED: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Violation:
    file: Path
    line: int
    variable: str


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _env_var(call_or_sub: ast.AST) -> str | None:
    # os.environ.get("KDIVE_X") / os.getenv("KDIVE_X")
    if isinstance(call_or_sub, ast.Call) and isinstance(call_or_sub.func, ast.Attribute):
        attr = call_or_sub.func
        is_get = attr.attr == "get" and isinstance(attr.value, ast.Attribute) and attr.value.attr == "environ"
        is_getenv = attr.attr == "getenv"
        if (is_get or is_getenv) and call_or_sub.args:
            return _literal(call_or_sub.args[0])
    # os.environ["KDIVE_X"]
    if isinstance(call_or_sub, ast.Subscript) and isinstance(call_or_sub.value, ast.Attribute):
        if call_or_sub.value.attr == "environ":
            return _literal(call_or_sub.slice)
    return None


def find_violations(files: list[Path], allowlist: set[Path]) -> list[Violation]:
    out: list[Violation] = []
    for f in files:
        if f in allowlist:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            var = _env_var(node)
            if var and var.startswith("KDIVE_"):
                out.append(Violation(f, getattr(node, "lineno", 0), var))
    return out


def main() -> int:
    allow = {_ALLOW_DIR / "*"}  # directory check below
    files = [p for p in _SRC.rglob("*.py")]
    allowlist = {p for p in files if _ALLOW_DIR in p.parents or p.name in _NOT_YET_MIGRATED}
    violations = find_violations(files, allowlist)
    for v in violations:
        rel = v.file.relative_to(_ROOT)
        print(f"{rel}:{v.line}: {v.variable} read outside kdive.config", file=sys.stderr)
    if violations:
        print(f"{len(violations)} stray KDIVE_* env read(s); route through kdive.config", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_config_env_guard.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Wire it (do NOT add to `ci` yet — the tree still has stray reads until Task 1.8)**

Add to `justfile`:

```make
config-guard:
    uv run python scripts/config_env_guard.py
```

- [ ] **Step 6: Commit**

```bash
git add scripts/config_env_guard.py tests/scripts/test_config_env_guard.py justfile
git commit -m "feat(config): structural KDIVE_* drift guard (ast, not yet gating)"
```

### Task 1.6: Provider `SETTINGS` + manifest wiring

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/config.py`, `src/kdive/providers/local_libvirt/discovery.py`, `src/kdive/providers/fault_inject/discovery.py`
- Modify: `src/kdive/config/manifest.py`
- Test: `tests/config/test_manifest_completeness.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_manifest_completeness.py
from __future__ import annotations

import kdive.config as config


def test_remote_libvirt_settings_present_without_enabling_them() -> None:
    # A server process never imports the remote provider, but the manifest
    # force-loads it, so its settings are visible (completeness).
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_REMOTE_LIBVIRT_URI" in names
    assert "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF" in names
    assert "KDIVE_FAULT_INJECT_SEED" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_manifest_completeness.py -q`
Expected: FAIL (names absent — modules not on the manifest)

- [ ] **Step 3: Write minimal implementation** — add a `SETTINGS` list to each provider config module declaring its existing vars (parse/default mirror the current constants), e.g. in `remote_libvirt/config.py`:

```python
from kdive.config.registry import Setting

_RT = frozenset({"worker", "reconciler"})


def _uri_set(env) -> bool:
    return bool(env.get("KDIVE_REMOTE_LIBVIRT_URI"))


SETTINGS = [
    Setting(name="KDIVE_REMOTE_LIBVIRT_URI", parse=str, group="remote-libvirt",
            processes=_RT, help="qemu+tls host URI; presence enables the provider."),
    Setting(name="KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF", parse=str, secret=True,
            group="remote-libvirt", processes=_RT, required_when=_uri_set,
            suggest="client cert secret ref"),
    Setting(name="KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF", parse=str, secret=True,
            group="remote-libvirt", processes=_RT, required_when=_uri_set,
            suggest="client key secret ref"),
    Setting(name="KDIVE_REMOTE_LIBVIRT_CA_CERT_REF", parse=str, secret=True,
            group="remote-libvirt", processes=_RT, required_when=_uri_set,
            suggest="CA cert secret ref"),
    # storage-pool, network, machine, gdb addr/port-range: parse=str/int, not required_when.
]
```

Declare `SETTINGS` similarly in `local_libvirt/discovery.py` (`KDIVE_LIBVIRT_URI`, `KDIVE_LIBVIRT_ALLOCATION_CAP`, `KDIVE_METADATA_NS`) and `fault_inject/discovery.py` (`KDIVE_FAULT_INJECT*`). Then uncomment the three module paths in `src/kdive/config/manifest.py::SETTING_MODULES`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_manifest_completeness.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/*/config.py src/kdive/providers/*/discovery.py src/kdive/config/manifest.py tests/config/test_manifest_completeness.py
git commit -m "feat(config): provider SETTINGS co-location + manifest aggregation"
```

### Task 1.7: Startup `load()` + `validate()` in entrypoints (incl. logging bootstrap)

**Files:**
- Modify: `src/kdive/__main__.py:150-203`
- Test: `tests/config/test_entrypoint_validation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_entrypoint_validation.py
from __future__ import annotations

import pytest

from kdive.__main__ import main
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_server_without_database_url_fails_fast(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_DATABASE_URL", raising=False)
    with pytest.raises(CategorizedError) as ei:
        main(["server"])
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_DATABASE_URL" in str(ei.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_entrypoint_validation.py -q`
Expected: FAIL (currently a server start path / pool error, not a CONFIGURATION_ERROR before binding)

- [ ] **Step 3: Write minimal implementation** — the order is load-bearing. In `main()`, the
sequence is: parse args → `config.load()` → resolve the log level via the registry → configure
logging → validate → dispatch. `config.load()` must come **before** anything reads a setting,
including the logging bootstrap (ADR-0087 decision 4's early bootstrap phase):

```python
    import kdive.config as config
    from kdive.config.core_settings import LOG_LEVEL

    args = build_parser().parse_args(argv)
    config.load()                                   # snapshot first — before any get()
    level = args.log_level or config.get(LOG_LEVEL)  # --log-level flag wins when passed
    configure_logging(level, secret_registry=secret_registry)
    if args.command in {"server", "worker", "reconciler", "migrate"}:
        config.validate(args.command)
    # ... existing dispatch ...
```

Drop the argparse default `os.environ.get("KDIVE_LOG_LEVEL", "INFO")` (change the `--log-level`
arg to `default=None`) so the only `KDIVE_LOG_LEVEL` read is `config.get(LOG_LEVEL)`; the flag,
when passed, overrides it.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_entrypoint_validation.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kdive/__main__.py tests/config/test_entrypoint_validation.py
git commit -m "feat(config): fail-fast startup validation in entrypoints"
```

### Task 1.8: Migrate all readers + activate the guard (atomic)

**Files (each: replace `os.environ` read with `config.get(<SETTING>)`):**
- `src/kdive/db/pool.py` (DATABASE_URL) · `src/kdive/mcp/auth.py` (OIDC_*) · `src/kdive/store/objectstore.py` (S3_*) · `src/kdive/domain/lease.py`, `src/kdive/services/allocation/renew.py`, `src/kdive/mcp/tools/lifecycle/allocations.py` (LEASE_*) · `src/kdive/mcp/tools/catalog/artifacts_uploads.py` (MAX_UPLOAD_BYTES, UPLOAD_TTL_SECONDS) · `src/kdive/mcp/tools/debug/ops.py` (DEBUG_DIR) · `src/kdive/security/secrets/secrets.py` (SECRETS_ROOT) · `src/kdive/prereqs/managed_ssh_key.py` (SSH_KEY_DIR, ROOTFS_AUTHORIZED_KEY) · `src/kdive/provider_components/catalog.py` (FIXTURE_CATALOG_PATH) · `src/kdive/providers/local_libvirt/**` and `remote_libvirt/**` and `fault_inject/**` (their provider vars) · `src/kdive/providers/composition.py` · `src/kdive/admin/bootstrap.py` (the `os.environ["KDIVE_DATABASE_URL"]` subscript).
- Modify: `scripts/config_env_guard.py` (`_NOT_YET_MIGRATED` → empty), `justfile` (add `config-guard` to `ci`), `.github/workflows/ci.yml`.
- Test: existing suites for each module (no behavior change).

> **Procedure (do this incrementally to keep CI green):** add each file to `_NOT_YET_MIGRATED` first, migrate it, run that module's tests, then remove it from the set. Activate the gate only when the set is empty.

- [ ] **Step 1: Run the guard to enumerate the work**

Run: `uv run python scripts/config_env_guard.py`
Expected: prints every stray `KDIVE_*` read (the migration worklist), exit 1.

- [ ] **Step 2: Migrate one module (example: `db/pool.py`)** — declare/reuse `DATABASE_URL` and read `config.get(DATABASE_URL)` where it currently reads `os.environ`. Run its tests:

Run: `uv run pytest tests/db/test_pool.py -q`
Expected: PASS (unchanged behavior; `config.load()` happens in the test reset fixture / explicit `config.load()`).

- [ ] **Step 3: Repeat for every file above**, running the relevant `tests/<area>/...` after each. Re-run the guard between modules:

Run: `uv run python scripts/config_env_guard.py`
Expected: shrinking violation count → 0.

- [ ] **Step 4: Activate the gate** — set `_NOT_YET_MIGRATED = frozenset()`, add `config-guard` to the `ci` recipe, and add a `config-guard` step to `.github/workflows/ci.yml` (CI runs recipes individually — the `ci` recipe alone does not gate PRs).

- [ ] **Step 5: Full verification**

Run: `uv run python scripts/config_env_guard.py && just lint && just type && uv run pytest -q`
Expected: guard exit 0; lint/type clean; all tests pass.

- [ ] **Step 6: Regenerate the reference (now complete) + commit**

```bash
uv run python scripts/gen_config_reference.py
git add -A
git commit -m "refactor(config): migrate all KDIVE_* reads to the registry; activate guard"
```

**Phase 1 acceptance:** `config-guard` exit 0; a missing required var fails the matching process with a named `configuration_error`; `config-docs-check` clean; full suite green.

---

## Phase 2 — Container image (ADR-0088)

### Task 2.1: Multi-stage Dockerfile + `.dockerignore`

**Files:**
- Create: `Dockerfile`, `.dockerignore`

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
# syntax=docker/dockerfile:1
# Builder: resolve the uv environment.
FROM python:3.13-slim-bookworm@sha256:<pin-digest> AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Final: slim base + worker toolchain (drives remote-libvirt over the network).
FROM python:3.13-slim-bookworm@sha256:<pin-digest>
# These are all real bookworm packages. drgn is NOT installed via apt — the project
# obtains drgn from Fedora today (virt-builder), and bookworm only ships the
# `python3-drgn` library, whose CLI/version is unproven for our `drgn --version`
# check; we install drgn from its pinned PyPI manylinux wheel instead (below).
# libelf1/libdw1/zlib1g are drgn's runtime shared libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc make binutils gdb libvirt-clients openssh-client \
      libelf1 libdw1 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
# Put the venv on PATH BEFORE the drgn install + verification, so the bare `drgn` check resolves.
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src \
    KDIVE_BUILD_WORKSPACE=/var/lib/kdive/build \
    KDIVE_INSTALL_STAGING=/var/lib/kdive/install
# drgn from its prebuilt wheel into the same venv (so `drgn` CLI + `import drgn` both work).
RUN uv pip install --python /opt/venv "drgn==<pin-drgn-version>"
# Fail the BUILD (not just the gated test) if any worker tool is missing or broken.
RUN drgn --version && gdb --version && virsh --version && gcc --version && make --version
RUN useradd --system --create-home --uid 10001 kdive \
    && mkdir -p /var/lib/kdive/build /var/lib/kdive/install \
    && chown -R kdive:kdive /var/lib/kdive
USER kdive
WORKDIR /app
ENTRYPOINT ["python", "-m", "kdive"]
CMD ["server"]
```

> Pin both `<pin-digest>` values to the current `python:3.13-slim-bookworm` digest (`docker buildx imagetools inspect python:3.13-slim-bookworm`). Pin `<pin-drgn-version>` to the latest drgn release with a cp313 manylinux wheel (`pip index versions drgn`, or check PyPI). The writable volumes (build/install/ssh/debug/crash) are mounted by compose/Helm; the image only pre-creates and chowns the build/install dirs. **If drgn has no cp313 wheel at pin time**, fall back to a Fedora base (matching the proven `virt-builder --install drgn` path) rather than building drgn from source in-image.

- [ ] **Step 2: Write `.dockerignore`**

```gitignore
.git
.venv
**/__pycache__
tests
docs
deploy
# Do NOT exclude *.md at the root: pyproject declares `readme = "README.md"`, which the
# uv_build backend reads during `uv sync` — excluding it breaks the project install.
AGENTS.md
CLAUDE.md
```

- [ ] **Step 3: Build it**

Run: `docker build -t kdive:dev .`
Expected: build succeeds.

- [ ] **Step 4: Verify each command starts and the toolchain resolves**

Run:
```bash
docker run --rm kdive:dev --help
for t in drgn gdb virsh ssh gcc make; do docker run --rm --entrypoint "$t" kdive:dev --version >/dev/null && echo "$t ok"; done
```
Expected: `--help` prints the subcommands; each tool prints a version (`ok`).

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "feat(image): multi-stage worker-toolchain image, non-root, KDIVE_* only"
```

### Task 2.2: Image smoke test (gated) + PR build job

**Files:**
- Create: `tests/image/test_image_smoke.py`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the gated smoke test**

```python
# tests/image/test_image_smoke.py
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KDIVE_IMAGE") is None or shutil.which("docker") is None,
    reason="set KDIVE_IMAGE and have docker to run the image smoke test",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "run", "--rm", *args], capture_output=True, text=True)


def test_entrypoint_lists_subcommands() -> None:
    img = os.environ["KDIVE_IMAGE"]
    res = _run(img, "--help")
    assert res.returncode == 0
    for cmd in ("server", "worker", "reconciler", "migrate"):
        assert cmd in res.stdout


def test_worker_toolchain_on_path() -> None:
    img = os.environ["KDIVE_IMAGE"]
    for tool in ("drgn", "gdb", "virsh"):
        res = _run("--entrypoint", tool, img, "--version")
        assert res.returncode == 0, f"{tool} missing: {res.stderr}"
```

- [ ] **Step 2: Run it locally against the dev image**

Run: `KDIVE_IMAGE=kdive:dev uv run pytest tests/image/test_image_smoke.py -q`
Expected: PASS (2 passed)

- [ ] **Step 3: Add a build-only CI job** to `.github/workflows/ci.yml`:

```yaml
  image-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>  # vX.Y.Z
        with: { persist-credentials: false }
      - uses: docker/setup-buildx-action@<sha>  # vX.Y.Z
      - uses: docker/build-push-action@<sha>  # vX.Y.Z
        with:
          context: .
          push: false
          load: true
          tags: kdive:ci
      - uses: astral-sh/setup-uv@<sha>  # vX.Y.Z
      - name: image smoke test
        run: uv run --no-project --with pytest pytest tests/image/test_image_smoke.py -q
        env:
          KDIVE_IMAGE: kdive:ci
```

> The job never runs `uv sync`, so `pytest` is not otherwise on PATH — run it through `uv` (the smoke test imports only stdlib + pytest and shells `docker run`, so `--no-project --with pytest` is enough, no full sync needed). Resolve each `<sha>` to the current release of the action (`gh api repos/<action>/releases/latest`); add the version comment. Run `zizmor .github/workflows/ci.yml` before committing.

- [ ] **Step 4: Commit**

```bash
git add tests/image/test_image_smoke.py .github/workflows/ci.yml
git commit -m "feat(image): gated smoke test + PR build-only CI job"
```

**Phase 2 acceptance:** `docker build` succeeds; all four commands start from the image; the smoke test passes; the PR build job is green.

---

## Phase 3 — Compose reference (ADR-0088)

### Task 3.1: App-tier compose with migrate one-shot

**Files:**
- Modify: `docker-compose.yml`
- Create: `deploy/compose/README.md`

- [ ] **Step 1: Add the migrate one-shot + app services** to `docker-compose.yml` (the backend services stay):

```yaml
# Top-level extension field defining the shared backend env ONCE. Every service
# merges it with `<<: *backends`; server adds its own HTTP vars on top.
x-backends: &backends
  KDIVE_DATABASE_URL: postgresql://kdive:kdive@postgres:5432/kdive  # pragma: allowlist secret — local dev only
  KDIVE_OIDC_ISSUER: http://oidc:8080/default
  KDIVE_OIDC_JWKS_URI: http://oidc:8080/default/jwks
  KDIVE_OIDC_AUDIENCE: kdive
  KDIVE_S3_ENDPOINT_URL: http://minio:9000
  KDIVE_S3_BUCKET: kdive-artifacts
  KDIVE_S3_REGION: us-east-1

services:
  migrate:
    build: .
    image: kdive:dev
    command: ["migrate"]
    environment:
      <<: *backends
    depends_on:
      postgres:
        condition: service_healthy

  server:
    image: kdive:dev
    command: ["server"]
    environment:
      <<: *backends
      KDIVE_HTTP_HOST: 0.0.0.0
      KDIVE_HTTP_PORT: "8000"
    ports: ["8000:8000"]
    depends_on:
      migrate:
        condition: service_completed_successfully

  worker:
    image: kdive:dev
    command: ["worker"]
    environment:
      <<: *backends
    volumes:
      - kdive-build:/var/lib/kdive/build
      - kdive-install:/var/lib/kdive/install
    depends_on:
      migrate:
        condition: service_completed_successfully

  reconciler:
    image: kdive:dev
    command: ["reconciler"]
    environment:
      <<: *backends
    depends_on:
      migrate:
        condition: service_completed_successfully

volumes:
  kdive-build:
  kdive-install:
```

> The `x-backends: &backends` extension field defines the shared env once; the `<<: *backends`
> merge key pulls it into each service (server adds HTTP vars on top). The
> `service_completed_successfully` condition is the ADR-0088 ordering fix. Merge these
> `services:`/`volumes:` keys into the existing `docker-compose.yml`, which already has a
> top-level `services:` block for the backends — do not introduce a second one.

- [ ] **Step 2: Validate the compose file**

Run: `docker compose config -q`
Expected: no output, exit 0 (valid).

- [ ] **Step 3: Bring the app tier up and verify ordering**

Run:
```bash
docker build -t kdive:dev .
docker compose up -d --wait postgres minio oidc
docker compose run --rm minio-init
docker compose up -d migrate
docker compose up -d server
docker compose logs migrate   # exits 0 before server accepts
curl -sf -o /dev/null http://localhost:8000/mcp && echo "server up"
```
Expected: migrate exits 0; `server up`.

- [ ] **Step 4: Write `deploy/compose/README.md`** — the bring-up sequence above plus the minted-token note (point at the existing live-stack runbook for token issuance).

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml deploy/compose/README.md
git commit -m "feat(compose): app-tier services + migrate one-shot over backends"
```

**Phase 3 acceptance:** `docker compose config -q` valid; migrate exits 0 before the server accepts connections; server liveness green.

---

## Phase 4 — Helm chart (ADR-0088)

### Task 4.1: Chart skeleton + app tier + migrate Job + demo gate

**Files:**
- Create: `deploy/helm/kdive/{Chart.yaml,values.yaml,templates/_helpers.tpl,templates/configmap.yaml,templates/deployment-server.yaml,templates/deployment-worker.yaml,templates/deployment-reconciler.yaml,templates/service.yaml,templates/job-migrate.yaml,templates/NOTES.txt}`

- [ ] **Step 1: Author `Chart.yaml` + `values.yaml`**

```yaml
# Chart.yaml
apiVersion: v2
name: kdive
description: kdive control plane (server/worker/reconciler)
type: application
version: 0.1.0
appVersion: "0.3.0"
dependencies:
  - name: postgresql
    version: <pin>
    repository: https://charts.bitnami.com/bitnami
    condition: bundledBackends
  - name: minio
    version: <pin>
    repository: https://charts.bitnami.com/bitnami
    condition: bundledBackends
```

```yaml
# values.yaml
image:
  repository: ghcr.io/randomparity/kdive
  tag: ""            # defaults to appVersion
  digest: ""         # set to pin by digest
bundledBackends: false
demoAcknowledged: false   # required true when bundledBackends is true
config:
  KDIVE_DATABASE_URL: ""
  KDIVE_OIDC_ISSUER: ""
  KDIVE_OIDC_JWKS_URI: ""
  KDIVE_OIDC_AUDIENCE: kdive
  KDIVE_S3_ENDPOINT_URL: ""
  KDIVE_S3_BUCKET: kdive-artifacts
  KDIVE_S3_REGION: us-east-1
server:
  replicas: 1
worker:
  replicas: 1
  persistence:
    build: { size: 10Gi }
    install: { size: 5Gi }
```

- [ ] **Step 2: Add helpers + the demo-acknowledged render gate** to `templates/_helpers.tpl`:

```yaml
{{- define "kdive.fullname" -}}{{ .Release.Name }}-kdive{{- end -}}
{{- define "kdive.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.digest -}}{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}{{ .Values.image.repository }}:{{ $tag }}{{- end -}}
{{- end -}}
{{- if and .Values.bundledBackends (not .Values.demoAcknowledged) -}}
{{- fail "bundledBackends is ephemeral/demo-only: set demoAcknowledged=true to use it (data is NOT durable)" -}}
{{- end -}}
```

- [ ] **Step 3: Author the migrate Job** (`templates/job-migrate.yaml`) — external backends use a `pre-install,pre-upgrade` hook; the demo path runs after the bundled DB:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "kdive.fullname" . }}-migrate
  annotations:
    {{- if .Values.bundledBackends }}
    "helm.sh/hook": post-install,post-upgrade   # after the bundled DB exists
    {{- else }}
    "helm.sh/hook": pre-install,pre-upgrade      # external backend pre-exists
    {{- end }}
    "helm.sh/hook-weight": "0"
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: {{ include "kdive.image" . }}
          args: ["migrate"]
          envFrom: [{ configMapRef: { name: {{ include "kdive.fullname" . }}-config } }]
```

- [ ] **Step 4: Author the ConfigMap, Service, and three Deployments**

```yaml
# templates/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "kdive.fullname" . }}-config
data:
  {{- range $k, $v := .Values.config }}
  {{ $k }}: {{ $v | quote }}
  {{- end }}
```

```yaml
# templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "kdive.fullname" . }}-server
spec:
  selector: { app: {{ include "kdive.fullname" . }}-server }
  ports: [{ port: 8000, targetPort: 8000 }]
```

```yaml
# templates/deployment-server.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "kdive.fullname" . }}-server
spec:
  replicas: {{ .Values.server.replicas }}
  selector: { matchLabels: { app: {{ include "kdive.fullname" . }}-server } }
  template:
    metadata: { labels: { app: {{ include "kdive.fullname" . }}-server } }
    spec:
      securityContext: { runAsNonRoot: true, runAsUser: 10001 }
      containers:
        - name: server
          image: {{ include "kdive.image" . }}
          args: ["server"]
          env: [{ name: KDIVE_HTTP_HOST, value: "0.0.0.0" }]
          envFrom: [{ configMapRef: { name: {{ include "kdive.fullname" . }}-config } }]
          ports: [{ containerPort: 8000 }]
          livenessProbe:                       # M2.1 scope: TCP only (ADR-0088 §5)
            tcpSocket: { port: 8000 }
            initialDelaySeconds: 5
```

```yaml
# templates/deployment-worker.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "kdive.fullname" . }}-worker
spec:
  replicas: {{ .Values.worker.replicas }}
  selector: { matchLabels: { app: {{ include "kdive.fullname" . }}-worker } }
  template:
    metadata: { labels: { app: {{ include "kdive.fullname" . }}-worker } }
    spec:
      securityContext: { runAsNonRoot: true, runAsUser: 10001, fsGroup: 10001 }
      containers:
        - name: worker
          image: {{ include "kdive.image" . }}
          args: ["worker"]
          envFrom: [{ configMapRef: { name: {{ include "kdive.fullname" . }}-config } }]
          volumeMounts:
            - { name: build, mountPath: /var/lib/kdive/build }
            - { name: install, mountPath: /var/lib/kdive/install }
      volumes:
        - name: build
          persistentVolumeClaim: { claimName: {{ include "kdive.fullname" . }}-build }
        - name: install
          persistentVolumeClaim: { claimName: {{ include "kdive.fullname" . }}-install }
```

`templates/deployment-reconciler.yaml` mirrors the server Deployment with `args: ["reconciler"]`, no Service, no ports, and a process liveness (omit the probe; k8s restarts on exit). Add `templates/pvc-worker.yaml` declaring the `build`/`install` PVCs from `.Values.worker.persistence`, and `templates/NOTES.txt`:

```
{{ if and .Values.bundledBackends .Values.demoAcknowledged }}
WARNING: bundledBackends is on — Postgres/MinIO run on emptyDir. Data is NOT durable;
a pod restart drops all state. Use external backends for anything but a throwaway demo.
{{ end }}
```

- [ ] **Step 5: Commit**

```bash
git add deploy/helm/kdive/
git commit -m "feat(helm): app-tier chart, migrate Job, demo-gated bundled backends"
```

### Task 4.2: Helm render/lint test

**Files:**
- Create: `tests/helm/test_helm_render.py`

- [ ] **Step 1: Write the test**

```python
# tests/helm/test_helm_render.py
from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

CHART = "deploy/helm/kdive"


def _template(*set_args: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "kdive", CHART]
    for s in set_args:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True)


def test_renders_three_deployments_against_external_backends() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("kind: Deployment") == 3
    assert "pre-install" in res.stdout            # external migrate hook phase


def test_bundled_without_ack_fails_to_render() -> None:
    res = _template("bundledBackends=true")
    assert res.returncode != 0
    assert "demoAcknowledged" in res.stderr


def test_bundled_with_ack_uses_post_install_migrate() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert "post-install" in res.stdout
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/helm/test_helm_render.py -q` (and `helm lint deploy/helm/kdive`)
Expected: PASS (3 passed); lint clean.

- [ ] **Step 3: Commit**

```bash
git add tests/helm/test_helm_render.py
git commit -m "test(helm): render gate — 3 deployments, demo ack, migrate hook phase"
```

**Phase 4 acceptance:** `helm lint` clean; external render has 3 Deployments + a pre-install migrate hook; `bundledBackends=true` without `demoAcknowledged=true` fails to render; the demo path uses a post-install migrate.

---

## Phase 5 — CI publish + provenance (ADR-0088)

### Task 5.1: Release workflow → GHCR with cosign + SBOM

**Files:**
- Create: `.github/workflows/release-image.yml`

- [ ] **Step 1: Author the release workflow**

```yaml
name: release-image
on:
  push:
    tags: ["v*.*.*"]
permissions:
  contents: read
  packages: write
  id-token: write     # keyless cosign
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>  # vX.Y.Z
        with: { persist-credentials: false }
      - uses: docker/login-action@<sha>  # vX.Y.Z
        with: { registry: ghcr.io, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
      - uses: docker/metadata-action@<sha>  # vX.Y.Z
        id: meta
        with:
          images: ghcr.io/randomparity/kdive
          tags: type=semver,pattern={{version}}
      - uses: docker/build-push-action@<sha>  # vX.Y.Z
        id: build
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          sbom: true                       # attach SBOM
          provenance: true
      - uses: sigstore/cosign-installer@<sha>  # vX.Y.Z
      - run: cosign sign --yes ghcr.io/randomparity/kdive@${{ steps.build.outputs.digest }}
```

> Resolve each `<sha>` to the action's current release and add the version comment. The tag is pinned by SemVer (ADR-0041); the signed subject is the immutable digest.

- [ ] **Step 2: Lint + security-scan the workflow**

Run: `actionlint .github/workflows/release-image.yml && zizmor .github/workflows/release-image.yml`
Expected: no findings.

- [ ] **Step 3: Document verification** — add to `deploy/compose/README.md` (and reference from the runbook) the consumer check:

```bash
cosign verify ghcr.io/randomparity/kdive:vX.Y.Z \
  --certificate-identity-regexp '^https://github.com/randomparity/kdive/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/release-image.yml deploy/compose/README.md
git commit -m "feat(ci): tagged GHCR publish with cosign signing + SBOM"
```

**Phase 5 acceptance:** `actionlint` + `zizmor` clean; on a tag the workflow pushes a SemVer+digest image, attaches an SBOM, and signs the digest; `cosign verify` succeeds.

---

## Phase 6 — Retire bootstrap + caller sweep (ADR-0088)

### Task 6.1: Remove the dead subcommands and helpers

**Files:**
- Modify: `src/kdive/__main__.py`, `src/kdive/admin/bootstrap.py`
- Test: `tests/admin/test_bootstrap_retirement.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/admin/test_bootstrap_retirement.py
from __future__ import annotations

import pytest

from kdive.__main__ import build_parser


def test_stack_subcommand_is_gone() -> None:
    parser = build_parser()
    for removed in ("stack", "install-compose", "print-local-env"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed])


def test_run_stack_not_importable() -> None:
    import kdive.admin.bootstrap as b
    assert not hasattr(b, "run_stack")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/admin/test_bootstrap_retirement.py -q`
Expected: FAIL (the subcommands still parse; `run_stack` still exists)

- [ ] **Step 3: Remove them** — delete the `stack`/`install-compose`/`print-local-env` subparsers and their `main()` dispatch branches in `__main__.py`, and delete `run_stack`/`install_compose`/`print_local_env` from `admin/bootstrap.py` (and their now-dead helpers). Keep `migrate`, `install-fixtures`, `seed-demo`.

- [ ] **Step 4: Run test + full suite**

Run: `uv run pytest tests/admin/test_bootstrap_retirement.py tests/admin -q`
Expected: PASS; no other admin test references the removed functions (fix any that do).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/__main__.py src/kdive/admin/bootstrap.py tests/admin/test_bootstrap_retirement.py
git commit -m "refactor: remove stack supervisor + install-compose/print-local-env crutches"
```

### Task 6.2: Sweep the callers

**Files:**
- Modify: `justfile`, `scripts/live-stack/start.sh`, `docs/runbooks/live-stack.md`

- [ ] **Step 1: Find every reference**

Run: `rg -n "kdive stack|run_stack|install-compose|print-local-env" justfile scripts docs`
Expected: a finite list (at least `justfile:68` `stack-up`'s echo, and `stack-start`/`stack-start-daemon` via `scripts/live-stack/start.sh`).

- [ ] **Step 2: Repoint them** — in `justfile`, change the `stack-up` echo to "Start the app tier with: `docker compose up -d migrate server worker reconciler`" and make `stack-start`/`stack-start-daemon` run the compose app tier (or remove them if the runbook covers it). Update `scripts/live-stack/start.sh`'s host-process start to the compose path. Update `docs/runbooks/live-stack.md` prose.

- [ ] **Step 3: Verify nothing references the removed subcommand**

Run: `rg -n "python -m kdive stack\b|run_stack" justfile scripts docs; echo "exit: $?"`
Expected: no matches (`rg` exit 1 = clean).

- [ ] **Step 4: Run shell linters on touched scripts**

Run: `shellcheck scripts/live-stack/start.sh && shfmt -d scripts/live-stack/start.sh`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add justfile scripts/live-stack/start.sh docs/runbooks/live-stack.md
git commit -m "chore: sweep callers of the removed stack subcommand to the compose app tier"
```

**Phase 6 acceptance:** removed subcommands gone; `run_stack` unimportable; no `justfile`/script/runbook references `python -m kdive stack`; shell linters clean.

---

## Milestone exit verification

- [ ] **Full local gate:** `just lint && just type && just config-guard && just config-docs-check && uv run pytest -q` — all green.
- [ ] **Image-only bring-up (the M2.1 exit criterion):** from a clean checkout, `docker build -t kdive:dev .`, then bring the compose app tier up with only `KDIVE_*` env (no source-tree scripts) and confirm: `migrate` exits 0, all three processes stay up, the server accepts a connection. Record the run.
- [ ] **Config discoverability:** `docs/guide/reference/config.md` lists every `KDIVE_*` variable grouped by process/group; a deliberately-missing `KDIVE_DATABASE_URL` makes `server` exit with a named `configuration_error` naming the variable.
- [ ] **Publish dry check:** push a throwaway tag to a fork (or use `act`/manual dispatch) and confirm the image is signed and SBOM-attached, `cosign verify` succeeds.

These map to the spec's Exit-criteria table: image-only bring-up (issues 1+2+6), compose/Helm healthy (issues 3+4), generated reference (issue 1).
