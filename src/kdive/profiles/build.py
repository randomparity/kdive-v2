"""The build-profile schema and its parse boundary (ADR-0029).

A build profile is a versioned, declarative document naming the kernel source tree,
the kernel ``.config`` to build with, and an optional patch applied on top of the base
tree. It is the opaque ``build_profile`` jsonb a Run already carries (ADR-0026 §6
deferred its validation here); the build plane parses it at the ``runs.build`` tool
boundary and in the build handler.

The model is ``frozen`` (immutable request inputs, ADR-0003/0011) and rejects unknown
fields. :meth:`BuildProfile.parse` is the sanctioned entry point: it maps Pydantic's
structural ``ValidationError`` onto the wire taxonomy's ``configuration_error`` and
scrubs submitted values out of the error details so a profile that references secret or
guest-derived material cannot leak it. Constructing a model directly bypasses this
mapping and is a caller error.

The kdump/debuginfo *config-correctness* requirements (``CONFIG_CRASH_DUMP``/
``crashkernel`` and ``CONFIG_DEBUG_INFO(_DWARF)``/BTF) are **not** checked here: the
profile only names a config by reference, so its contents are not in this document. The
builder resolves the config and preflights it against the kernel tree (ADR-0029 §3).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    field_validator,
)

from kdive.domain.errors import CategorizedError, ErrorCategory

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after whitespace stripping; blank values fail validation."""


class BuildProfile(BaseModel):
    """A versioned build profile: kernel source ref, config ref, optional patch ref."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    kernel_source_ref: NonEmptyStr
    config_ref: NonEmptyStr
    patch_ref: NonEmptyStr | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _reject_coerced_version(cls, value: object) -> object:
        """Reject a non-``int`` version before ``Literal`` coercion accepts it.

        ``Literal[1]`` otherwise matches ``True`` and ``1.0`` (``True == 1.0 == 1``),
        which would tolerate a malformed version. The message names the constraint, not
        the value, to preserve the redaction guarantee.
        """
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("schema_version must be an integer")
        return value

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> BuildProfile:
        """Validate a build-profile document, mapping any failure to ``configuration_error``.

        Args:
            data: The deserialized profile document (a mapping; YAML/JSON parsing is the
                caller's responsibility).

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
                "invalid build profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details=details,
            ) from exc
