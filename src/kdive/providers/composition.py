"""Active provider composition boundary.

This module is the only place the MCP and worker assembly path constructs local-libvirt
providers. It bootstraps a capability registry and exposes typed runtime adapters so the
MCP/tool and worker layers dispatch operations by provider capability rather than by concrete
provider name.
"""

from __future__ import annotations

from typing import cast

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
    BuildOutput,
    CaptureOutput,
    Connector,
    Controller,
    CrashOutput,
    CrashPostmortem,
    GdbMiEngine,
    Installer,
    IntrospectOutput,
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


class _DispatchedProvisioner:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def provision(self, *args: object, **kwargs: object) -> str:
        call = self._registry.dispatch(Plane.PROVISIONING, "provision", _LOCAL_KIND).call
        return cast(str, call(*args, **kwargs))

    def reprovision(self, *args: object, **kwargs: object) -> str:
        call = self._registry.dispatch(Plane.PROVISIONING, "reprovision", _LOCAL_KIND).call
        return cast(str, call(*args, **kwargs))

    def teardown(self, *args: object, **kwargs: object) -> None:
        call = self._registry.dispatch(Plane.PROVISIONING, "teardown", _LOCAL_KIND).call
        call(*args, **kwargs)


class _DispatchedBuilder:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def build(self, *args: object, **kwargs: object) -> BuildOutput:
        call = self._registry.dispatch(Plane.BUILD, "build", _LOCAL_KIND).call
        return cast(BuildOutput, call(*args, **kwargs))


class _DispatchedInstallerBooter:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def install(self, *args: object, **kwargs: object) -> None:
        call = self._registry.dispatch(Plane.INSTALL, "install", _LOCAL_KIND).call
        call(*args, **kwargs)

    def boot(self, *args: object, **kwargs: object) -> None:
        call = self._registry.dispatch(Plane.INSTALL, "boot", _LOCAL_KIND).call
        call(*args, **kwargs)


class _DispatchedConnector:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def open_transport(self, *args: object, **kwargs: object) -> object:
        call = self._registry.dispatch(Plane.CONNECT, "open_transport", _LOCAL_KIND).call
        return call(*args, **kwargs)

    def close_transport(self, *args: object, **kwargs: object) -> None:
        call = self._registry.dispatch(Plane.CONNECT, "close_transport", _LOCAL_KIND).call
        call(*args, **kwargs)


class _DispatchedController:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def power(self, *args: object, **kwargs: object) -> None:
        call = self._registry.dispatch(Plane.CONTROL, "power", _LOCAL_KIND).call
        call(*args, **kwargs)

    def force_crash(self, *args: object, **kwargs: object) -> None:
        call = self._registry.dispatch(Plane.CONTROL, "force_crash", _LOCAL_KIND).call
        call(*args, **kwargs)


class _DispatchedRetriever:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def capture(self, *args: object, **kwargs: object) -> CaptureOutput:
        call = self._registry.dispatch(Plane.RETRIEVE, "capture", _LOCAL_KIND).call
        return cast(CaptureOutput, call(*args, **kwargs))


class _DispatchedCrashPostmortem:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def run_crash_postmortem(self, *args: object, **kwargs: object) -> CrashOutput:
        call = self._registry.dispatch(Plane.RETRIEVE, "run_crash_postmortem", _LOCAL_KIND).call
        return cast(CrashOutput, call(*args, **kwargs))


class _DispatchedVmcoreIntrospector:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def from_vmcore(self, *args: object, **kwargs: object) -> IntrospectOutput:
        call = self._registry.dispatch(Plane.DEBUG, "from_vmcore", _LOCAL_KIND).call
        return cast(IntrospectOutput, call(*args, **kwargs))


class _DispatchedLiveIntrospector:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def introspect_live(self, *args: object, **kwargs: object) -> IntrospectOutput:
        call = self._registry.dispatch(Plane.DEBUG, "introspect_live", _LOCAL_KIND).call
        return cast(IntrospectOutput, call(*args, **kwargs))


class ProviderRuntime:
    """Typed adapters backed by the immutable capability registry."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def provisioner(self) -> Provisioner:
        return _DispatchedProvisioner(self._registry)

    def builder(self) -> Builder:
        return _DispatchedBuilder(self._registry)

    def install_boot(self) -> tuple[Installer, Booter]:
        install = _DispatchedInstallerBooter(self._registry)
        return install, install

    def connector(self) -> Connector:
        return cast(Connector, _DispatchedConnector(self._registry))

    def controller(self) -> Controller:
        return _DispatchedController(self._registry)

    def retriever(self) -> Retriever:
        return _DispatchedRetriever(self._registry)

    def crash_postmortem(self) -> CrashPostmortem:
        return _DispatchedCrashPostmortem(self._registry)

    def vmcore_introspector(self) -> VmcoreIntrospector:
        return _DispatchedVmcoreIntrospector(self._registry)

    def live_introspector(self) -> LiveIntrospector:
        return _DispatchedLiveIntrospector(self._registry)

    def attach_seam(self) -> AttachSeam:
        return default_attach_seam

    def debug_engine(self) -> GdbMiEngine:
        return cast(GdbMiEngine, LocalGdbMiEngine())


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
    _register_provider(
        registry,
        LocalLibvirtProvisioning.from_env(),
        [
            _capability(Plane.PROVISIONING, "provision", _LONG_RUNNING_CONTRACT),
            _capability(Plane.PROVISIONING, "teardown", _DESTRUCTIVE_LONG_RUNNING_CONTRACT),
            _capability(Plane.PROVISIONING, "reprovision", _DESTRUCTIVE_LONG_RUNNING_CONTRACT),
        ],
        "provisioning",
    )
    _register_provider(
        registry,
        LocalLibvirtBuild.from_env(),
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
    _register_provider(
        registry,
        LocalLibvirtConnect.from_env(),
        [
            _capability(Plane.CONNECT, "open_transport", _SYNC_CONTRACT),
            _capability(Plane.CONNECT, "close_transport", _SYNC_CONTRACT),
        ],
        "connect",
    )
    _register_provider(
        registry,
        LocalLibvirtControl.from_env(),
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
    _register_provider(
        registry,
        LocalLibvirtVmcoreIntrospect.from_env(),
        [_capability(Plane.DEBUG, "from_vmcore", _SYNC_CONTRACT)],
        "vmcore-introspect",
    )
    _register_provider(
        registry,
        LocalLibvirtLiveIntrospect.from_env(),
        [_capability(Plane.DEBUG, "introspect_live", _SYNC_CONTRACT)],
        "live-introspect",
    )
    return ProviderRuntime(registry)


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
