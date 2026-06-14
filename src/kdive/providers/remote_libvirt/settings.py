"""Operational ``KDIVE_REMOTE_LIBVIRT_*`` host knobs (ADR-0087, ADR-0112).

A dependency-light module (imports only :class:`Setting`). The connection identity (URI, TLS cert
refs, gdbstub address/range, base image, allocation cap) moved to the declarative ``systems.toml``
``[[remote_libvirt]]`` inventory instance (M2.6 Phase 3, #395); only the libvirt host topology
knobs the v2 model does not carry — storage pool, network, and QEMU machine type — remain env
settings here.
"""

from __future__ import annotations

from kdive.config.registry import Setting

_RT = frozenset({"worker", "reconciler"})


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

SETTINGS = [
    REMOTE_LIBVIRT_STORAGE_POOL,
    REMOTE_LIBVIRT_NETWORK,
    REMOTE_LIBVIRT_MACHINE,
]
