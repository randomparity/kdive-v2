"""The provisioning-profile schema and its parse boundary (ADR-0011, ADR-0024).

A provisioning profile is a versioned, declarative document with a
provider-agnostic core (target arch, vCPU, memory, disk, boot method, kernel-source
reference) and a provider-specific section keyed by provider name. The production
default is ``local-libvirt``; ``fault-inject`` is implemented as an opt-in provider
behind ``ProviderResolver`` for test and failure-path coverage.

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
    model_validator,
)

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DestructiveJobKind, ResourceKind
from kdive.domain.profile_documents import SerializedProvisioningProfile
from kdive.domain.sizing import AllocationSizing
from kdive.profiles._schema import schema_version_validator
from kdive.profiles.types import ProvisioningProfileInput
from kdive.provider_components.catalog import load_fixture_catalog
from kdive.provider_components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
)

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after whitespace stripping; blank values fail validation."""

SUPPORTED_DOMAIN_XML_PARAMS = frozenset({"machine"})


class BootMethod(StrEnum):
    """The provider-agnostic boot methods (ADR-0024 decision 2a, ADR-0080).

    ``disk-image`` boots an operator-staged base-OS image and iterates kernels by
    in-guest install + reboot (the remote-libvirt model, ADR-0078); ``direct-kernel``
    stays the local-libvirt/fault-inject method.
    """

    DIRECT_KERNEL = "direct-kernel"
    DISK_IMAGE = "disk-image"


