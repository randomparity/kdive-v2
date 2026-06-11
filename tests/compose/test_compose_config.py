"""Structural test for the reference compose (ADR-0088 Phase 3).

Gated on the ``docker compose`` plugin being available (it is on
``ubuntu-latest``, so this runs in the normal ``just test`` job and gates every
PR). It does not build or pull anything — ``docker compose config`` only parses
the committed file, resolves the ``x-backends`` anchor / merge keys, and renders
the canonical service model.

It locks the load-bearing ADR-0088 decision-4 ordering contract: the app
services depend on the ``migrate`` one-shot with
``service_completed_successfully`` (a bare ``depends_on`` would let them boot
before migrations finish), and ``migrate`` itself waits for a healthy Postgres.
A future edit that weakens the condition fails here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


def _docker_compose_available() -> bool:
    # Gate on the compose *plugin*, not just the `docker` binary: a host with docker
    # but no compose plugin would otherwise hard-fail instead of skipping.
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_compose_available(),
    reason="the docker compose plugin is required to render the compose model",
)

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
_APP_SERVICES = ("server", "worker", "reconciler")


def _config() -> dict[str, Any]:
    res = subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, f"compose config invalid: {res.stderr}"
    return json.loads(res.stdout)


def _services() -> dict[str, Any]:
    return _config()["services"]


def test_compose_config_is_valid() -> None:
    # `docker compose config -q` is the issue's acceptance gate; rendering to JSON
    # exercises the same parse and gives us the model the rest of the file asserts on.
    assert _services()  # non-empty → parsed


def test_migrate_one_shot_runs_command_and_waits_for_postgres() -> None:
    migrate = _services()["migrate"]
    assert migrate["command"] == ["migrate"]
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_app_service_waits_for_migrate_completion(service: str) -> None:
    # The ADR-0088 ordering fix: completion, not mere start. A bare depends_on
    # (condition "service_started") would let the app hit the DB pre-migration.
    dep = _services()[service]["depends_on"]
    assert dep["migrate"]["condition"] == "service_completed_successfully"


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_app_service_waits_for_bucket_creation(service: str) -> None:
    # All three app processes do object-store I/O, so they wait for the minio-init
    # one-shot to complete — which transitively guarantees minio is healthy and the
    # artifacts bucket exists. Without this edge a bare `up <service>` starts a
    # process whose first S3 call fails (no bucket).
    dep = _services()[service]["depends_on"]
    assert dep["minio-init"]["condition"] == "service_completed_successfully"


def test_server_waits_for_the_issuer() -> None:
    # The server validates bearer tokens against the issuer; a bare `up server`
    # must start oidc too. The mock issuer has no healthcheck, so this is a
    # start-ordering edge, not a health gate.
    dep = _services()["server"]["depends_on"]
    assert "oidc" in dep


@pytest.mark.parametrize("service", ("migrate", *_APP_SERVICES))
def test_shared_backend_env_is_merged_into_every_app_service(service: str) -> None:
    # The `x-backends` anchor is merged into each service via `<<: *backends`, so
    # the DSN appears once in the source but on every process here.
    env = _services()[service]["environment"]
    assert env["KDIVE_DATABASE_URL"].startswith("postgresql://")


def test_server_binds_all_interfaces_and_publishes_its_port() -> None:
    server = _services()["server"]
    assert server["environment"]["KDIVE_HTTP_HOST"] == "0.0.0.0"
    published = {str(p.get("published")) for p in server.get("ports", [])}
    assert "8000" in published


# Per-process aux health/metrics ports (ADR-0090 §5): the loopback default ports the
# bind decision keeps, but bound to 0.0.0.0 inside each container so the compose
# healthcheck (and an in-network scrape) can reach a side port that is never published.
_AUX_PORTS = {"server": 9464, "worker": 9465, "reconciler": 9466}


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_aux_health_listener_binds_container_interface_on_its_port(service: str) -> None:
    # Each process runs in its own container/network namespace, so the aux listener is
    # bound 0.0.0.0:<per-process-port> via an explicit KDIVE_HEALTH_BIND_ADDR (loopback
    # default is not reachable by a healthcheck that execs in the container's PID/net ns
    # only via 127.0.0.1, and never by an in-network scrape). The container ns IS the
    # trust boundary: the aux port is never published to the host.
    env = _services()[service]["environment"]
    assert env["KDIVE_HEALTH_BIND_ADDR"] == f"0.0.0.0:{_AUX_PORTS[service]}"


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_aux_port_is_not_published_to_the_host(service: str) -> None:
    # The aux listener carries no auth; the network boundary is its access control. It
    # must never be in a host port mapping (only the server's 8000 MCP port is).
    published = {str(p.get("published")) for p in _services()[service].get("ports", [])}
    assert str(_AUX_PORTS[service]) not in published


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_app_service_has_healthcheck_against_its_aux_readyz(service: str) -> None:
    # The compose liveness/readiness wiring probes /readyz on the process's own aux port.
    healthcheck = _services()[service]["healthcheck"]
    test_cmd = healthcheck["test"]
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    assert f"127.0.0.1:{_AUX_PORTS[service]}/readyz" in joined
