"""Render the MCP coverage-campaign environment from a single systems descriptor.

The campaign spans three deployments whose per-environment facts (remote host FQDN/IP,
k8s namespace + forward ports, the workstation LAN IP that remote guests must reach) are
otherwise re-derived by hand each run. This loader promotes those facts into one
gitignored TOML file (`systems.toml` at the repo root, scaffolded from
`systems.toml.example`) and renders the two things setup needs:

  render-env       emit the `d1.env` exports (the workstation provider env)
  setup-commands   emit the per-deployment copy-paste setup commands

Usage:
  uv run python -m scripts.coverage_campaign.systems render-env > artifacts/coverage-campaign/d1.env
  uv run python -m scripts.coverage_campaign.systems setup-commands
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

DEFAULT_DESCRIPTOR = Path("systems.toml")  # repo root, gitignored (scaffold: systems.toml.example)


def _load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"systems descriptor not found: {path}\n"
            "Copy systems.toml.example to ./systems.toml (repo root) and fill it in."
        )
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _render_env(doc: dict) -> str:
    """Render the workstation (D1) provider env as shell exports.

    Mirrors scripts/coverage_campaign/d1.env.template field-for-field so the rendered
    output is a drop-in d1.env that downstream setup can `source`.
    """
    ws = doc["workstation"]
    rl = doc["remote_libvirt"]
    lines = [
        "# Rendered by scripts/coverage_campaign/systems.py from systems.toml. Do not hand-edit.",
        "# Source AFTER scripts/live-stack/env.sh, under bash (env.sh uses ${BASH_SOURCE[0]}).",
        f"export KDIVE_FAULT_INJECT={1 if ws.get('fault_inject', True) else 0}",
        f'export KDIVE_SECRETS_ROOT="{ws["secrets_root"]}"',
        f'export KDIVE_REMOTE_LIBVIRT_URI="{rl["libvirt_uri"]}"',
        f'export KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF="{rl["client_cert_ref"]}"',
        f'export KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF="{rl["client_key_ref"]}"',
        f'export KDIVE_REMOTE_LIBVIRT_CA_CERT_REF="{rl["ca_cert_ref"]}"',
        f'export KDIVE_REMOTE_LIBVIRT_GDB_ADDR="{rl["gdb_addr"]}"',
        f'export KDIVE_REMOTE_BASE_IMAGE_VOLUME="{rl["base_image_volume"]}"',
        f'export KDIVE_BUILD_WORKSPACE="{ws["build_workspace"]}"',
        f'export KDIVE_BUILD_COMPONENT_ROOTS="{ws["build_component_roots"]}"',
        f'export KDIVE_S3_ENDPOINT_URL="{ws["s3_endpoint_url"]}"',
    ]
    return "\n".join(lines) + "\n"


def _setup_commands(doc: dict) -> str:
    """Emit the per-deployment setup commands with descriptor values substituted."""
    ws = doc["workstation"]
    rl = doc["remote_libvirt"]
    k8s = doc["k8s"]
    acl_ip = rl.get("acl_source_ip", ws["lan_ip"])
    out: list[str] = []

    out.append("# === remote-libvirt host (run on the host; revert at campaign end) ===")
    out.append(f"# host: {rl['host_fqdn']} ({rl['host_ip']})  ssh: {rl.get('ssh_target', '?')}")
    out.append(f"sudo iptables -I INPUT -s {acl_ip} -p tcp --dport {rl['tls_port']} -j ACCEPT")
    out.append(f"sudo iptables -I INPUT -s {acl_ip} -p tcp --dport {rl['gdbstub_range']} -j ACCEPT")
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
    doc = _load(ns.descriptor)
    if ns.command == "render-env":
        sys.stdout.write(_render_env(doc))
    else:
        sys.stdout.write(_setup_commands(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
