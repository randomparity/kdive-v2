"""The provisioning-profile schema and its parse boundary (ADR-0011, ADR-0024).

A provisioning profile is a versioned, declarative document with a
provider-agnostic core (target arch, vCPU, memory, disk, boot method, kernel-source
reference) and a provider-specific section keyed by provider name. M0 ships the
``local-libvirt`` variant only.

The models are ``frozen`` (the immutable-request-inputs invariant, ADR-0003/0011)
and reject unknown fields. :meth:`ProvisioningProfile.parse` is the sanctioned entry
point: it maps Pydantic's structural ``ValidationError`` onto the wire taxonomy's
``configuration_error`` and scrubs submitted values out of the error details so a
profile that references secret or guest-derived material cannot leak it (ADR-0024
decision 3). Constructing a model directly bypasses this mapping and is a caller
error.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from kdive.components.catalog import load_fixture_catalog
from kdive.components.references import ArtifactComponentRef, CatalogComponentRef, LocalComponentRef
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.profiles._schema import schema_version_validator
from kdive.profiles.types import ProvisioningProfileInput, SerializedProvisioningProfile

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after whitespace stripping; blank values fail validation."""

SUPPORTED_DOMAIN_XML_PARAMS = frozenset({"machine"})


class BootMethod(StrEnum):
    """The provider-agnostic boot methods; M0 ships one (ADR-0024 decision 2a)."""

    DIRECT_KERNEL = "direct-kernel"


