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
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

CHART = str(Path(__file__).resolve().parents[2] / "deploy" / "helm" / "kdive")

# Per-process aux health/metrics ports (ADR-0090 §5), matching the registry defaults.
_AUX_PORTS = {"server": 9464, "worker": 9465, "reconciler": 9466}


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


def _hooks_by_kind(*set_args: str) -> dict[str, dict[str, Any]]:
    """Render the chart and index its hook-annotated manifests by Kind.

    Returns ``{kind: {"phase": <hook value>, "weight": <int>}}`` for every doc
    that carries a ``helm.sh/hook`` annotation, so a test can assert the relative
    creation order Helm derives from hook phase + weight.
    """
    res = _template(*set_args)
    assert res.returncode == 0, res.stderr
    out: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not isinstance(doc, dict):
            continue
        annotations = doc.get("metadata", {}).get("annotations") or {}
        hook = annotations.get("helm.sh/hook")
        if hook is None:
            continue
        out[doc["kind"]] = {
            "phase": hook,
            "weight": int(annotations.get("helm.sh/hook-weight", "0")),
        }
    return out


def test_external_configmap_is_a_pre_install_hook_before_migrate() -> None:
    # The migrate Job is a pre-install hook that envFroms the config ConfigMap. Helm
    # creates normal resources only AFTER pre-install hooks, so a normal-resource
    # ConfigMap leaves the migrate pod in CreateContainerConfigError until the hook
    # timeout (issue #311). The ConfigMap must therefore be a pre-install hook too,
    # weighted strictly lower than the migrate Job so Helm creates it first.
    hooks = _hooks_by_kind("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert "ConfigMap" in hooks, "external-path ConfigMap is not a hook; migrate cannot read it"
    assert "Job" in hooks
    assert "pre-install" in hooks["ConfigMap"]["phase"]
    assert "pre-upgrade" in hooks["ConfigMap"]["phase"]
    assert hooks["ConfigMap"]["weight"] < hooks["Job"]["weight"], (
        "ConfigMap hook-weight must be strictly below the migrate Job's so it is created first"
    )


def test_bundled_configmap_stays_a_normal_resource() -> None:
    # The bundled demo path runs migrate POST-install (after the bundled Postgres is
    # created), so its ConfigMap is already present as a normal resource by then.
    # Turning it into a hook would change the bundled path's behavior for no reason,
    # so the hook annotation must be scoped to the external path only.
    hooks = _hooks_by_kind("bundledBackends=true", "demoAcknowledged=true")
    assert "ConfigMap" not in hooks, "bundled-path ConfigMap should stay a normal resource"


def _deployments() -> dict[str, dict[str, Any]]:
    """Render the external-backend chart and index Deployments by process name."""
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    out: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            name = doc["metadata"]["name"]
            for proc in _AUX_PORTS:
                if name.endswith(f"-{proc}"):
                    out[proc] = doc
    return out


def _container(deploy: dict[str, Any]) -> dict[str, Any]:
    return deploy["spec"]["template"]["spec"]["containers"][0]


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_deployment_binds_aux_listener_to_pod_interface(proc: str) -> None:
    # Each pod runs in its own network namespace; the kubelet probes from the node and a
    # scrape comes from outside the container, so the aux listener binds 0.0.0.0:<port>
    # via an explicit per-deployment KDIVE_HEALTH_BIND_ADDR env (env wins over the shared
    # configMap). No Service fronts the aux port, so it stays pod-local / non-public.
    env = {e["name"]: e.get("value") for e in _container(_deployments()[proc])["env"]}
    assert env["KDIVE_HEALTH_BIND_ADDR"] == f"0.0.0.0:{_AUX_PORTS[proc]}"


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_deployment_liveness_probes_livez_on_aux_port(proc: str) -> None:
    # Liveness probes /livez (loop-alive), NOT /readyz: a failing readiness (a backend
    # down) must not let the kubelet kill a live-but-not-ready pod (ADR-0090 §5).
    probe = _container(_deployments()[proc])["livenessProbe"]
    assert probe["httpGet"]["path"] == "/livez"
    assert probe["httpGet"]["port"] == _AUX_PORTS[proc]


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_deployment_readiness_probes_readyz_on_aux_port(proc: str) -> None:
    probe = _container(_deployments()[proc])["readinessProbe"]
    assert probe["httpGet"]["path"] == "/readyz"
    assert probe["httpGet"]["port"] == _AUX_PORTS[proc]


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_deployment_has_prometheus_scrape_annotations(proc: str) -> None:
    # The pull-based scrape targets /metrics on the aux port (ADR-0090 §5).
    annotations = _deployments()[proc]["spec"]["template"]["metadata"].get("annotations", {})
    assert annotations.get("prometheus.io/scrape") == "true"
    assert annotations.get("prometheus.io/path") == "/metrics"
    assert annotations.get("prometheus.io/port") == str(_AUX_PORTS[proc])


def test_service_does_not_expose_the_aux_port() -> None:
    # The aux listener carries no auth; the network boundary is its access control. The
    # only Service (server's MCP) must front 8000 only, never the aux port.
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Service":
            svc_ports = {p.get("port") for p in doc["spec"]["ports"]}
            assert svc_ports == {8000}


def test_lint_is_clean() -> None:
    res = subprocess.run(
        ["helm", "lint", CHART],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 chart(s) failed" in res.stdout
