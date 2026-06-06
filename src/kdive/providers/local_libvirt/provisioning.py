"""Local-libvirt Provisioning plane: define/start and destroy/undefine a tagged domain (ADR-0025).

`LocalLibvirtProvisioning` renders a domain XML from a `ProvisioningProfile` (tagged with the
System id in the kdive metadata element discovery reads), `defineXML`+`create`s it on
`provision`, and `destroy`+`undefine`s it idempotently on `teardown`, over an injected
connection factory (unit tests never touch a real host; the real `libvirt.open` adapter is
`live_vm`-only). It owns no Postgres — the `systems.*` handlers drive the state machine.

The domain XML is *constructed* with `xml.etree.ElementTree` (no string interpolation, so a
profile value cannot inject XML; no untrusted-input parse here, so no XXE surface). It renders
the domain shell, the rootfs disk, and the metadata tag — no `<kernel>`/`<cmdline>`: libvirt
ignores `<os><cmdline>` without a `<kernel>` element, and the test kernel plus its
`crashkernel=` kdump reservation are the install/boot plane's (#17).
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.providers.local_libvirt.discovery import _KDIVE_METADATA_NS
from kdive.rootfs.catalog import load_catalog

_log = logging.getLogger(__name__)

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"
_DEFAULT_MACHINE = "q35"
SUPPORTED_DOMAIN_XML_PARAMS = frozenset({"machine"})
_ROOTFS_DIR = "/var/lib/kdive/rootfs"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}\Z")

# Register the kdive metadata prefix once at import (global ElementTree state) so the
# rendered tag serializes as `kdive:system` rather than an auto-generated `ns0:` prefix.
ET.register_namespace("kdive", _KDIVE_METADATA_NS)


class _LibvirtDomain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _LibvirtConn(Protocol):
    def defineXML(self, xml: str) -> _LibvirtDomain: ...
    def lookupByName(self, name: str) -> _LibvirtDomain: ...
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


class Provisioner(Protocol):
    """The handler-facing provisioning port (the realized M0 contract).

    Distinct from :class:`kdive.providers.interfaces.ProvisioningPlane`, the capability-dispatch
    placeholder that keys on the *Allocation*: row-first ordering (ADR-0021/0025) mints the
    System **before** provisioning, so this port keys on the already-minted ``system_id`` and
    returns the libvirt domain name the handler stores and later tears down.
    :class:`LocalLibvirtProvisioning` satisfies it structurally; the `systems.*` job handlers
    depend on it so tests can inject a fake provider without a libvirt host. Reconciling the
    capability-dispatch Protocol with the realized providers is deferred to the
    capability-dispatch integration (provisioning is not dispatched through the registry in M0).
    """

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str: ...
    def teardown(self, domain_name: str) -> None: ...
    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str: ...


def domain_name_for(system_id: UUID) -> str:
    """The deterministic libvirt domain name for a System."""
    return f"kdive-{system_id}"


def resolve_rootfs_path(rootfs: RootfsSource, *, tenant: str, system_id: UUID) -> str:
    """Resolve a rootfs source to the libvirt-readable disk path (ADR-0048 §5).

    ``path`` is the declared file; ``upload`` is the System-owned object's local staging
    path; ``url``/``catalog`` resolve to a content-/name-addressed staging path under the
    rootfs dir (fetch lands in the next spec). The reference is validated here; existence
    of an unfetched image is the next spec's concern.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a malformed url checksum or unknown
            catalog name.
    """
    if rootfs.kind == "path":
        return rootfs.path
    if rootfs.kind == "upload":
        # Forward-plumbing for the worker: the tool boundary (validate_rootfs_reference)
        # rejects an upload reference until the DEFINED producer lands (#111), so no
        # persisted upload profile reaches this branch yet.
        return f"{_ROOTFS_DIR}/{tenant}-systems-{system_id}-rootfs.qcow2"
    if rootfs.kind == "url":
        if not _SHA256.match(rootfs.sha256):
            raise CategorizedError(
                "rootfs url sha256 must be 'sha256:<64-hex>'",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return f"{_ROOTFS_DIR}/url-{rootfs.sha256.removeprefix('sha256:')}.qcow2"
    entry = load_catalog().lookup(rootfs.name)
    if entry is None:
        raise CategorizedError(
            f"unknown rootfs catalog name: {rootfs.name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": rootfs.name},
        )
    return f"{_ROOTFS_DIR}/{entry.name}.qcow2"


def validate_rootfs_reference(rootfs: RootfsSource) -> None:
    """Validate a rootfs reference's resolvability (a synchronous tool-boundary check).

    Mirrors :func:`resolve_rootfs_path`'s static checks (url sha256 format, catalog-name
    existence) but needs no ``system_id`` — so ``systems.provision`` rejects a bad reference
    synchronously as ``configuration_error`` instead of dead-lettering the provision job.
    ``path`` needs no static check. ``upload`` is rejected here until its producer lands
    (#111): nothing creates the ``DEFINED`` System that opens a rootfs upload window, so an
    ``upload`` reference can never have a staged object — fail fast at the boundary rather
    than insert a System and dead-letter (or leak a started domain) at commit time.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a malformed url checksum, unknown
            catalog name, or the not-yet-available ``upload`` kind.
    """
    if rootfs.kind == "upload":
        raise CategorizedError(
            "rootfs 'upload' kind is not yet available (no create-without-provision path); "
            "use 'path', 'url', or 'catalog' (#111)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if rootfs.kind == "url" and not _SHA256.match(rootfs.sha256):
        raise CategorizedError(
            "rootfs url sha256 must be 'sha256:<64-hex>'",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if rootfs.kind == "catalog" and load_catalog().lookup(rootfs.name) is None:
        raise CategorizedError(
            f"unknown rootfs catalog name: {rootfs.name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": rootfs.name},
        )


def validate_profile(profile: ProvisioningProfile) -> None:
    """Reject a profile whose libvirt ``domain_xml_params`` carry an unsupported key.

    Called at the tool boundary so a bad param is a synchronous ``configuration_error``
    response, not a dead-lettered provision job. Also validates the rootfs reference's
    static resolvability (ADR-0048 §5).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` naming the unsupported key(s), or for an
            unresolvable rootfs reference.
    """
    params = profile.provider.local_libvirt.domain_xml_params
    unknown = sorted(set(params) - SUPPORTED_DOMAIN_XML_PARAMS)
    if unknown:
        raise CategorizedError(
            f"unsupported domain_xml_params: {', '.join(unknown)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"unsupported": unknown, "supported": sorted(SUPPORTED_DOMAIN_XML_PARAMS)},
        )
    validate_rootfs_reference(profile.provider.local_libvirt.rootfs)


def render_domain_xml(system_id: UUID, profile: ProvisioningProfile) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3).

    Renders the domain shell, the rootfs disk, and the kdive metadata tag — no
    ``<kernel>``/``<cmdline>`` (the kdump ``crashkernel=`` reservation is the install/boot
    plane's, #17, and is inert without a ``<kernel>`` element).
    """
    validate_profile(profile)
    section = profile.provider.local_libvirt
    machine = section.domain_xml_params.get("machine", _DEFAULT_MACHINE)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    rootfs_path = resolve_rootfs_path(section.rootfs, tenant="local", system_id=system_id)
    ET.SubElement(disk, "source", file=rootfs_path)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{_KDIVE_METADATA_NS}}}system").text = str(system_id)

    return ET.tostring(domain, encoding="unicode")


class LocalLibvirtProvisioning:
    """The `ProvisioningPlane` for the local libvirt host (define/start, destroy/undefine)."""

    def __init__(self, *, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_env(cls) -> LocalLibvirtProvisioning:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        host_uri = os.environ.get(_URI_ENV, _DEFAULT_URI)
        # `virConnect` structurally satisfies the narrow `_LibvirtConn` Protocol (only
        # `defineXML`/`lookupByName`), so no suppression is needed at this seam.
        return cls(connect=lambda: libvirt.open(host_uri))

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Define and start the tagged domain; return its name.

        Idempotent: ``defineXML`` redefines an existing domain, and a ``create`` that reports
        the domain is **already running** (``VIR_ERR_OPERATION_INVALID``) is the desired
        post-state, not a failure — so a handler retry after a partial provision does not mark a
        running System failed.

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` on any other libvirt error.
        """
        xml = render_domain_xml(system_id, profile)
        try:
            conn = self._connect()
            try:
                domain = conn.defineXML(xml)
                try:
                    domain.create()
                except libvirt.libvirtError as exc:
                    if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                        # Not "already running" — a real start failure. Undefine the domain we
                        # just defined so provision stays transactional (a started domain or
                        # none); swallow an undefine error so it cannot mask the start failure.
                        try:
                            domain.undefine()
                        except libvirt.libvirtError:
                            _log.warning(
                                "failed to undefine domain after a failed start; continuing",
                                exc_info=True,
                            )
                        raise
            finally:
                _close(conn)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "libvirt failed to define/start the domain",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc
        return domain_name_for(system_id)

    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Wipe the System's current install and define+start the new profile in place.

        Destructive (ADR-0038 §3): destroys+undefines the System's current domain, then
        defines+starts the new profile under the **same** deterministic domain name (the
        ``system_id`` is stable). Built from the idempotent ``teardown``/``provision``
        primitives — an absent prior domain is swallowed by ``teardown`` (so a retry after a
        partial wipe still provisions), and a ``provision`` failure surfaces as
        ``PROVISIONING_FAILURE`` (so the handler drives ``reprovisioning -> failed``).

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` if the new domain cannot be
                defined/started; ``INFRASTRUCTURE_FAILURE`` if the wipe cannot be completed.
        """
        self.teardown(domain_name_for(system_id))
        return self.provision(system_id, profile)

    def teardown(self, domain_name: str) -> None:
        """Destroy and undefine the domain; idempotent over an already-absent domain.

        "No such domain" on lookup/undefine and "not running" on destroy are the achieved
        post-state, so they are swallowed; any other libvirt error fails.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any other libvirt error.
        """
        try:
            conn = self._connect()
        except libvirt.libvirtError as exc:
            raise self._infra("connecting to libvirt to tear down", domain_name) from exc
        try:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return  # already gone
                raise self._infra("looking up", domain_name) from exc
            try:
                domain.destroy()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                    raise self._infra("destroying", domain_name) from exc
            try:
                domain.undefine()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                    raise self._infra("undefining", domain_name) from exc
        finally:
            _close(conn)

    @staticmethod
    def _infra(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        )
