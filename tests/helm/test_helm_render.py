"""Render/lint gate for the kdive Helm chart (ADR-0088, M2.1 Phase 4).

These tests shell out to a real ``helm`` binary so the chart's templating logic
(the demo-acknowledged render gate, migrate-Job hook phase, Deployment count) is
exercised end to end. They skip when ``helm`` is not installed; a skipped run
validates nothing, so CI must provide the binary for this gate to mean anything.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

CHART = str(Path(__file__).resolve().parents[2] / "deploy" / "helm" / "kdive")


def _template(*set_args: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "kdive", CHART]
    for s in set_args:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def test_renders_three_deployments_against_external_backends() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("kind: Deployment") == 3
    assert "pre-install" in res.stdout


def test_bundled_without_ack_fails_to_render() -> None:
    res = _template("bundledBackends=true")
    assert res.returncode != 0
    assert "demoAcknowledged" in res.stderr


def test_bundled_with_ack_uses_post_install_migrate() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert "post-install" in res.stdout


def test_external_render_omits_post_install_migrate_hook() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert "post-install" not in res.stdout


def test_bundled_path_wires_backends_into_config() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    # The demo apps must reach the in-release services, not render empty config.
    dsn = "postgresql://kdive:kdive-demo@kdive-postgresql:5432/kdive"  # pragma: allowlist secret
    assert f'KDIVE_DATABASE_URL: "{dsn}"' in res.stdout
    assert 'KDIVE_S3_ENDPOINT_URL: "http://kdive-minio:9000"' in res.stdout
    assert "wait-for-db" in res.stdout


def test_external_path_passes_db_url_through_and_omits_demo_creds() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://ext/db")
    assert res.returncode == 0, res.stderr
    assert 'KDIVE_DATABASE_URL: "postgresql://ext/db"' in res.stdout
    assert "AWS_ACCESS_KEY_ID" not in res.stdout
    assert "wait-for-db" not in res.stdout


def test_lint_is_clean() -> None:
    res = subprocess.run(
        ["helm", "lint", CHART],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 chart(s) failed" in res.stdout
