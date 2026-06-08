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
    _UploadRootfs,
)
from kdive.profiles.provisioning import (
    validate_profile as _validate_profile,
)
from kdive.providers.local_libvirt.discovery import _KDIVE_METADATA_NS
from kdive.providers.local_libvirt.lifecycle.materialize import (
    RootfsMaterializationContext,
    RootfsUploadContext,
    materialize_rootfs_base,
)
from kdive.providers.runtime_paths import console_log_path, domain_name_for

_log = logging.getLogger(__name__)

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"
_DEFAULT_MACHINE = "q35"
_ROOTFS_DIR = "/var/lib/kdive/rootfs"
_ROOTFS_CACHE_DIR = f"{_ROOTFS_DIR}/cache"
_QEMU_IMG_TIMEOUT_S = 5 * 60


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
    if isinstance(rootfs, _UploadRootfs):
        raise CategorizedError(
            "rootfs 'upload' kind requires systems.define + artifacts.create_system_upload first; "
            "use 'local', 'artifact', or 'catalog' for a one-step provision",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def render_domain_xml(system_id: UUID, profile: ProvisioningProfile, *, disk_path: str) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3).

    Renders the domain shell, the rootfs disk, the always-on serial console with a ``<log>``
    tee to ``_CONSOLE_DIR``, and the kdive metadata tag — no ``<kernel>``/``<cmdline>`` (the
    kdump ``crashkernel=`` reservation is the install/boot plane's, #17, and is inert without a
    ``<kernel>`` element). ``disk_path`` is explicit so rootfs materialization policy stays in
    the materialization plane; production passes the per-System overlay (ADR-0060).
    """
    _ensure_kdive_namespace_registered()
    _validate_profile(profile)
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
    ET.SubElement(disk, "source", file=disk_path)
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
    except FileNotFoundError as exc:
        raise CategorizedError(
            "qemu-img is not installed; cannot create the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("create_overlay", overlay, tool="qemu-img"),
        ) from exc
    except OSError as exc:
        details = _overlay_error_details("create_overlay", overlay, tool="qemu-img")
        details["error"] = type(exc).__name__
        raise CategorizedError(
            "failed to launch qemu-img to create the per-System rootfs overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "qemu-img exceeded the overlay creation timeout",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("create_overlay", overlay, tool="qemu-img"),
                "timeout_s": _QEMU_IMG_TIMEOUT_S,
            },
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to create the per-System rootfs overlay",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("create_overlay", overlay, tool="qemu-img"),
                "stderr": result.stderr[-2000:],
            },
        )


def _real_remove_overlay(overlay: str) -> None:
    """Remove a System's overlay file; an absent file is the achieved post-state (idempotent)."""
    try:
        Path(overlay).unlink(missing_ok=True)
    except OSError as exc:
        details = _overlay_error_details("remove_overlay", overlay)
        details["error"] = type(exc).__name__
        raise CategorizedError(
            "failed to remove the per-System rootfs overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc


def _overlay_error_details(op: str, overlay: str, *, tool: str | None = None) -> dict[str, object]:
    details: dict[str, object] = {"op": op, "overlay": Path(overlay).name}
    if tool is not None:
        details["tool"] = tool
    return details


def _real_overlay_exists(overlay: str) -> bool:
    return Path(overlay).exists()


type MakeOverlay = Callable[[str, str], None]
type RemoveOverlay = Callable[[str], None]
type OverlayExists = Callable[[str], bool]
type MaterializeRootfs = Callable[[RootfsSource, UUID], str]
type PrepareConsoleLog = Callable[[Path], None]


def _prepare_console_log(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o644, exist_ok=True)
        path.chmod(0o644)
    except OSError as exc:
        raise CategorizedError(
            "failed to prepare libvirt console log",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"path": str(path)},
        ) from exc


class LocalLibvirtProvisioning:
    """The realized provisioning port for the local libvirt host."""

    def __init__(
        self,
        *,
        connect: Connect,
        make_overlay: MakeOverlay = _real_make_overlay,
        remove_overlay: RemoveOverlay = _real_remove_overlay,
        overlay_exists: OverlayExists = _real_overlay_exists,
        allowed_roots: list[Path] | None = None,
        cache_dir: Path = Path(_ROOTFS_CACHE_DIR),
        materialize_rootfs: MaterializeRootfs | None = None,
        prepare_console_log: PrepareConsoleLog = _prepare_console_log,
    ) -> None:
        self._connect = connect
        self._make_overlay = make_overlay
        self._remove_overlay = remove_overlay
        self._overlay_exists = overlay_exists
        self._allowed_roots = allowed_roots or [Path(_ROOTFS_DIR)]
        self._cache_dir = cache_dir
        self._materialize_rootfs = materialize_rootfs or self._materialize_rootfs_base
        self._prepare_console_log = prepare_console_log

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
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid profile/rootfs input,
                ``MISSING_DEPENDENCY`` for unavailable rootfs materialization or ``qemu-img``,
                ``PROVISIONING_FAILURE`` for domain/rootfs creation failures, or
                ``INFRASTRUCTURE_FAILURE`` for provider control-plane or overlay IO faults.
        """
        base = self._materialize_rootfs(profile.provider.local_libvirt.rootfs, system_id)
        overlay = overlay_path(system_id)
        xml = render_domain_xml(system_id, profile, disk_path=overlay)  # validates the profile
        created_overlay = not self._overlay_exists(overlay)
        if created_overlay:
            self._make_overlay(base, overlay)  # the domain boots this overlay, not the base
        try:
            self._prepare_console_log(console_log_path(system_id))
            self._define_and_start(xml, system_id)
        except CategorizedError:
            self._cleanup_overlay_if_created(created_overlay, overlay)
            raise
        return domain_name_for(system_id)

    def _define_and_start(self, xml: str, system_id: UUID) -> None:
        try:
            conn = self._connect()
        except libvirt.libvirtError as exc:
            raise self._provisioning_failure(system_id) from exc
        try:
            domain = conn.defineXML(xml)
            try:
                domain.create()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                    return
                # Not "already running" — a real start failure. Undefine the domain we just
                # defined so provision stays transactional (a started domain or none). The
                # overlay is reclaimed by provision(), which catches this re-raise.
                try:
                    domain.undefine()
                except libvirt.libvirtError:
                    _log.warning(
                        "failed to undefine domain after a failed start; continuing",
                        exc_info=True,
                    )
                raise
        except libvirt.libvirtError as exc:
            raise self._provisioning_failure(system_id) from exc
        finally:
            _close(conn)

    def _cleanup_overlay_if_created(self, created_overlay: bool, overlay: str) -> None:
        if not created_overlay:
            return
        try:
            self._remove_overlay(overlay)
        except CategorizedError:
            _log.warning("failed to remove overlay after failed provision", exc_info=True)

    @staticmethod
    def _provisioning_failure(system_id: UUID) -> CategorizedError:
        return CategorizedError(
            "libvirt failed to define/start the domain",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"system_id": str(system_id)},
        )

    def validate_rootfs_ref(self, rootfs: RootfsSource) -> None:
        """Validate that a rootfs ref can materialize within provider roots."""
        self._materialize_rootfs_base(rootfs, UUID(int=0))

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

    def _materialize_rootfs_base(self, rootfs: RootfsSource, system_id: UUID) -> str:
        return str(
            materialize_rootfs_base(
                rootfs,
                context=RootfsMaterializationContext(
                    allowed_roots=self._allowed_roots,
                    upload=RootfsUploadContext("local", system_id, Path(_ROOTFS_DIR)),
                ),
            )
        )

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