class _ProfileBase(BaseModel):
    """Shared config: reject unknown fields and freeze after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _UploadRootfs(_ProfileBase):
    # A System-owned uploaded qcow2; opened by systems.define + artifacts.create_system_upload and
    # committed at provisioning->ready (ADR-0048 §5). path/url/catalog are alternatives.
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


class FaultInjectProfile(_ProfileBase):
    """The ``fault-inject`` provider section (ADR-0072).

    The mock provider owns no rootfs/domain XML materialization; its section carries only
    the knobs shared by generic control/retrieve gates.
    """

    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    capture_method: CaptureMethod = CaptureMethod.CONSOLE


class RemoteLibvirtProfile(_ProfileBase):
    """The ``remote-libvirt`` provider section (ADR-0080).

    ``base_image_volume`` names the **operator-staged** qcow2 volume on the remote
    host's storage pool carrying the base OS (with qemu-guest-agent enabled, drgn,
    and matching vmlinux/debuginfo — image-content obligations the operator owns,
    ADR-0078/0079); provisioning verifies the volume exists, not its contents.
    ``crashkernel`` mirrors the local section (the kdump prerequisite token; the
    booted kernel is the arbiter of its grammar). ``destructive_ops`` is the
    deny-by-default destructive-op opt-in factor (ADR-0028 §2). There is no rootfs,
    SSH credential, or gdbstub flag: the base image is the rootfs, in-guest access
    rides the guest-agent seam, and the gdbstub is unconditionally enabled with a
    per-System port the provisioning plane allocates (ADR-0079/0080).
    """

    base_image_volume: NonEmptyStr
    crashkernel: NonEmptyStr | None = None
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)


class ProviderSection(_ProfileBase):
    """The provider-specific section, keyed by provider name (ADR-0024 decision 1).

    Exactly one concrete provider section is required. The public properties return the
    concrete section for callers that have already selected a provider-specific path.
    """

    local_libvirt_section: LibvirtProfile | None = Field(
        default=None,
        validation_alias=ResourceKind.LOCAL_LIBVIRT.value,
        serialization_alias=ResourceKind.LOCAL_LIBVIRT.value,
    )
    fault_inject_section: FaultInjectProfile | None = Field(
        default=None,
        validation_alias=ResourceKind.FAULT_INJECT.value,
        serialization_alias=ResourceKind.FAULT_INJECT.value,
    )
    remote_libvirt_section: RemoteLibvirtProfile | None = Field(
        default=None,
        validation_alias=ResourceKind.REMOTE_LIBVIRT.value,
        serialization_alias=ResourceKind.REMOTE_LIBVIRT.value,
    )

    @model_validator(mode="after")
    def _require_exactly_one_provider(self) -> ProviderSection:
        present = [
            self.local_libvirt_section is not None,
            self.fault_inject_section is not None,
            self.remote_libvirt_section is not None,
        ]
        if sum(present) != 1:
            raise ValueError("profile provider must contain exactly one provider section")
        return self

    @property
    def local_libvirt(self) -> LibvirtProfile:
        """Return the local-libvirt section for local-libvirt-specific callers."""
        if self.local_libvirt_section is None:
            raise AttributeError("profile has no local-libvirt provider section")
        return self.local_libvirt_section

    @property
    def fault_inject(self) -> FaultInjectProfile:
        """Return the fault-inject section for fault-inject-specific callers."""
        if self.fault_inject_section is None:
            raise AttributeError("profile has no fault-inject provider section")
        return self.fault_inject_section

    @property
    def remote_libvirt(self) -> RemoteLibvirtProfile:
        """Return the remote-libvirt section for remote-libvirt-specific callers."""
        if self.remote_libvirt_section is None:
            raise AttributeError("profile has no remote-libvirt provider section")
        return self.remote_libvirt_section


class ProvisioningProfile(_ProfileBase):
    """A versioned provisioning profile: agnostic core plus a provider section.

    The sizing fields (``vcpu`` / ``memory_mb`` / ``disk_gb``) are **optional** (ADR-0024
    delta, ADR-0067): a shape-sized allocation omits them and ``systems.provision``
    constructs them from the resolved sizing snapshot via :func:`reconcile_profile_sizing`
    before the profile is stored. A *present* value is still strictly ``> 0``. A stored
    profile always carries concrete sizing — reconciliation fills the snapshot and the
    no-snapshot lane rejects a NULL-sized profile — so the libvirt renderer never reads a
    ``None`` (it dereferences ``vcpu``/``memory_mb`` unconditionally).
    """

    schema_version: Literal[1]
    arch: NonEmptyStr
    vcpu: int | None = Field(default=None, gt=0, strict=True)
    memory_mb: int | None = Field(default=None, gt=0, strict=True)
    disk_gb: int | None = Field(default=None, gt=0, strict=True)
    boot_method: BootMethod
    kernel_source_ref: NonEmptyStr
    provider: ProviderSection

    _reject_coerced_version = schema_version_validator

    @model_validator(mode="after")
    def _pair_boot_method_with_provider(self) -> ProvisioningProfile:
        """``disk-image`` and the remote-libvirt section require each other (ADR-0080)."""
        remote = self.provider.remote_libvirt_section is not None
        disk_image = self.boot_method is BootMethod.DISK_IMAGE
        if remote != disk_image:
            raise ValueError(
                "boot_method 'disk-image' and the remote-libvirt provider section "
                "require each other (ADR-0080)"
            )
        return self

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


def reconcile_profile_sizing(
    data: ProvisioningProfileInput, sizing: AllocationSizing
) -> dict[str, object]:
    """Build a profile dict whose sizing equals the allocation snapshot (ADR-0024 delta).

    For a shape-sized allocation the resolved tuple is the authority: a profile may omit
    ``vcpu`` / ``memory_mb`` / ``disk_gb`` (they are filled from ``sizing``), or restate
    them — but only with the *same* values; a conflicting restatement is rejected so
    admitted size and booted size can never diverge. Builds a new dict (the immutable
    request-inputs invariant, ADR-0003/0024) rather than mutating the input. Reads only the
    passed snapshot, never the catalog, so a later ``shapes.set`` cannot re-size a stamped
    profile.

    Args:
        data: The submitted profile document (sizing optional or matching).
        sizing: The Allocation's persisted sizing snapshot.

    Returns:
        A new profile dict with concrete ``vcpu`` / ``memory_mb`` / ``disk_gb``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if a submitted size conflicts with the
            snapshot.
    """
    reconciled = dict(data)
    for field, resolved in (
        ("vcpu", sizing.vcpu),
        ("memory_mb", sizing.memory_mb),
        ("disk_gb", sizing.disk_gb),
    ):
        submitted = reconciled.get(field)
        if submitted is not None and submitted != resolved:
            raise CategorizedError(
                f"provisioning profile {field}={submitted!r} conflicts with the "
                f"allocation's resolved size {resolved}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"field": field, "resolved": str(resolved)},
            )
        reconciled[field] = resolved
    return reconciled


def require_concrete_sizing(profile: ProvisioningProfile) -> None:
    """Reject a profile with any NULL sizing field (the no-snapshot lane, ADR-0067).

    A full-custom or legacy allocation carries no resolved sizing snapshot, so its profile
    must supply its own ``vcpu`` / ``memory_mb`` / ``disk_gb``. A stored profile must never
    carry a ``None`` size — the libvirt renderer dereferences them unconditionally.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if any sizing field is ``None``.
    """
    missing = [
        field for field in ("vcpu", "memory_mb", "disk_gb") if getattr(profile, field) is None
    ]
    if missing:
        raise CategorizedError(
            f"provisioning profile is missing required sizing: {', '.join(missing)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing": missing},
        )


def dump_profile(profile: ProvisioningProfile) -> SerializedProvisioningProfile:
    """Serialize a parsed provisioning profile for JSON persistence."""
    return cast(
        SerializedProvisioningProfile,
        profile.model_dump(mode="json", by_alias=True, exclude_none=True),
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
    rootfs = rootfs_source(profile)
    return rootfs is not None and rootfs.kind == "upload"


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


def rootfs_source(profile: ProvisioningProfile) -> RootfsSource | None:
    """Return the profile's rootfs source, or ``None`` for providers that do not use one."""
    section = profile.provider.local_libvirt_section
    return section.rootfs if section is not None else None