class _ProfileBase(BaseModel):
    """Shared config: reject unknown fields and freeze after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _UploadRootfs(_ProfileBase):
    # A System-owned uploaded qcow2; opened by systems.define + artifacts.create_system_upload and
    # committed at provisioning->ready (ADR-0048 §5, #111). path/url/catalog are alternatives.
    kind: Literal["upload"]


type RootfsSource = Annotated[
    LocalComponentRef | ArtifactComponentRef | CatalogComponentRef | _UploadRootfs,
    Field(discriminator="kind"),
]
"""A discriminated rootfs source (ADR-0065); ``upload`` remains System-owned."""


class LibvirtDebugOptions(_ProfileBase):
    """Per-System debug provisioning flags (ADR-0049 Decision 3).

    Bound at provision/boot; declare which capture methods the System is
    provisioned for. ``preserve_on_crash`` adds a pvpanic device +
    ``<on_crash>preserve</on_crash>``; ``gdbstub`` adds the QEMU ``-gdb`` argument.
    """

    preserve_on_crash: bool = False
    gdbstub: bool = False


class LibvirtProfile(_ProfileBase):
    """The ``local-libvirt`` provider section (ADR-0024 decisions 1, 2b, 2c).

    ``domain_xml_params`` is an optionally-empty map whose values are non-empty;
    ``rootfs`` is the discriminated rootfs source (ADR-0048 §3) keyed by ``kind`` —
    ``path`` (a declared file), ``upload`` (a System-owned uploaded object), ``url``
    (a content-addressed fetch), or ``catalog`` (a curated image by name); the resolver
    maps it to the libvirt-readable disk path at provisioning. ``crashkernel`` is an
    optional opaque non-empty token (the kdump prerequisite — the booted kernel is the
    arbiter of its grammar); ``None`` when the System is not provisioned for kdump.
    ``destructive_ops`` is the optionally-empty list of destructive op kinds this profile
    opts in (e.g. ``["force_crash"]``); the control plane's gate resolves the opt-in
    factor from it (deny-by-default — an absent or empty list refuses every destructive
    op, ADR-0028 §2). ``ssh_credential_ref`` is the optional opaque **reference** (never
    the value) into the file-ref secret backend that the live ssh transport resolves a
    guest credential through (ADR-0039 §2); a profile that does not opt into live ssh
    introspection leaves it ``None``. ``debug`` declares which crash-capture methods the
    System is provisioned for (ADR-0049 Decision 3); defaults to all flags disabled.
    """

    domain_xml_params: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    rootfs: RootfsSource
    crashkernel: NonEmptyStr | None = None
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    ssh_credential_ref: NonEmptyStr | None = None
    debug: LibvirtDebugOptions = Field(default_factory=LibvirtDebugOptions)


class ProviderSection(_ProfileBase):
    """The provider-specific section, keyed by provider name (ADR-0024 decision 1).

    M0 requires exactly the ``local-libvirt`` section; an unknown provider key is
    rejected by ``extra="forbid"``.
    """

    local_libvirt: LibvirtProfile = Field(alias=ResourceKind.LOCAL_LIBVIRT.value)


class ProvisioningProfile(_ProfileBase):
    """A versioned provisioning profile: agnostic core plus a provider section."""

    schema_version: Literal[1]
    arch: NonEmptyStr
    vcpu: int = Field(gt=0, strict=True)
    memory_mb: int = Field(gt=0, strict=True)
    disk_gb: int = Field(gt=0, strict=True)
    boot_method: BootMethod
    kernel_source_ref: NonEmptyStr
    provider: ProviderSection

    _reject_coerced_version = schema_version_validator

    @classmethod
    def parse(cls, data: ProvisioningProfileInput) -> ProvisioningProfile:
        """Validate a profile document, mapping any failure to ``configuration_error``.

        Args:
            data: The deserialized profile document (a mapping; YAML/JSON parsing is
                the caller's responsibility).

        Returns:
            The validated, frozen profile.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for any structural failure —
                missing/unknown field, wrong type, empty required string, unreadable
                schema version. The error details carry field locations, types, and
                messages, but never the submitted values.
        """
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            details: dict[str, object] = {
                "errors": exc.errors(include_url=False, include_input=False, include_context=False),
            }
            raise CategorizedError(
                "invalid provisioning profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details=details,
            ) from exc


def dump_profile(profile: ProvisioningProfile) -> SerializedProvisioningProfile:
    """Serialize a parsed provisioning profile for JSON persistence."""
    return cast(
        SerializedProvisioningProfile,
        profile.model_dump(mode="json", by_alias=True),
    )


def profile_digest(profile: ProvisioningProfile) -> str:
    """Return the SHA-256 hex of a canonical encoding of a parsed profile (ADR-0038 §3).

    Computed over the parsed, alias-keyed model dump with sorted keys, so digest equality
    is *semantic* equality: two byte-different but equivalent submissions (key order,
    whitespace) produce the same digest, and any meaningful change produces a distinct one.
    This is the dedup factor in the reprovision ``dedup_key`` (mirrors
    :func:`kdive.security.audit.args_digest`).

    Args:
        profile: A validated profile (parse before hashing — never hash raw input, whose
            ordering and coercions are not normalized).
    """
    canonical = json.dumps(dump_profile(profile), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parsed_profile(profile: ProvisioningProfile | Mapping[str, object]) -> ProvisioningProfile:
    if isinstance(profile, ProvisioningProfile):
        return profile
    return ProvisioningProfile.parse(profile)


def rootfs_upload_window_allowed(profile: ProvisioningProfile) -> bool:
    """Return whether the profile's rootfs expects a System upload window."""
    return profile.provider.local_libvirt.rootfs.kind == "upload"


def reject_rootfs_upload_without_window(profile: ProvisioningProfile) -> None:
    """Reject a profile whose rootfs needs a System upload window in a no-window lane.

    ``systems.define`` opens the window for a System-owned rootfs upload. Direct provision and
    reprovision do not, so accepting an upload-kind rootfs there would enqueue work that cannot
    commit its disk artifact.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the profile needs an upload window.
    """
    if rootfs_upload_window_allowed(profile):
        raise CategorizedError(
            "upload-kind rootfs requires systems.define upload window",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def validate_rootfs_reference(rootfs: RootfsSource) -> None:
    """Validate a rootfs reference's static resolvability."""
    if isinstance(rootfs, CatalogComponentRef) and (
        load_fixture_catalog().rootfs_entry(rootfs.provider, rootfs.name) is None
    ):
        raise CategorizedError(
            f"unknown rootfs catalog name: {rootfs.name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": rootfs.provider, "name": rootfs.name},
        )


def validate_profile(profile: ProvisioningProfile) -> None:
    """Reject unsupported provider params and unresolvable rootfs references."""
    params = profile.provider.local_libvirt.domain_xml_params
    unknown = sorted(set(params) - SUPPORTED_DOMAIN_XML_PARAMS)
    if unknown:
        raise CategorizedError(
            f"unsupported domain_xml_params: {', '.join(unknown)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"unsupported": unknown, "supported": sorted(SUPPORTED_DOMAIN_XML_PARAMS)},
        )
    validate_rootfs_reference(profile.provider.local_libvirt.rootfs)


def destructive_opt_in(profile: ProvisioningProfile, op: str) -> bool:
    """Return whether the profile opts into a destructive operation."""
    return op in profile.provider.local_libvirt.destructive_ops


def capture_method(profile: ProvisioningProfile | Mapping[str, object]) -> CaptureMethod:
    """Resolve the crash-capture method a provisioning profile enables.

    Stored legacy rows may contain malformed profile mappings. Those are treated as the
    baseline console method, matching the previous tolerant read path.
    """
    try:
        parsed = _parsed_profile(profile)
    except CategorizedError:
        return CaptureMethod.CONSOLE
    section = parsed.provider.local_libvirt
    if section.crashkernel is not None:
        return CaptureMethod.KDUMP
    if section.debug.gdbstub:
        return CaptureMethod.GDBSTUB
    if section.debug.preserve_on_crash:
        return CaptureMethod.HOST_DUMP
    return CaptureMethod.CONSOLE
