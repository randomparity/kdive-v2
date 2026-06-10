"""Co-located ``KDIVE_LIBVIRT_*`` settings for the local-libvirt provider (ADR-0087).

A dedicated, dependency-light module (imports only :class:`Setting`) so aggregating it
through the manifest never pulls the ``libvirt`` C-extension into a process that does
not use the provider. The provider's readers import these settings and resolve them via
``kdive.config.get``.
"""

from __future__ import annotations

from kdive.config.registry import Setting

_RT = frozenset({"worker", "reconciler"})

LIBVIRT_URI = Setting(
    name="KDIVE_LIBVIRT_URI",
    parse=str,
    default="qemu:///system",
    group="local-libvirt",
    processes=_RT,
    help="libvirt connection URI for the local host.",
)
LIBVIRT_ALLOCATION_CAP = Setting(
    name="KDIVE_LIBVIRT_ALLOCATION_CAP",
    parse=str,
    default="1",
    group="local-libvirt",
    processes=_RT,
    help="Per-host concurrent-Allocation cap.",
)

SETTINGS = [LIBVIRT_URI, LIBVIRT_ALLOCATION_CAP]