def ssh_credential_ref(profile: ProvisioningProfile) -> str | None:
    """Return the SSH credential reference for providers with credential-backed SSH."""
    section = profile.provider.local_libvirt_section
    return section.ssh_credential_ref if section is not None else None


def drgn_live_requires_credential(profile: ProvisioningProfile) -> bool:
    """Return whether this profile's drgn-live transport needs a core-resolved credential.

    True for a local-libvirt section (drgn-live is realized over SSH, ADR-0039); False
    otherwise (remote reaches the guest agent over qemu+tls; fault-inject is synthetic). Keeps
    the credential decision provider-agnostic in core, which only asks this predicate
    (ADR-0085 Decision 2).
    """
    return profile.provider.local_libvirt_section is not None


def validate_profile(profile: ProvisioningProfile) -> None:
    """Reject unsupported provider params and unresolvable rootfs references."""
    section = profile.provider.local_libvirt_section
    if section is None:
        return
    params = section.domain_xml_params
    unknown = sorted(set(params) - SUPPORTED_DOMAIN_XML_PARAMS)
    if unknown:
        raise CategorizedError(
            f"unsupported domain_xml_params: {', '.join(unknown)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"unsupported": unknown, "supported": sorted(SUPPORTED_DOMAIN_XML_PARAMS)},
        )
    validate_rootfs_reference(section.rootfs)


def destructive_opt_in(profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
    """Return whether the profile opts into a destructive operation."""
    if profile.provider.local_libvirt_section is not None:
        return op.value in profile.provider.local_libvirt_section.destructive_ops
    if profile.provider.remote_libvirt_section is not None:
        return op.value in profile.provider.remote_libvirt_section.destructive_ops
    return op.value in profile.provider.fault_inject.destructive_ops


def capture_method(profile: ProvisioningProfile | Mapping[str, object]) -> CaptureMethod:
    """Resolve the crash-capture method a provisioning profile enables."""
    parsed = _parsed_profile(profile)
    remote = parsed.provider.remote_libvirt_section
    if remote is not None:
        # The gdbstub is unconditionally enabled for every remote System (ADR-0079/0080),
        # so kdump opts in via crashkernel and the gdbstub is the floor, never console.
        if remote.crashkernel is not None:
            return CaptureMethod.KDUMP
        return CaptureMethod.GDBSTUB
    section = parsed.provider.local_libvirt_section
    if section is None:
        return parsed.provider.fault_inject.capture_method
    if section.crashkernel is not None:
        return CaptureMethod.KDUMP
    if section.debug.gdbstub:
        return CaptureMethod.GDBSTUB
    if section.debug.preserve_on_crash:
        return CaptureMethod.HOST_DUMP
    return CaptureMethod.CONSOLE
