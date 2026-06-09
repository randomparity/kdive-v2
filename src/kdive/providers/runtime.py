"""Neutral provider runtime contract.

The dataclass in this module is the high-level MCP and worker provider seam. It imports only
provider port protocols and domain value types; concrete provider assembly stays in
``kdive.providers.composition``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.profiles.provisioning import RootfsSource
from kdive.provider_components.references import ComponentRef
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.ports import (
    AttachSeam,
    Booter,
    Builder,
    Connector,
    Controller,
    CrashPostmortem,
    GdbBreakpointRef,
    GdbMiAttachment,
    GdbMiEngine,
    GdbStopRecord,
    Installer,
    LiveIntrospector,
    Provisioner,
    Retriever,
    VmcoreIntrospector,
)

type DiscoveryRegistrar = Callable[[AsyncConnectionPool], Awaitable[None]]
type BuildConfigValidator = Callable[[ComponentRef], None]
type RootfsValidator = Callable[[RootfsSource], None]


def _unconfigured_component_sources() -> ComponentSourceCapabilities:
    return ComponentSourceCapabilities(provider="unconfigured", accepted_component_sources={})


def _missing_attach_seam(
    *,
    host: str,
    port: int,
    run_id: str,
    transcript_path: Path,
) -> GdbMiAttachment:
    raise RuntimeError(
        "debug attach seam is not configured for this ProviderRuntime; "
        "build the runtime through kdive.providers.composition"
    )


class _UnavailableGdbMiEngine:
    def _raise(self) -> NoReturn:
        raise RuntimeError(
            "gdb/MI engine is not configured for this ProviderRuntime; "
            "build the runtime through kdive.providers.composition"
        )

    def set_breakpoint(self, attachment: GdbMiAttachment, location: str) -> GdbBreakpointRef:
        self._raise()

    def clear_breakpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        self._raise()

    def list_breakpoints(self, attachment: GdbMiAttachment) -> list[GdbBreakpointRef]:
        self._raise()

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        self._raise()

    def read_registers(
        self, attachment: GdbMiAttachment, register_names: list[str]
    ) -> dict[str, object]:
        self._raise()

    def continue_(self, attachment: GdbMiAttachment, *, timeout_sec: float) -> GdbStopRecord:
        self._raise()

    def interrupt(self, attachment: GdbMiAttachment) -> GdbStopRecord | None:
        self._raise()


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """Typed provider ports for the active runtime."""

    provisioner: Provisioner
    builder: Builder
    installer: Installer
    booter: Booter
    connector: Connector
    controller: Controller
    retriever: Retriever
    crash_postmortem: CrashPostmortem
    vmcore_introspector: VmcoreIntrospector
    live_introspector: LiveIntrospector
    supported_capture_methods: frozenset[CaptureMethod] = field(
        default_factory=lambda: frozenset(CaptureMethod)
    )
    discovery_registrar: DiscoveryRegistrar | None = None
    attach_seam: AttachSeam = _missing_attach_seam
    debug_engine: GdbMiEngine = field(default_factory=_UnavailableGdbMiEngine)
    component_sources: ComponentSourceCapabilities = field(
        default_factory=_unconfigured_component_sources
    )
    build_config_validator: BuildConfigValidator | None = None
    rootfs_validator: RootfsValidator | None = None

    async def register_discovery(self, pool: AsyncConnectionPool) -> None:
        if self.discovery_registrar is not None:
            await self.discovery_registrar(pool)
