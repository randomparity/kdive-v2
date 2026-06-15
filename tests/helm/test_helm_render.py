"""Render/lint gate for the kdive Helm chart (ADR-0088, M2.1 Phase 4).

These tests shell out to a real ``helm`` binary so the chart's templating logic
(the demo-acknowledged render gate, migrate-Job hook phase, Deployment count) is
exercised end to end. They skip when ``helm`` is not installed; a skipped run
validates nothing, so CI must provide the binary for this gate to mean anything.
"""

from __future__ import annotations

import json
import re
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


def _notes(*set_args: str) -> str:
    """Render the chart's NOTES.txt (helm template omits NOTES; --dry-run=client emits it)."""
    args = ["helm", "install", "kdive", CHART, "--dry-run=client"]
    for s in set_args:
        args += ["--set", s]
    res = subprocess.run(args, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    _, _, notes = res.stdout.partition("NOTES:")
    return notes


def _oidc_request_mappings(res: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    """Parse the demo issuer's ``requestMappings`` out of the rendered JSON_CONFIG env var.

    Returns the ordered list of mappings (order is load-bearing: mock-oauth2-server is
    first-match-wins). Raises if the oidc Deployment or its JSON_CONFIG is absent.
    """
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "Deployment"):
            continue
        if not str(doc.get("metadata", {}).get("name", "")).endswith("-oidc"):
            continue
        env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
        raw = next(e["value"] for e in env if e["name"] == "JSON_CONFIG")
        mappings = json.loads(raw)["tokenCallbacks"][0]["requestMappings"]
        assert isinstance(mappings, list)
        return mappings
    raise AssertionError("no -oidc Deployment with a JSON_CONFIG env var in the render")


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
    # The demo apps must reach the in-chart services, not render empty config.
    dsn = (
        "postgresql://kdive:kdive-demo@kdive-kdive-postgres:5432/kdive"  # pragma: allowlist secret
    )
    assert f'KDIVE_DATABASE_URL: "{dsn}"' in res.stdout
    assert 'KDIVE_S3_ENDPOINT_URL: "http://kdive-kdive-minio:9000"' in res.stdout
    assert 'KDIVE_OIDC_ISSUER: "http://kdive-kdive-oidc:8080/default"' in res.stdout
    assert 'KDIVE_OIDC_JWKS_URI: "http://kdive-kdive-oidc:8080/default/jwks"' in res.stdout
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


def _service(*set_args: str) -> dict[str, Any]:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", *set_args)
    assert res.returncode == 0, res.stderr
    return next(
        doc
        for doc in yaml.safe_load_all(res.stdout)
        if isinstance(doc, dict) and doc.get("kind") == "Service"
    )


def test_service_defaults_to_clusterip_without_a_nodeport() -> None:
    svc = _service()
    assert svc["spec"]["type"] == "ClusterIP"
    assert "nodePort" not in svc["spec"]["ports"][0]


def test_service_type_nodeport_lets_the_cluster_assign_the_port() -> None:
    svc = _service("service.type=NodePort")
    assert svc["spec"]["type"] == "NodePort"
    # No pin: the cluster assigns the nodePort, so the chart must not emit one.
    assert "nodePort" not in svc["spec"]["ports"][0]


def test_service_nodeport_can_be_pinned() -> None:
    svc = _service("service.type=NodePort", "service.nodePort=30800")
    assert svc["spec"]["type"] == "NodePort"
    assert svc["spec"]["ports"][0]["nodePort"] == 30800


def _deployments_with(*set_args: str) -> dict[str, dict[str, Any]]:
    """Render with extra --set args and index Deployments by process name."""
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", *set_args)
    assert res.returncode == 0, res.stderr
    out: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            name = doc["metadata"]["name"]
            for proc in _AUX_PORTS:
                if name.endswith(f"-{proc}"):
                    out[proc] = doc
    return out


def test_secrets_unset_mounts_nothing() -> None:
    # The opt-in secret projection (#313) must be inert by default: no mount, no volume, no
    # KDIVE_SECRETS_ROOT, so a deployment that does not need file secrets is unchanged.
    for proc, deploy in _deployments_with().items():
        container = _container(deploy)
        env_names = {e["name"] for e in container["env"]}
        assert "KDIVE_SECRETS_ROOT" not in env_names, proc
        mounts = {m["name"] for m in container.get("volumeMounts", [])}
        assert "kdive-secrets" not in mounts, proc
        volumes = {v["name"] for v in deploy["spec"]["template"]["spec"].get("volumes", [])}
        assert "kdive-secrets" not in volumes, proc


@pytest.mark.parametrize("proc", list(_AUX_PORTS))
def test_secrets_set_projects_readonly_on_each_component(proc: str) -> None:
    # With secrets.secretName set, every component that resolves file-ref secrets gets the
    # Secret mounted read-only under KDIVE_SECRETS_ROOT (#313): worker/reconciler open the
    # remote-libvirt qemu+tls transport, the server resolves debug-session secrets.
    deploy = _deployments_with("secrets.secretName=kdive-remote-tls")[proc]
    container = _container(deploy)
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["KDIVE_SECRETS_ROOT"] == "/etc/kdive/secrets"
    mount = next(m for m in container["volumeMounts"] if m["name"] == "kdive-secrets")
    assert mount["mountPath"] == "/etc/kdive/secrets"
    assert mount["readOnly"] is True
    volume = next(
        v for v in deploy["spec"]["template"]["spec"]["volumes"] if v["name"] == "kdive-secrets"
    )
    assert volume["secret"]["secretName"] == "kdive-remote-tls"  # pragma: allowlist secret
    # The non-root UID (10001) reads the root-owned Secret files via the pod fsGroup's group
    # bit, so the mode must grant group read (0440, not owner-only 0400) and fsGroup must be set
    # — verified on a real cluster. YAML parses the octal literal 0440 to 288.
    assert volume["secret"]["defaultMode"] == 0o440
    assert deploy["spec"]["template"]["spec"]["securityContext"]["fsGroup"] == 10001


def test_systems_inventory_unset_mounts_nothing() -> None:
    for proc, deploy in _deployments_with().items():
        container = _container(deploy)
        env_names = {e["name"] for e in container["env"]}
        assert "KDIVE_SYSTEMS_TOML" not in env_names, proc
        mounts = {m["name"] for m in container.get("volumeMounts", [])}
        assert "kdive-systems" not in mounts, proc
        volumes = {v["name"] for v in deploy["spec"]["template"]["spec"].get("volumes", [])}
        assert "kdive-systems" not in volumes, proc


def test_systems_inventory_configmap_mounts_on_components_and_migrate() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=inv")
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]

    deployments = {
        doc["metadata"]["name"].removeprefix("kdive-kdive-"): doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].startswith("kdive-kdive-")
    }
    for proc in ("server", "worker", "reconciler"):
        deploy = deployments[proc]
        container = _container(deploy)
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["KDIVE_SYSTEMS_TOML"] == "/etc/kdive/systems/systems.toml"
        mount = next(m for m in container["volumeMounts"] if m["name"] == "kdive-systems")
        assert mount["mountPath"] == "/etc/kdive/systems"
        assert mount["readOnly"] is True
        volume = next(
            v for v in deploy["spec"]["template"]["spec"]["volumes"] if v["name"] == "kdive-systems"
        )
        assert volume["configMap"]["name"] == "inv"
        assert volume["configMap"]["items"] == [{"key": "systems.toml", "path": "systems.toml"}]

    migrate = next(doc for doc in docs if doc.get("kind") == "Job")
    container = migrate["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["KDIVE_SYSTEMS_TOML"] == "/etc/kdive/systems/systems.toml"
    assert any(m["name"] == "kdive-systems" for m in container["volumeMounts"])
    assert any(v["name"] == "kdive-systems" for v in migrate["spec"]["template"]["spec"]["volumes"])


def test_bundled_renders_demo_backends() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    for name in ("kdive-kdive-postgres", "kdive-kdive-minio", "kdive-kdive-oidc"):
        assert f"name: {name}\n" in res.stdout, name
    assert "mock-oauth2-server" in res.stdout
    assert "kind: NetworkPolicy" in res.stdout
    # six Deployments on the demo path: 3 app + 3 demo backends.
    assert res.stdout.count("kind: Deployment") == 6


def test_external_path_has_no_demo_backends() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert "mock-oauth2-server" not in res.stdout
    assert "kind: NetworkPolicy" not in res.stdout
    assert res.stdout.count("kind: Deployment") == 3


def test_bundled_oidc_pins_audience_kdive() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert '"aud":["kdive"]' in res.stdout


def test_bundled_oidc_mints_role_claims() -> None:
    # The default demo claim set must carry a usable RBAC grant so a stock demo deploy can
    # exercise the authz surface (#369): admin on the seeded `demo` project + all three
    # platform roles. toJson sorts keys, so these JSON substrings are stable.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert '"projects":["demo"]' in res.stdout
    assert '"roles":{"demo":"admin"}' in res.stdout
    for role in ("platform_admin", "platform_operator", "platform_auditor"):
        assert f'"{role}"' in res.stdout, role


def test_bundled_oidc_claims_value_is_wired() -> None:
    # An operator narrows the grant to test a denial; --set deep-merges into the default
    # map, overriding only the targeted leaf and leaving the other claims defaulted.
    res = _template(
        "bundledBackends=true",
        "demoAcknowledged=true",
        "demo.oidc.claims.roles.demo=viewer",
    )
    assert res.returncode == 0, res.stderr
    assert '"roles":{"demo":"viewer"}' in res.stdout
    assert '"roles":{"demo":"admin"}' not in res.stdout
    # The other defaults survive the targeted override (deep-merge, not replace).
    assert '"projects":["demo"]' in res.stdout
    assert '"platform_admin"' in res.stdout
    assert '"aud":["kdive"]' in res.stdout


def test_bundled_oidc_aud_pin_survives_override() -> None:
    # `aud` is a template invariant, not a value: an operator override can never break
    # audience verification and lock the demo out.
    res = _template(
        "bundledBackends=true",
        "demoAcknowledged=true",
        "demo.oidc.claims.aud=nope",
    )
    assert res.returncode == 0, res.stderr
    assert '"aud":["kdive"]' in res.stdout
    assert "nope" not in res.stdout


def test_bundled_oidc_mints_role_scoped_variants() -> None:
    # ADR-0108 §4: per-role client_id mappings let `demo-token.sh --role <role>` mint a
    # narrowed token (project role only, no platform roles) so a denial is reachable without
    # a chart redeploy. toJson sorts keys, so each variant's full claims object is a stable
    # substring — pinning it proves the variant carries NO platform_roles.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert '"match":"kdive-demo-viewer","requestParam":"client_id"' in res.stdout
    assert '"match":"kdive-demo-operator","requestParam":"client_id"' in res.stdout
    assert (
        '{"aud":["kdive"],"projects":["demo"],"roles":{"demo":"viewer"},"sub":"kdive-demo-viewer"}'
        in res.stdout
    )
    assert (
        '{"aud":["kdive"],"projects":["demo"],"roles":{"demo":"operator"},"sub":"kdive-demo-operator"}'
        in res.stdout
    )


def test_bundled_oidc_variant_mappings_precede_catch_all() -> None:
    # The feature's load-bearing invariant: every per-role client_id mapping must come BEFORE
    # the grant_type:"*" catch-all. mock-oauth2-server is first-match-wins, so if a reorder
    # ever put the catch-all first, a client_id=kdive-demo-viewer request would match it and
    # silently mint a FULL-ADMIN token — a privilege escalation for a request that asked for
    # less. A presence-only assertion can't catch that; pin the order explicitly.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    mappings = _oidc_request_mappings(res)
    catch_all_idx = next(
        i for i, m in enumerate(mappings) if m["requestParam"] == "grant_type" and m["match"] == "*"
    )
    client_id_idxs = [i for i, m in enumerate(mappings) if m["requestParam"] == "client_id"]
    assert client_id_idxs, "expected per-role client_id mappings"
    assert max(client_id_idxs) < catch_all_idx, (
        "a client_id variant mapping is at/after the catch-all; first-match-wins would mint "
        "an admin token for a narrowed-role request"
    )
    # The catch-all is the only mapping carrying platform_roles (the full admin grant); the
    # variants must not, or the denial they exist to demonstrate would not occur.
    for m in mappings:
        if m["requestParam"] == "client_id":
            assert "platform_roles" not in m["claims"], m["match"]


def test_demo_token_script_client_ids_match_rendered_variants() -> None:
    # The script (scripts/demo-token.sh) and the chart hold the client_id literals in two
    # places; if they drift (e.g. the template prefix changes), `--role viewer` sends an
    # unmatched client_id that falls through to the admin catch-all and silently mints a full
    # token. Pin that every non-admin role the script can request maps to a client_id the
    # chart actually registers as a variant.
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    rendered_variant_ids = {
        m["match"] for m in _oidc_request_mappings(res) if m["requestParam"] == "client_id"
    }

    script = (Path(CHART).resolve().parents[2] / "scripts" / "demo-token.sh").read_text()
    # Lines like: `viewer) client_id="kdive-demo-viewer" ;;`
    script_map = {
        m.group(1): m.group(2)
        for m in re.finditer(r'^(\w+)\)\s*client_id="([^"]+)"', script, re.MULTILINE)
    }
    narrowed = {cid for role, cid in script_map.items() if role != "admin"}
    assert narrowed, "no non-admin client_id literals parsed from demo-token.sh"
    missing = narrowed - rendered_variant_ids
    assert not missing, f"script client_ids with no chart variant mapping (drift): {missing}"

    # The symmetric hole: `admin` must resolve via the grant_type catch-all, NOT a variant.
    # If it ever pointed at a variant client_id, `--role admin` would silently mint a narrowed
    # token and break the default full grant every first-run flow relies on.
    assert script_map.get("admin") not in rendered_variant_ids, (
        "demo-token.sh maps --role admin to a variant client_id; it must use the catch-all"
    )


def test_notes_warns_when_exposed_object_store_is_world_open() -> None:
    # Exposing the bundled object store (type != ClusterIP) with a world-open ingress range is
    # a footgun (static demo creds + all artifacts reachable). NOTES must warn when the
    # effective range is 0.0.0.0/0 — whether by the empty-default or an explicit entry — and
    # stay quiet when it's scoped or the store stays in-cluster.
    base = ("bundledBackends=true", "demoAcknowledged=true")
    marker = "exposed off-cluster"

    assert marker not in _notes(*base), "ClusterIP default must not warn"
    assert marker in _notes(*base, "demo.minio.service.type=LoadBalancer"), (
        "exposed + empty sourceRanges (world-open default) must warn"
    )
    assert marker in _notes(
        *base, "demo.minio.service.type=NodePort", "demo.minio.service.sourceRanges={0.0.0.0/0}"
    ), "explicit 0.0.0.0/0 must warn"
    assert marker not in _notes(
        *base,
        "demo.minio.service.type=LoadBalancer",
        "demo.minio.service.sourceRanges={192.168.16.0/24}",
    ), "a scoped CIDR must not warn"


def test_bundled_oidc_blanked_claims_degrade_to_floor() -> None:
    # Blanking the claims map (a plausible "token with no grant" override) must render the
    # safe {sub,aud} floor, not a nil-pointer template error. With no role grant the
    # role-scoped variants are suppressed (there is nothing to downgrade), so the catch-all
    # mapping is the only one rendered.
    res = _template("bundledBackends=true", "demoAcknowledged=true", "demo.oidc.claims=null")
    assert res.returncode == 0, res.stderr
    assert '"aud":["kdive"]' in res.stdout
    assert '"sub":"kdive-demo"' in res.stdout
    assert '"roles"' not in res.stdout
    assert '"requestParam":"client_id"' not in res.stdout


def test_bundled_demo_services_are_clusterip() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Service":
            assert doc["spec"].get("type", "ClusterIP") == "ClusterIP", doc["metadata"]["name"]


def test_bundled_with_nodeport_is_rejected() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true", "service.type=NodePort")
    assert res.returncode != 0
    assert "service.type must stay ClusterIP" in res.stderr


def test_bundled_has_a_helm_test_hook() -> None:
    hooks = _hooks_by_kind("bundledBackends=true", "demoAcknowledged=true")
    assert hooks.get("Pod", {}).get("phase") == "test"


def test_lint_is_clean() -> None:
    res = subprocess.run(
        ["helm", "lint", CHART],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 chart(s) failed" in res.stdout
