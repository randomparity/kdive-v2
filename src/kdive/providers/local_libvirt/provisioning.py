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
import subprocess  # noqa: S404 - qemu-img is invoked with a fixed argv, no shell
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RootfsSource,
    validate_profile,
)
from kdive.profiles.provisioning import (
    validate_rootfs_reference as validate_rootfs_reference,
)
from kdive.providers.local_libvirt.discovery import _KDIVE_METADATA_NS
from kdive.providers.ports import Provisioner as Provisioner
from kdive.providers.runtime_paths import console_log_path, domain_name_for
from kdive.rootfs.catalog import load_catalog

_log = logging.getLogger(__name__)

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"
_DEFAULT_MACHINE = "q35"
_ROOTFS_DIR = "/var/lib/kdive/rootfs"
_QEMU_IMG_TIMEOUT_S = 5 * 60
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}\Z")


def overlay_path(system_id: UUID | str) -> str:
    """The per-System qcow2 overlay path (a writable layer backed by the shared base image).

    Each System boots its own overlay so two domains never contend for the read-only base's
    qcow2 write lock and one System's writes never bleed into another (ADR-0060). Accepts the
    raw id string too, so ``teardown`` can derive it from the domain name without a UUID parse.
    """
    return f"{_ROOTFS_DIR}/{system_id}-overlay.qcow2"


_kdive_namespace_registered = False


def _ensure_kdive_namespace_registered() -> None:
    """Register the kdive XML prefix when rendering domain XML."""
    global _kdive_namespace_registered
    if _kdive_namespace_registered:
        return
    # ElementTree keeps namespace prefixes in process-global state. Keep that mutation out of
    # import time and perform it at the rendering boundary that needs deterministic prefixes.
    ET.register_namespace("kdive", _KDIVE_METADATA_NS)
    _kdive_namespace_registered = True


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
        # The System-owned uploaded object's local staging path. The object is committed
        # (its artifacts row written) at provisioning->ready by _commit_uploaded_rootfs;
        # staging the bytes down to this path is the install/boot spec's concern (ADR-0048 §7).
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


def reject_rootfs_without_upload_window(rootfs: RootfsSource) -> None:
    """Reject an ``upload`` rootfs in a lane that has no pre-provision upload window.

    An ``upload`` rootfs resolves a System-owned object that exists only after
    ``systems.define`` opens an upload window and the agent PUTs it (ADR-0048 §5). The
    one-step ``systems.provision`` *create* lane and ``systems.reprovision`` have no such
    window, so an ``upload`` reference there can never have a staged object — fail fast at the
    boundary rather than insert/replace and dead-letter (or leak a started domain) at commit.
    ``define`` and the worker do **not** call this guard.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an ``upload`` rootfs.
    """
    if rootfs.kind == "upload":
        raise CategorizedError(
            "rootfs 'upload' kind requires systems.define + artifacts.create_system_upload first; "
            "use 'path', 'url', or 'catalog' for a one-step provision",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def render_domain_xml(
    system_id: UUID, profile: ProvisioningProfile, *, disk_path: str | None = None
) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3).

    Renders the domain shell, the rootfs disk, the always-on serial console with a ``<log>``
    tee to ``_CONSOLE_DIR``, and the kdive metadata tag — no ``<kernel>``/``<cmdline>`` (the
    kdump ``crashkernel=`` reservation is the install/boot plane's, #17, and is inert without a
    ``<kernel>`` element). ``disk_path`` overrides the disk source: ``provision`` passes the
    per-System overlay (ADR-0060); a bare render (tests) defaults to the resolved base image.
    """
    _ensure_kdive_namespace_registered()
    validate_profile(profile)
    section = profile.provider.local_libvirt
    machine = section.domain_xml_params.get("machine", _DEFAULT_MACHINE)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    # A deterministic uuid (= the System id) makes `defineXML` redefine the System's existing
    # domain in place on a provision retry, instead of failing the name collision with a fresh
    # libvirt-assigned uuid ("domain already exists with uuid ...") — the libvirt-level half of
    # provision idempotency (ADR-0025; the unit test's fake defineXML cannot model this).
    ET.SubElement(domain, "uuid").text = str(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    # The rootfs images are qcow2 (build-guest-image.sh / virt-make-fs --format=qcow2). Without an
    # explicit driver type libvirt defaults to raw, so the guest would read the qcow2 header as the
    # start of the disk and fail to mount root; declare the format so /dev/vda is the ext4
    # filesystem, not the container metadata.
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    rootfs_path = (
        disk_path
        if disk_path is not None
        else resolve_rootfs_path(section.rootfs, tenant="local", system_id=system_id)
    )
    ET.SubElement(disk, "source", file=rootfs_path)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "log", file=str(console_log_path(system_id)))
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{_KDIVE_METADATA_NS}}}system").text = str(system_id)

    return ET.tostring(domain, encoding="unicode")


def _real_make_overlay(base: str, overlay: str) -> None:
    """Create the per-System qcow2 overlay backed by ``base`` with ``qemu-img`` (ADR-0060).

    ``-F qcow2`` names the backing format (the rootfs images are qcow2), so qemu-img does not
    format-probe the base. A non-zero exit is a ``PROVISIONING_FAILURE`` with a redacted stderr
    tail.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted paths
            ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, overlay],
            capture_output=True,
            text=True,
            timeout=_QEMU_IMG_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "qemu-img exceeded the overlay creation timeout",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"timeout_s": _QEMU_IMG_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to create the per-System rootfs overlay",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"stderr": result.stderr[-2000:]},
        )


