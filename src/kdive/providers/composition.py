"""Active provider composition boundary.

This module is the only place the MCP and worker assembly path constructs local-libvirt
providers. It bootstraps a capability registry and exposes typed runtime adapters so the
MCP/tool and worker layers dispatch operations by provider capability rather than by concrete
provider name.
"""

from __future__ import annotations

from kdive.domain.models import ResourceKind
from kdive.domain.state import ResourceStatus
from kdive.providers.capability import (
    Capability,
    CapabilityRegistry,
    CleanupGuarantee,
    OpContract,
    Plane,
)
from kdive.providers.local_libvirt.build import (
    LocalLibvirtBuild,
    validate_external_artifacts,
)
from kdive.providers.local_libvirt.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.control import LocalLibvirtControl
from kdive.providers.local_libvirt.debug_gdbmi import (
    GdbMiEngine as LocalGdbMiEngine,
)
from kdive.providers.local_libvirt.debug_gdbmi import (
    default_attach_seam,
)
from kdive.providers.local_libvirt.install import LocalLibvirtInstall, read_console_log
from kdive.providers.local_libvirt.introspect_drgn import (
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
)
from kdive.providers.local_libvirt.provisioning import (
    LocalLibvirtProvisioning,
    console_log_path,
    domain_name_for,
    reject_rootfs_without_upload_window,
    validate_profile,
)
from kdive.providers.local_libvirt.retrieve import (
    LocalLibvirtRetrieve,
    crash_command_rejection_reason,
)
from kdive.providers.ports import (
    AttachSeam,
    Booter,
    Builder,
    Connector,
    Controller,
    CrashPostmortem,
    GdbMiEngine,
    Installer,
    LiveIntrospector,
    Provisioner,
    Retriever,
    VmcoreIntrospector,
)

_LOCAL_PROVIDER_ID = "local-libvirt"
_LOCAL_KIND = ResourceKind.LOCAL_LIBVIRT
_LOCAL_COST_CLASS = "local"

_SYNC_CONTRACT = OpContract(
    idempotent=True,
    destructive=False,
    cancelable=False,
    long_running=False,
    cleanup=CleanupGuarantee.BEST_EFFORT,
)
_LONG_RUNNING_CONTRACT = OpContract(
    idempotent=True,
    destructive=False,
    cancelable=False,
    long_running=True,
    cleanup=CleanupGuarantee.BEST_EFFORT,
)
_DESTRUCTIVE_LONG_RUNNING_CONTRACT = OpContract(
    idempotent=True,
    destructive=True,
    cancelable=False,
    long_running=True,
    cleanup=CleanupGuarantee.BEST_EFFORT,
)


def _capability(plane: Plane, operation: str, contract: OpContract) -> Capability:
    return Capability(
        plane=plane,
        operation=operation,
        resource_kind=_LOCAL_KIND,
        contract=contract,
    )


class ProviderRuntime:
    """Typed provider ports for the default runtime."""

    def __init__(
        self,
        *,
        provisioner: Provisioner,
        builder: Builder,
        installer: Installer,
        booter: Booter,
        connector: Connector,
        controller: Controller,
        retriever: Retriever,
        crash_postmortem: CrashPostmortem,
        vmcore_introspector: VmcoreIntrospector,
        live_introspector: LiveIntrospector,
        attach_seam: AttachSeam = default_attach_seam,
        debug_engine: GdbMiEngine | None = None,
    ) -> None:
        self._provisioner = provisioner
        self._builder = builder
        self._installer = installer
        self._booter = booter
        self._connector = connector
        self._controller = controller
        self._retriever = retriever
        self._crash_postmortem = crash_postmortem
        self._vmcore_introspector = vmcore_introspector
        self._live_introspector = live_introspector
        self._attach_seam = attach_seam
        self._debug_engine = debug_engine if debug_engine is not None else LocalGdbMiEngine()

    def provisioner(self) -> Provisioner:
        return self._provisioner

    def builder(self) -> Builder:
        return self._builder

    def install_boot(self) -> tuple[Installer, Booter]:
        return self._installer, self._booter

    def connector(self) -> Connector:
        return self._connector

    def controller(self) -> Controller:
        return self._controller

    def retriever(self) -> Retriever:
        return self._retriever

    def crash_postmortem(self) -> CrashPostmortem:
        return self._crash_postmortem

    def vmcore_introspector(self) -> VmcoreIntrospector:
        return self._vmcore_introspector

    def live_introspector(self) -> LiveIntrospector:
        return self._live_introspector

    def attach_seam(self) -> AttachSeam:
        return self._attach_seam

    def debug_engine(self) -> GdbMiEngine:
        return self._debug_engine


def _register_provider(
    registry: CapabilityRegistry, provider: object, capabilities: list[Capability], suffix: str
) -> None:
    registry.register(
        provider,
        capabilities,
        provider_id=f"{_LOCAL_PROVIDER_ID}:{suffix}",
        health=ResourceStatus.AVAILABLE,
        cost_class=_LOCAL_COST_CLASS,
    )


