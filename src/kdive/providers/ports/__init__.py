"""Handler-facing provider ports used by MCP tools and worker handlers.

Concrete providers satisfy these contracts structurally. Provider implementation modules may
import these types, but MCP and worker code should not import provider-specific contracts.

This package-level facade is the stable import surface for callers; implementation ownership is
split by provider plane in sibling modules.
"""

from __future__ import annotations

from kdive.providers.ports.build import Builder, TransportCapableBuilder
from kdive.providers.ports.debug import (
    AttachSeam,
    GdbBreakpointRef,
    GdbController,
    GdbFrame,
    GdbMiAttachment,
    GdbMiEngine,
    GdbStopRecord,
)
from kdive.providers.ports.handles import OwnedInfra, SystemHandle, TransportHandle
from kdive.providers.ports.lifecycle import (
    Booter,
    Connector,
    Controller,
    Installer,
    InstallRequest,
    Provisioner,
    TransportHandleData,
    TransportHandleKind,
)
from kdive.providers.ports.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashPostmortem,
    CrashResult,
    IntrospectOutput,
    LiveIntrospector,
    Retriever,
    VmcoreIntrospector,
)

__all__ = [
    "AttachSeam",
    "Booter",
    "Builder",
    "CaptureOutput",
    "Connector",
    "Controller",
    "CrashOutput",
    "CrashPostmortem",
    "CrashResult",
    "GdbBreakpointRef",
    "GdbController",
    "GdbFrame",
    "GdbMiAttachment",
    "GdbMiEngine",
    "GdbStopRecord",
    "InstallRequest",
    "Installer",
    "IntrospectOutput",
    "LiveIntrospector",
    "OwnedInfra",
    "Provisioner",
    "Retriever",
    "SystemHandle",
    "TransportCapableBuilder",
    "TransportHandle",
    "TransportHandleData",
    "TransportHandleKind",
    "VmcoreIntrospector",
]
