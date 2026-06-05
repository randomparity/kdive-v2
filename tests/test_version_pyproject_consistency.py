"""Guard that `[project].version` and the installed distribution metadata agree.

The test runs under `uv run`, which resyncs the editable install to the current
`pyproject.toml` first, so a divergence means a genuine packaging problem (ADR-0041
decision 4). `uv.lock` staleness is guarded separately by `uv lock --check`.
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