def build_default_provider_runtime() -> ProviderRuntime:
    """Build the default runtime provider registry without opening live provider connections."""
    registry = CapabilityRegistry()
    provisioner = LocalLibvirtProvisioning.from_env()
    _register_provider(
        registry,
        provisioner,
        [
            _capability(Plane.PROVISIONING, "provision", _LONG_RUNNING_CONTRACT),
            _capability(Plane.PROVISIONING, "teardown", _DESTRUCTIVE_LONG_RUNNING_CONTRACT),
            _capability(Plane.PROVISIONING, "reprovision", _DESTRUCTIVE_LONG_RUNNING_CONTRACT),
        ],
        "provisioning",
    )
    builder = LocalLibvirtBuild.from_env()
    _register_provider(
        registry,
        builder,
        [_capability(Plane.BUILD, "build", _LONG_RUNNING_CONTRACT)],
        "build",
    )
    install = LocalLibvirtInstall.from_env()
    _register_provider(
        registry,
        install,
        [
            _capability(Plane.INSTALL, "install", _LONG_RUNNING_CONTRACT),
            _capability(Plane.INSTALL, "boot", _LONG_RUNNING_CONTRACT),
        ],
        "install",
    )
    connector = LocalLibvirtConnect.from_env()
    _register_provider(
        registry,
        connector,
        [
            _capability(Plane.CONNECT, "open_transport", _SYNC_CONTRACT),
            _capability(Plane.CONNECT, "close_transport", _SYNC_CONTRACT),
        ],
        "connect",
    )
    controller = LocalLibvirtControl.from_env()
    _register_provider(
        registry,
        controller,
        [
            _capability(Plane.CONTROL, "power", _DESTRUCTIVE_LONG_RUNNING_CONTRACT),
            _capability(Plane.CONTROL, "force_crash", _DESTRUCTIVE_LONG_RUNNING_CONTRACT),
        ],
        "control",
    )
    retrieve = LocalLibvirtRetrieve.from_env()
    _register_provider(
        registry,
        retrieve,
        [
            _capability(Plane.RETRIEVE, "capture", _LONG_RUNNING_CONTRACT),
            _capability(Plane.RETRIEVE, "run_crash_postmortem", _SYNC_CONTRACT),
        ],
        "retrieve",
    )
    vmcore_introspector = LocalLibvirtVmcoreIntrospect.from_env()
    _register_provider(
        registry,
        vmcore_introspector,
        [_capability(Plane.DEBUG, "from_vmcore", _SYNC_CONTRACT)],
        "vmcore-introspect",
    )
    live_introspector = LocalLibvirtLiveIntrospect.from_env()
    _register_provider(
        registry,
        live_introspector,
        [_capability(Plane.DEBUG, "introspect_live", _SYNC_CONTRACT)],
        "live-introspect",
    )
    return ProviderRuntime(
        provisioner=provisioner,
        builder=builder,
        installer=install,
        booter=install,
        connector=connector,
        controller=controller,
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=vmcore_introspector,
        live_introspector=live_introspector,
    )


def provisioner_from_env() -> Provisioner:
    return build_default_provider_runtime().provisioner()


def controller_from_env() -> Controller:
    return build_default_provider_runtime().controller()


def builder_from_env() -> Builder:
    return build_default_provider_runtime().builder()


def install_boot_from_env() -> tuple[Installer, Booter]:
    return build_default_provider_runtime().install_boot()


def connector_from_env() -> Connector:
    return build_default_provider_runtime().connector()


def attach_seam_from_env() -> AttachSeam:
    return build_default_provider_runtime().attach_seam()


def debug_engine_from_env() -> GdbMiEngine:
    return build_default_provider_runtime().debug_engine()


def retriever_from_env() -> Retriever:
    return build_default_provider_runtime().retriever()


def crash_postmortem_from_env() -> CrashPostmortem:
    return build_default_provider_runtime().crash_postmortem()


def vmcore_introspector_from_env() -> VmcoreIntrospector:
    return build_default_provider_runtime().vmcore_introspector()


def live_introspector_from_env() -> LiveIntrospector:
    return build_default_provider_runtime().live_introspector()


__all__ = [
    "ProviderRuntime",
    "build_default_provider_runtime",
    "builder_from_env",
    "attach_seam_from_env",
    "connector_from_env",
    "console_log_path",
    "controller_from_env",
    "crash_command_rejection_reason",
    "crash_postmortem_from_env",
    "domain_name_for",
    "install_boot_from_env",
    "live_introspector_from_env",
    "provisioner_from_env",
    "read_console_log",
    "reject_rootfs_without_upload_window",
    "retriever_from_env",
    "validate_external_artifacts",
    "validate_profile",
    "vmcore_introspector_from_env",
]
