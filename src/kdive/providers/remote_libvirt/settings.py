"""Co-located ``KDIVE_REMOTE_LIBVIRT_*`` settings (ADR-0087, ADR-0076).

A dependency-light module (imports only :class:`Setting`). The provider is opt-in:
presence of the URI enables it, and the mutual-TLS cert refs are required only then
(``required_when``), so an operator running only local-libvirt never fails startup on a
remote provider they did not enable. The cert refs are secrets-by-reference.
"""

from __future__ import annotations

from collections.abc import Mapping

from kdive.config.registry import Setting

_RT = frozenset({"worker", "reconciler"})


def _uri_set(env: Mapping[str, str]) -> bool:
    return bool(env.get("KDIVE_REMOTE_LIBVIRT_URI"))


REMOTE_LIBVIRT_URI = Setting(
    name="KDIVE_REMOTE_LIBVIRT_URI",
    parse=str,
    group="remote-libvirt",
    processes=_RT,
    help="qemu+tls host URI; its presence enables the remote-libvirt provider.",
)
REMOTE_LIBVIRT_CLIENT_CERT_REF = Setting(
    name="KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF",
    parse=str,
    secret=True,
    group="remote-libvirt",
    processes=_RT,
    required_when=_uri_set,
    suggest="set the mutual-TLS client cert secret ref",
)
REMOTE_LIBVIRT_CLIENT_KEY_REF = Setting(
    name="KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF",  # pragma: allowlist secret - env var name
    parse=str,
    secret=True,
    group="remote-libvirt",
    processes=_RT,
    required_when=_uri_set,
    suggest="set the mutual-TLS client key secret ref",
)
REMOTE_LIBVIRT_CA_CERT_REF = Setting(
    name="KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
    parse=str,
    secret=True,
    group="remote-libvirt",
    processes=_RT,
    required_when=_uri_set,
    suggest="set the CA cert secret ref",
)
REMOTE_LIBVIRT_ALLOCATION_CAP = Setting(
    name="KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP",
    parse=str,
    default="1",
    group="remote-libvirt",
    processes=_RT,
    help="Per-host concurrent-Allocation cap.",
)
REMOTE_LIBVIRT_STORAGE_POOL = Setting(
    name="KDIVE_REMOTE_LIBVIRT_STORAGE_POOL",
    parse=str,
    default="default",
    group="remote-libvirt",
    processes=_RT,
    help="libvirt storage pool for guest disks.",
)
REMOTE_LIBVIRT_NETWORK = Setting(
    name="KDIVE_REMOTE_LIBVIRT_NETWORK",
    parse=str,
    default="default",
    group="remote-libvirt",
    processes=_RT,
    help="libvirt network for guests.",
)
REMOTE_LIBVIRT_MACHINE = Setting(
    name="KDIVE_REMOTE_LIBVIRT_MACHINE",
    parse=str,
    default="pc",
    group="remote-libvirt",
    processes=_RT,
    help="QEMU machine type (pc/i440fx by default; q35 opt-in).",
)
REMOTE_LIBVIRT_GDB_ADDR = Setting(
    name="KDIVE_REMOTE_LIBVIRT_GDB_ADDR",
    parse=str,
    group="remote-libvirt",
    processes=_RT,
    help="gdbstub listen address (ACL'd boundary; no default, fails closed if unset).",
)
REMOTE_LIBVIRT_GDB_PORT_MIN = Setting(
    name="KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN",
    parse=str,
    default="47000",
    group="remote-libvirt",
    processes=_RT,
    help="Low end of the gdbstub port range.",
)
REMOTE_LIBVIRT_GDB_PORT_MAX = Setting(
    name="KDIVE_REMOTE_LIBVIRT_GDB_PORT_MAX",
    parse=str,
    default="47099",
    group="remote-libvirt",
    processes=_RT,
    help="High end of the gdbstub port range.",
)

SETTINGS = [
    REMOTE_LIBVIRT_URI,
    REMOTE_LIBVIRT_CLIENT_CERT_REF,
    REMOTE_LIBVIRT_CLIENT_KEY_REF,
    REMOTE_LIBVIRT_CA_CERT_REF,
    REMOTE_LIBVIRT_ALLOCATION_CAP,
    REMOTE_LIBVIRT_STORAGE_POOL,
    REMOTE_LIBVIRT_NETWORK,
    REMOTE_LIBVIRT_MACHINE,
    REMOTE_LIBVIRT_GDB_ADDR,
    REMOTE_LIBVIRT_GDB_PORT_MIN,
    REMOTE_LIBVIRT_GDB_PORT_MAX,
]
