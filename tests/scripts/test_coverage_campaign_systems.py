"""The coverage-campaign render-env/setup-commands consume the v2 systems.toml (#395, ADR-0112).

Phase 3 folds the campaign descriptor into the single v2 ``systems.toml``: the remote connection
is parsed from the ``[[remote_libvirt]]`` + ``[[image]]`` inventory via the shared
``kdive.inventory.loader.load_inventory`` (exactly one schema), and the deployment-only knobs live
in an extra ``[campaign]`` table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.coverage_campaign.systems import main

_DESCRIPTOR = """
schema_version = 2

[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "fedora-kdive-remote-base-43.qcow2"

[[remote_libvirt]]
name = "ub24-big"
uri = "qemu+tls://ub24-big.example/system"
gdb_addr = "192.168.10.20"
gdbstub_range = "47000:47099"
client_cert_ref = "remote-clientcert.pem"
client_key_ref = "remote-clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "remote-ca.pem"
base_image = "fedora-kdive-remote-base-43"
cost_class = "remote"

[campaign.workstation]
fault_inject = true
secrets_root = "/home/dave/.kdive-secrets"
build_workspace = "/work/.live-build"
build_component_roots = "/work/fixtures:/work/.live-components"
s3_endpoint_url = "http://192.168.2.99:9000"
lan_ip = "192.168.2.99"

[campaign.remote_libvirt]
host_fqdn = "ub24-big.example"
host_ip = "192.168.10.20"
ssh_target = "dave@ub24-big.example"
tls_port = 16514
acl_source_ip = "192.168.2.99"

[campaign.k8s]
context = "kdive-d2"
namespace = "kdive"
oidc_forward = "svc/oidc"
oidc_local_port = 8080
oidc_service_port = 80
server_forward = "svc/server"
server_local_port = 8081
server_service_port = 80
in_cluster_issuer = "http://oidc.kdive.svc/realms/kdive"
"""


def _write(tmp_path: Path) -> Path:
    path = tmp_path / "systems.toml"
    path.write_text(_DESCRIPTOR)
    return path


def test_render_env_points_at_descriptor_not_singletons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path)
    rc = main(["render-env", "--descriptor", str(path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f'export KDIVE_SYSTEMS_TOML="{path}"' in out
    assert 'export KDIVE_S3_ENDPOINT_URL="http://192.168.2.99:9000"' in out
    assert "export KDIVE_FAULT_INJECT=1" in out
    # The deleted connection singletons must NOT be re-exported.
    assert "KDIVE_REMOTE_LIBVIRT_URI" not in out
    assert "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF" not in out
    assert "KDIVE_REMOTE_LIBVIRT_GDB_ADDR" not in out


def test_setup_commands_use_inventory_connection_and_campaign_knobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path)
    rc = main(["setup-commands", "--descriptor", str(path)])
    out = capsys.readouterr().out
    assert rc == 0
    # gdbstub_range comes from the [[remote_libvirt]] inventory instance, not the campaign table.
    assert "--dport 47000:47099" in out
    assert "--dport 16514" in out  # tls_port from [campaign.remote_libvirt]
    assert "ub24-big.example (192.168.10.20)" in out
    assert "namespace: kdive" in out


def test_missing_descriptor_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["render-env", "--descriptor", str(tmp_path / "absent.toml")])


def test_malformed_inventory_exits(tmp_path: Path) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n[[remote_libvirt]\n")  # malformed header
    with pytest.raises(SystemExit):
        main(["setup-commands", "--descriptor", str(path)])


def test_missing_campaign_table_exits(tmp_path: Path) -> None:
    path = tmp_path / "systems.toml"
    # A valid v2 inventory but no [campaign] table the campaign tooling needs.
    path.write_text(
        "schema_version = 2\n"
        '[[image]]\nprovider = "remote-libvirt"\nname = "b"\narch = "x86_64"\n'
        'format = "qcow2"\nroot_device = "/dev/vda"\nvisibility = "public"\n'
        '[image.source]\nkind = "staged"\nvolume = "b.qcow2"\n'
    )
    with pytest.raises(SystemExit):
        main(["render-env", "--descriptor", str(path)])
