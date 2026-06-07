"""Declarative request profiles (provisioning, and later build)."""

from __future__ import annotations

from kdive.profiles.build import BuildProfile, ExternalBuildProfile, ServerBuildProfile
from kdive.profiles.provisioning import (
    BootMethod,
    LibvirtProfile,
    ProviderSection,
    ProvisioningProfile,
    capture_method,
    destructive_opt_in,
    profile_digest,
    reject_rootfs_upload_without_window,
    rootfs_upload_window_allowed,
)

__all__ = [
    "BootMethod",
    "BuildProfile",
    "ExternalBuildProfile",
    "LibvirtProfile",
    "ProviderSection",
    "ProvisioningProfile",
    "ServerBuildProfile",
    "capture_method",
    "destructive_opt_in",
    "profile_digest",
    "reject_rootfs_upload_without_window",
    "rootfs_upload_window_allowed",
]
