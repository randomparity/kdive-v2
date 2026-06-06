"""Local-libvirt Install + boot plane: stage a direct-kernel boot, bring the System up (ADR-0030).

`LocalLibvirtInstall` realizes two handler-facing ports keyed on the System-tagged libvirt
domain (`kdive-{system_id}`, minted by the provisioning plane, ADR-0025):

- `install(system_id, run_id, kernel_ref, *, cmdline)` stages the built kernel/initrd to a
  **per-Run** host-local path (`{staging_root}/{system_id}/{run_id}/{kernel,initrd}`) via a
  temp-then-rename fetch, verifies the kdump capture prerequisite (`configuration_error` if
  absent — checked **before** any redefine), and `defineXML`s the domain with a direct-kernel
  `<os>` (`<kernel>`/`<initrd>`/`<cmdline>`) referencing that path. The `<os>` is built with
  `xml.etree.ElementTree` (no string interpolation), so a `cmdline` value cannot inject XML.
- `boot(system_id)` power-cycles the domain into the staged `<kernel>` (`destroy` if running,
  then `create`) and polls the run-readiness preflight within a bounded window: the System
  never answering is `boot_timeout`; answering-but-failing a check is `readiness_failure`; a
  libvirt error starting the domain is `install_failure`.

DB-free: it owns no Postgres — the `runs.*` install/boot handlers drive the step ledger.
The slow, host-bound seams (libvirt connect, object-store fetch, kdump/readiness checks, the
poll clock) are **injected**, so unit tests cover the orchestration/error contract without a
host; the real `libvirt.open`/object-store path is `live_vm`-only.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET  # noqa: S405 - constructs/edits self-owned domain XML only
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.provisioning import domain_name_for

_log = logging.getLogger(__name__)

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"
_STAGING_ENV = "KDIVE_INSTALL_STAGING"
_DEFAULT_STAGING = "/var/lib/kdive/install"
_DEFAULT_BOOT_WINDOW_POLLS = 30


class ReadinessResult(NamedTuple):
    """The run-readiness preflight result: did the System answer, and did its checks pass."""

    answered: bool
    ok: bool


class _LibvirtDomain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - mirrors the libvirt binding name
    def isActive(self) -> int: ...  # noqa: N802 - mirrors the libvirt binding name
    def create(self) -> int: ...
    def destroy(self) -> int: ...


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def defineXML(self, xml: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]
type Fetch = Callable[[str, Path], None]
type KdumpCheck = Callable[[UUID], bool]
type Readiness = Callable[[UUID], ReadinessResult]


class Installer(Protocol):
    """The handler-facing install port (the realized M0 contract), keyed on the System.

    `run_id` keys the per-Run staging path (ADR-0030 §5); `cmdline` is the gated command line
    (the `crashkernel=` reservation is enforced at the `runs.install` tool, before this runs).
    """

    def install(self, system_id: UUID, run_id: UUID, kernel_ref: str, *, cmdline: str) -> None: ...


class Booter(Protocol):
    """The handler-facing boot port: power-cycle the domain and confirm run-readiness."""

    def boot(self, system_id: UUID) -> None: ...


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


class LocalLibvirtInstall:
    """The realized `Installer` + `Booter` for the local libvirt host (ADR-0030)."""

    def __init__(
        self,
        *,
        connect: Connect,
        fetch_kernel: Fetch,
        fetch_initrd: Fetch,
        kdump_check: KdumpCheck,
        readiness: Readiness,
        staging_root: Path,
        boot_window_polls: int = _DEFAULT_BOOT_WINDOW_POLLS,
    ) -> None:
        self._connect = connect
        self._fetch_kernel = fetch_kernel
        self._fetch_initrd = fetch_initrd
        self._kdump_check = kdump_check
        self._readiness = readiness
        self._staging_root = staging_root
        self._boot_window_polls = boot_window_polls

    @classmethod
    def from_env(cls) -> LocalLibvirtInstall:
        """Build from the ``KDIVE_*`` environment; does not connect to libvirt or the store.

        The real object-store fetch and the real kdump/readiness preflight are `live_vm`-only
        seams (they need a host and a kernel tree), so they default to stubs that raise
        ``MISSING_DEPENDENCY`` off the gate — exactly as the build plane's real `make`/checkout
        seams do — and the worker registers its handlers without a host present.
        """
        host_uri = os.environ.get(_URI_ENV, _DEFAULT_URI)
        staging_root = Path(os.environ.get(_STAGING_ENV, _DEFAULT_STAGING))
        return cls(
            connect=lambda: libvirt.open(host_uri),
            fetch_kernel=_real_fetch,
            fetch_initrd=_real_fetch,
            kdump_check=_real_kdump_check,
            readiness=_real_readiness,
            staging_root=staging_root,
        )

    def install(self, system_id: UUID, run_id: UUID, kernel_ref: str, *, cmdline: str) -> None:
        """Stage the kernel/initrd and redefine the domain for direct-kernel boot.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the kdump capture path is absent
                (checked before any redefine); ``INSTALL_FAILURE`` on a libvirt redefine error;
                any fetch error category propagated from the object-store seam.
        """
        staging_dir = self._staging_root / str(system_id) / str(run_id)
        staging_dir.mkdir(parents=True, exist_ok=True)
        kernel_path = staging_dir / "kernel"
        initrd_path = staging_dir / "initrd"
        self._fetch_kernel(kernel_ref, kernel_path)
        self._fetch_initrd(kernel_ref, initrd_path)
        if not self._kdump_check(system_id):
            raise CategorizedError(
                "kdump capture service/initramfs not present on the staged System",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id)},
            )
        domain_name = domain_name_for(system_id)
        conn = self._open("for install")
        try:
            xml = self._render_direct_kernel_xml(
                conn, domain_name, kernel_path, initrd_path, cmdline
            )
            try:
                conn.defineXML(xml)
            except libvirt.libvirtError as exc:
                raise self._install_failure("redefining", domain_name) from exc
        finally:
            _close(conn)

    def boot(self, system_id: UUID) -> None:
        """Power-cycle the domain into the staged kernel and confirm run-readiness.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` if the domain is absent or libvirt cannot
                start it; ``BOOT_TIMEOUT`` if the System never answers within the boot window;
                ``READINESS_FAILURE`` if it answers but a readiness check fails.
        """
        domain_name = domain_name_for(system_id)
        conn = self._open("to boot")
        try:
            domain = self._lookup(conn, domain_name)
            self._power_cycle(domain, domain_name)
        finally:
            _close(conn)
        self._await_ready(system_id)

    def _render_direct_kernel_xml(
        self,
        conn: _LibvirtConn,
        domain_name: str,
        kernel_path: Path,
        initrd_path: Path,
        cmdline: str,
    ) -> str:
        """Read the existing domain XML and add a direct-kernel `<os>` section (ADR-0030 §5)."""
        try:
            domain = conn.lookupByName(domain_name)
            current = domain.XMLDesc(0)
        except libvirt.libvirtError as exc:
            raise self._install_failure("looking up", domain_name) from exc
        # `XMLDesc` crosses the same libvirtd trust boundary the discovery plane parses
        # with defusedxml: parse it the same way so a DOCTYPE/entity-expansion document
        # cannot become a billion-laughs DoS here. A malformed/forbidden document is a
        # clean install_failure, not a raw parser exception out of the handler.
        try:
            root = _safe_fromstring(current)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise self._install_failure("parsing the domain XML of", domain_name) from exc
        os_el = root.find("os")
        if os_el is None:
            os_el = ET.SubElement(root, "os")
        for tag in ("kernel", "initrd", "cmdline"):
            existing = os_el.find(tag)
            if existing is not None:
                os_el.remove(existing)
        ET.SubElement(os_el, "kernel").text = str(kernel_path)
        ET.SubElement(os_el, "initrd").text = str(initrd_path)
        ET.SubElement(os_el, "cmdline").text = cmdline
        return ET.tostring(root, encoding="unicode")

    def _power_cycle(self, domain: _LibvirtDomain, domain_name: str) -> None:
        try:
            if domain.isActive():
                domain.destroy()
            domain.create()
        except libvirt.libvirtError as exc:
            raise self._install_failure("power-cycling", domain_name) from exc

    def _await_ready(self, system_id: UUID) -> None:
        answered = False
        for _ in range(self._boot_window_polls):
            result = self._readiness(system_id)
            if result.answered:
                answered = True
                if result.ok:
                    return
                raise CategorizedError(
                    "System booted but a run-readiness check failed",
                    category=ErrorCategory.READINESS_FAILURE,
                    details={"system_id": str(system_id)},
                )
        category = ErrorCategory.READINESS_FAILURE if answered else ErrorCategory.BOOT_TIMEOUT
        raise CategorizedError(
            "System did not become ready within the boot window",
            category=category,
            details={"system_id": str(system_id)},
        )

    def _open(self, purpose: str) -> _LibvirtConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._install_failure(f"connecting to libvirt {purpose}", "install") from exc

    @staticmethod
    def _lookup(conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise LocalLibvirtInstall._install_failure("looking up", domain_name) from exc

    @staticmethod
    def _install_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INSTALL_FAILURE,
            details={"domain": domain_name},
        )


def read_console_log(path: Path) -> bytes:
    """Read the System's console log; absent → empty (boot may not have written).

    A ``PermissionError`` (the worker cannot read qemu's ``0600`` log — see Task 2.4's
    group setup) is treated as empty but **logged**, so a permission fault is never a
    silent empty console.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except PermissionError:
        _log.warning("console log %s not readable by the worker; registering empty", path)
        return b""


def _real_fetch(kernel_ref: str, dest: Path) -> None:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real object-store fetch runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"kernel_ref": kernel_ref, "dest": str(dest)},
    )


def _real_kdump_check(system_id: UUID) -> bool:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real kdump preflight runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id)},
    )


def _real_readiness(system_id: UUID) -> ReadinessResult:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real run-readiness preflight runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id)},
    )


__all__ = ["Booter", "Installer", "LocalLibvirtInstall", "ReadinessResult", "read_console_log"]
