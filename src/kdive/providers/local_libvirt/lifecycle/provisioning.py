"""Local-libvirt Provisioning plane: define/start and destroy/undefine a tagged domain (ADR-0025).

`LocalLibvirtProvisioning` renders a domain XML from a `ProvisioningProfile` (tagged with the
System id in the kdive metadata element discovery reads), `defineXML`+`create`s it on
`provision`, and `destroy`+`undefine`s it idempotently on `teardown`, over an injected
connection factory (unit tests never touch a real host; the real `libvirt.open` adapter is
`live_vm`-only). It owns no Postgres — the `systems.*` handlers drive the state machine.

Storage file lifecycle is delegated to ``lifecycle.storage`` and pure XML rendering to
``lifecycle.xml``. This facade owns materialization, libvirt define/start, and teardown
orchestration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RootfsSource,
    _UploadRootfs,
    validate_rootfs_reference,
)
from kdive.provider_components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
)
from kdive.providers.local_libvirt.lifecycle.materialize import (
    MaterializableRootfsRef,
    RootfsMaterializationContext,
    RootfsUploadContext,
    materialize_rootfs_base,
)
from kdive.providers.local_libvirt.lifecycle.storage import (
    ROOTFS_DIR,
    ProvisioningFiles,
    overlay_path,
)
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.runtime_paths import console_log_path, domain_name_for

__all__ = [
    "LocalLibvirtProvisioning",
    "ProvisioningFiles",
    "console_log_path",
    "domain_name_for",
    "overlay_path",
    "reject_rootfs_without_upload_window",
    "render_domain_xml",
]

_log = logging.getLogger(__name__)


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


type MaterializeRootfs = Callable[[RootfsSource, UUID], str]


def _materializable_rootfs(rootfs: RootfsSource) -> MaterializableRootfsRef:
    if isinstance(rootfs, LocalComponentRef | CatalogComponentRef | _UploadRootfs):
        return rootfs
    if isinstance(rootfs, ArtifactComponentRef):
        raise CategorizedError(
            "artifact-backed rootfs materialization is not wired yet",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    raise CategorizedError(
        "unsupported rootfs component reference",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


class LocalLibvirtProvisioning:
    """The realized provisioning port for the local libvirt host."""

    def __init__(
        self,
        *,
        connect: Connect,
        files: ProvisioningFiles | None = None,
        allowed_roots: list[Path] | None = None,
        materialize_rootfs: MaterializeRootfs | None = None,
    ) -> None:
        self._connect = connect
        self._files = files or ProvisioningFiles()
        self._allowed_roots = allowed_roots or [Path(ROOTFS_DIR)]
        self._materialize_rootfs = materialize_rootfs or self._materialize_rootfs_base

    @classmethod
    def from_env(cls) -> LocalLibvirtProvisioning:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        host_uri = config.require(LIBVIRT_URI)
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
        overlay = self._files.prepare_overlay(system_id, base=base)
        xml = render_domain_xml(system_id, profile, disk_path=overlay.path)  # validates the profile
        try:
            self._files.prepare_console(system_id)
            self._define_and_start(xml, system_id)
        except CategorizedError:
            self._files.cleanup_overlay_if_created(overlay)
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

    @staticmethod
    def _provisioning_failure(system_id: UUID) -> CategorizedError:
        return CategorizedError(
            "libvirt failed to define/start the domain",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"system_id": str(system_id)},
        )

    def validate_rootfs_ref(self, rootfs: RootfsSource) -> None:
        """Validate that a rootfs ref is statically resolvable.

        A ``catalog`` reference is validated by name against the baseline catalog (its object is
        resolved at provision time through the DB-backed materialize fetch, ADR-0092, which needs
        a connection this admission-time validator does not hold); a ``local``/``upload``
        reference is validated by materializing it within the provider roots.
        """
        if isinstance(rootfs, CatalogComponentRef):
            validate_rootfs_reference(rootfs)
            return
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
        self._files.remove_overlay_for_domain(domain_name)

    def _materialize_rootfs_base(self, rootfs: RootfsSource, system_id: UUID) -> str:
        rootfs = _materializable_rootfs(rootfs)
        return str(
            materialize_rootfs_base(
                rootfs,
                context=RootfsMaterializationContext(
                    allowed_roots=self._allowed_roots,
                    upload=RootfsUploadContext("local", system_id, Path(ROOTFS_DIR)),
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
