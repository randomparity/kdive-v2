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
