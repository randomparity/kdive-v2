"""Render the MCP coverage-campaign environment from the v2 ``systems.toml`` (ADR-0112).

The campaign spans three deployments whose per-environment facts (remote host FQDN/IP, k8s
namespace + forward ports, the workstation LAN IP that remote guests must reach) are otherwise
re-derived by hand each run. Since M2.6 Phase 3 (#395) those facts share the single declarative
``systems.toml`` the app loads: the provider **connection** (remote URI, TLS cert refs, gdbstub
address/range, base-image volume) is parsed from the v2 ``[[remote_libvirt]]`` + ``[[image]]``
entries via :func:`kdive.inventory.loader.load_inventory` (exactly one schema, no second parser),
and the campaign-only deployment knobs live in an extra ``[campaign]`` table the inventory loader
ignores.

  render-env       emit the `d1.env` exports (the workstation provider env)
  setup-commands   emit the per-deployment copy-paste setup commands

Because the remote connection is now resolved from ``systems.toml`` itself (the singleton
``KDIVE_REMOTE_LIBVIRT_*`` env vars are gone, #395), ``render-env`` points the app at the
descriptor via ``KDIVE_SYSTEMS_TOML`` instead of re-exporting the connection as env.

Usage:
  uv run python -m scripts.coverage_campaign.systems render-env > artifacts/coverage-campaign/d1.env
  uv run python -m scripts.coverage_campaign.systems setup-commands
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory
from kdive.inventory.model import InventoryDoc, RemoteLibvirtInstance

DEFAULT_DESCRIPTOR = Path("systems.toml")  # repo root, gitignored (scaffold: systems.toml.example)


def _require_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(
            f"systems descriptor not found: {path}\n"
            "Copy systems.toml.example to ./systems.toml (repo root) and fill it in."
        )


def _load_inventory(path: Path) -> InventoryDoc:
    try:
        return load_inventory(path)
    except InventoryError as exc:
        raise SystemExit(f"invalid systems.toml: {exc}") from exc


def _load_campaign(path: Path) -> dict:
    """Read the campaign-only ``[campaign]`` table (deployment knobs not in the inventory model)."""
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    campaign = data.get("campaign")
    if not isinstance(campaign, dict):
        raise SystemExit(
            "systems.toml is missing the [campaign] table the coverage campaign needs "
            "(workstation/k8s/remote-networking knobs); see systems.toml.example."
        )
    return campaign


def _remote_instance(doc: InventoryDoc) -> RemoteLibvirtInstance:
    if not doc.remote_libvirt:
        raise SystemExit("systems.toml declares no [[remote_libvirt]] instance for the campaign.")
    return doc.remote_libvirt[0]


def _render_env(doc: InventoryDoc, campaign: dict, descriptor: Path) -> str:
    """Render the workstation (D1) provider env as shell exports.

    The remote connection is resolved by the app from ``systems.toml`` itself, so this points
    ``KDIVE_SYSTEMS_TOML`` at the descriptor instead of re-exporting the connection singletons.
    """
    ws = campaign["workstation"]
    lines = [
        "# Rendered by scripts/coverage_campaign/systems.py from systems.toml. Do not hand-edit.",
        "# Source AFTER scripts/live-stack/env.sh, under bash (env.sh uses ${BASH_SOURCE[0]}).",
        f'export KDIVE_SYSTEMS_TOML="{descriptor}"',
        f"export KDIVE_FAULT_INJECT={1 if ws.get('fault_inject', True) else 0}",
        f'export KDIVE_SECRETS_ROOT="{ws["secrets_root"]}"',
        f'export KDIVE_BUILD_WORKSPACE="{ws["build_workspace"]}"',
        f'export KDIVE_BUILD_COMPONENT_ROOTS="{ws["build_component_roots"]}"',
        f'export KDIVE_S3_ENDPOINT_URL="{ws["s3_endpoint_url"]}"',
    ]
    return "\n".join(lines) + "\n"


def _setup_commands(doc: InventoryDoc, campaign: dict) -> str:
    """Emit the per-deployment setup commands with descriptor + inventory values substituted."""
    ws = campaign["workstation"]
    net = campaign["remote_libvirt"]
    k8s = campaign["k8s"]
    instance = _remote_instance(doc)
    tls_port = net.get("tls_port", 16514)
    acl_ip = net.get("acl_source_ip", ws["lan_ip"])
    out: list[str] = []

    out.append("# === remote-libvirt host (run on the host; revert at campaign end) ===")
    out.append(f"# host: {net['host_fqdn']} ({net['host_ip']})  ssh: {net.get('ssh_target', '?')}")
    out.append(f"sudo iptables -I INPUT -s {acl_ip} -p tcp --dport {tls_port} -j ACCEPT")
    out.append(
        f"sudo iptables -I INPUT -s {acl_ip} -p tcp --dport {instance.gdbstub_range} -j ACCEPT"
    )
    out.append("")
    out.append("# === D2 k8s port-forwards (run on the workstation) ===")
    out.append(f"# context: {k8s['context']}  namespace: {k8s['namespace']}")
    out.append(
        f"kubectl -n {k8s['namespace']} port-forward {k8s['oidc_forward']} "
        f"{k8s['oidc_local_port']}:{k8s['oidc_service_port']} &"
    )
    out.append(
        f"kubectl -n {k8s['namespace']} port-forward {k8s['server_forward']} "
        f"{k8s['server_local_port']}:{k8s['server_service_port']} &"
    )
    out.append(f"# in-cluster issuer (for iss match): {k8s['in_cluster_issuer']}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["render-env", "setup-commands"], help="what to emit")
    parser.add_argument(
        "--descriptor", type=Path, default=DEFAULT_DESCRIPTOR, help="path to systems.toml"
    )
    ns = parser.parse_args(argv)
    _require_file(ns.descriptor)
    doc = _load_inventory(ns.descriptor)
    campaign = _load_campaign(ns.descriptor)
    if ns.command == "render-env":
        sys.stdout.write(_render_env(doc, campaign, ns.descriptor))
    else:
        sys.stdout.write(_setup_commands(doc, campaign))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
