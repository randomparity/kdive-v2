"""Handler-facing provider ports used by the MCP and worker layers.

The active provider implementation currently lives under ``local_libvirt``, but MCP tool
modules depend on this facade rather than provider implementation modules. Composition code
owns the concrete provider imports and factories.
"""

from __future__ import annotations

from kdive.providers.local_libvirt.build import Builder, BuildOutput, ValidatedUpload
from kdive.providers.local_libvirt.connect import Connector, TransportHandleData
from kdive.providers.local_libvirt.control import Controller, PowerAction
from kdive.providers.local_libvirt.debug_gdbmi import (
    AttachSeam,
    GdbMiAttachment,
    GdbMiEngine,
    GdbMiSessionRegistry,
)
from kdive.providers.local_libvirt.install import Booter, Installer
from kdive.providers.local_libvirt.introspect_drgn import (
    IntrospectOutput,
    LiveIntrospector,
    VmcoreIntrospector,
)
from kdive.providers.local_libvirt.provisioning import Provisioner
from kdive.providers.local_libvirt.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashPostmortem,
    CrashResult,
    Retriever,
)

__all__ = [
    "AttachSeam",
    "Booter",
    "Builder",
    "BuildOutput",
    "CaptureOutput",
    "Connector",
    "Controller",
    "CrashOutput",
    "CrashPostmortem",
    "CrashResult",
    "GdbMiAttachment",
    "GdbMiEngine",
    "GdbMiSessionRegistry",
    "Installer",
    "IntrospectOutput",
    "LiveIntrospector",
    "PowerAction",
    "Provisioner",
    "Retriever",
    "TransportHandleData",
    "ValidatedUpload",
    "VmcoreIntrospector",
]
