"""Compatibility exports for direct `systems.*` handler tests."""

from __future__ import annotations

from kdive.mcp.tools.lifecycle.systems.admin import SystemAdminHandlers, teardown_system
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers, get_system

__all__ = [
    "get_system",
    "SystemAdminHandlers",
    "SystemProvisionHandlers",
    "teardown_system",
]
