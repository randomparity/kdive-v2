"""Declarative request profiles (provisioning, and later build)."""

from __future__ import annotations

from kdive.profiles.build import BuildProfile, ExternalBuildProfile, ServerBuildProfile
from kdive.profiles.provisioning import (
    BootMethod,
    LibvirtProfile,
    ProviderSection,
    ProvisioningProfile,
)

__all__ = [
    "BootMethod",
    "BuildProfile",
    "ExternalBuildProfile",
    "LibvirtProfile",
    "ProviderSection",
    "ProvisioningProfile",
    "ServerBuildProfile",
]
