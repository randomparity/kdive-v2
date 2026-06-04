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


class LibvirtProfile(_ProfileBase):
    """The ``local-libvirt`` provider section (ADR-0024 decisions 1, 2b, 2c).

    ``domain_xml_params`` is an optionally-empty map whose values are non-empty;
    ``crashkernel`` is an opaque non-empty token (the kdump prerequisite — the booted
    kernel is the arbiter of its grammar).
    """

    domain_xml_params: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    rootfs_image_ref: NonEmptyStr
    crashkernel: NonEmptyStr


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
