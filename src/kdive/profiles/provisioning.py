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
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after whitespace stripping; blank values fail validation."""


class BootMethod(StrEnum):
    """The provider-agnostic boot methods; M0 ships one (ADR-0024 decision 2a)."""

    DIRECT_KERNEL = "direct-kernel"


class _ProfileBase(BaseModel):
    """Shared config: reject unknown fields and freeze after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _PathRootfs(_ProfileBase):
    kind: Literal["path"]
    path: NonEmptyStr


class _UploadRootfs(_ProfileBase):
    # A System-owned uploaded qcow2; opened by systems.define + artifacts.create_upload and
    # committed at provisioning->ready (ADR-0048 §5, #111). path/url/catalog are alternatives.
    kind: Literal["upload"]


class _UrlRootfs(_ProfileBase):
    kind: Literal["url"]
    url: NonEmptyStr
    sha256: NonEmptyStr  # 'sha256:<64-hex>'; format checked at resolve


class _CatalogRootfs(_ProfileBase):
    kind: Literal["catalog"]
    name: NonEmptyStr


type RootfsSource = Annotated[
    _PathRootfs | _UploadRootfs | _UrlRootfs | _CatalogRootfs,
    Field(discriminator="kind"),
]
"""A discriminated rootfs source (ADR-0048 §3); ``kind`` selects the variant."""


class LibvirtProfile(_ProfileBase):
    """The ``local-libvirt`` provider section (ADR-0024 decisions 1, 2b, 2c).

    ``domain_xml_params`` is an optionally-empty map whose values are non-empty;
    ``rootfs`` is the discriminated rootfs source (ADR-0048 §3) keyed by ``kind`` —
    ``path`` (a declared file), ``upload`` (a System-owned uploaded object), ``url``
    (a content-addressed fetch), or ``catalog`` (a curated image by name); the resolver
    maps it to the libvirt-readable disk path at provisioning. ``crashkernel`` is an
    opaque non-empty token (the kdump prerequisite — the booted kernel is the arbiter of
    its grammar). ``destructive_ops`` is the optionally-empty list of destructive op kinds
    this profile opts in (e.g. ``["force_crash"]``); the control plane's gate resolves the
    opt-in factor from it (deny-by-default — an absent or empty list refuses every
    destructive op, ADR-0028 §2). ``ssh_credential_ref`` is the optional opaque
    **reference** (never the value) into the file-ref secret backend that the live ssh
    transport resolves a guest credential through (ADR-0039 §2); a profile that does not
    opt into live ssh introspection leaves it ``None``.
    """

    domain_xml_params: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    rootfs: RootfsSource
    crashkernel: NonEmptyStr
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    ssh_credential_ref: NonEmptyStr | None = None


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

    @field_validator("schema_version", mode="before")
    @classmethod
    def _reject_coerced_version(cls, value: object) -> object:
        """Reject a non-``int`` version before ``Literal`` coercion accepts it.

        ``Literal[1]`` otherwise matches ``True`` and ``1.0`` (``True == 1.0 == 1``),
        which would tolerate a malformed version the way lax integers tolerate
        ``vcpu: "4"`` (ADR-0024 decision 2d). The message names the constraint, not
        the value, to preserve the redaction guarantee (decision 3).
        """
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("schema_version must be an integer")
        return value

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> ProvisioningProfile:
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
    canonical = json.dumps(profile.model_dump(by_alias=True), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