def _real_remove_overlay(overlay: str) -> None:
    """Remove a System's overlay file; an absent file is the achieved post-state (idempotent)."""
    Path(overlay).unlink(missing_ok=True)


def _real_overlay_exists(overlay: str) -> bool:
    return Path(overlay).exists()


type MakeOverlay = Callable[[str, str], None]
type RemoveOverlay = Callable[[str], None]
type OverlayExists = Callable[[str], bool]


class LocalLibvirtProvisioning:
    """The realized provisioning port for the local libvirt host."""

    def __init__(
        self,
        *,
        connect: Connect,
        make_overlay: MakeOverlay = _real_make_overlay,
        remove_overlay: RemoveOverlay = _real_remove_overlay,
        overlay_exists: OverlayExists = _real_overlay_exists,
    ) -> None:
        self._connect = connect
        self._make_overlay = make_overlay
        self._remove_overlay = remove_overlay
        self._overlay_exists = overlay_exists

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
        running System failed. The overlay is created only when **absent**: a retry must never
        recreate the overlay a running QEMU holds open (qemu-img would fail the lock or truncate
        the live disk), so a present overlay is left in place (ADR-0060).

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` on any other libvirt error.
        """
        base = resolve_rootfs_path(
            profile.provider.local_libvirt.rootfs, tenant="local", system_id=system_id
        )
        overlay = overlay_path(system_id)
        xml = render_domain_xml(system_id, profile, disk_path=overlay)  # validates the profile
        created_overlay = not self._overlay_exists(overlay)
        if created_overlay:
            self._make_overlay(base, overlay)  # the domain boots this overlay, not the base
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
                        # The overlay is reclaimed by the outer handler, which catches this
                        # re-raise as well as a define failure.
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
            if created_overlay:
                self._remove_overlay(overlay)  # no started domain; reclaim the overlay we created
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
        """Destroy+undefine the domain and reclaim its per-System overlay; idempotent.

        The overlay is removed after the libvirt teardown — including the already-absent-domain
        path — so a torn-down System leaves no orphaned disk (ADR-0060). An absent overlay is a
        no-op (``unlink(missing_ok)``).

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any libvirt error other than the
                achieved post-states.
        """
        self._teardown_domain(domain_name)
        self._remove_overlay(overlay_path(domain_name.removeprefix("kdive-")))

    def _teardown_domain(self, domain_name: str) -> None:
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
