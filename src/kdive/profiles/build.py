"""The build-profile schema and its parse boundary (ADR-0029).

A build profile is a versioned, declarative document naming either a server-build lane
(``source="server"``: kernel source tree, ``.config``, optional patch) or an external-build
lane (``source="external"``: no source-tree fields — the artifact is ingested, not built).
It is the opaque ``build_profile`` jsonb a Run already carries (ADR-0026 §6 deferred its
validation here); the build plane parses it at the ``runs.build`` tool boundary and in the
build handler.

Both variants are ``frozen`` (immutable request inputs, ADR-0003/0011) and reject unknown
fields. :meth:`BuildProfile.parse` is the sanctioned entry point: it dispatches on
``source`` (defaulting to ``"server"`` so existing server-build documents without the field
continue to parse), maps Pydantic's structural ``ValidationError`` onto the wire taxonomy's
``configuration_error``, and scrubs submitted values out of the error details so a profile
that references secret or guest-derived material cannot leak it. Constructing a model
directly bypasses this mapping and is a caller error.

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
)

from kdive.components.references import ComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles._schema import schema_version_validator

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after whitespace stripping; blank values fail validation."""


class _BuildProfileBase(BaseModel):
    """Shared config + version guard for both build-lane profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]

    _reject_coerced_version = schema_version_validator


class ProfileRequirementsRef(BaseModel):
    """A provider-scoped profile requirement selector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: NonEmptyStr
    name: NonEmptyStr


class ServerBuildProfile(_BuildProfileBase):
    """Server-build lane: names a source tree, a config, and an optional patch."""

    source: Literal["server"] = "server"
    kernel_source_ref: NonEmptyStr
    config: ComponentRef
    profile_requirements: ProfileRequirementsRef | None = None
    patch_ref: NonEmptyStr | None = None


class ExternalBuildProfile(_BuildProfileBase):
    """External-build lane: the discriminator alone — no source-tree fields."""

    source: Literal["external"]
    profile_requirements: ProfileRequirementsRef | None = None


type ParsedBuildProfile = ServerBuildProfile | ExternalBuildProfile


class BuildProfile:
    """Parse boundary that dispatches a build-profile document on ``source``."""

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> ParsedBuildProfile:
        """Validate a build-profile document, mapping any failure to ``configuration_error``.

        Dispatches on ``source`` (default ``"server"``, so existing server documents
        without the field still parse). The error details carry field locations but never
        the submitted values (redaction guarantee, ADR-0029).

        Args:
            data: The deserialized profile document (a mapping; YAML/JSON parsing is the
                caller's responsibility). Non-mapping inputs are rejected as
                ``CONFIGURATION_ERROR``.

        Returns:
            The validated, frozen profile — a :class:`ServerBuildProfile` or
            :class:`ExternalBuildProfile` depending on ``source``.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for any structural failure —
                missing/unknown field, wrong type, empty required string, unreadable
                schema version, or unknown ``source``. The error details carry field
                locations, types, and messages, but never the submitted values.
        """
        source = data.get("source", "server") if isinstance(data, Mapping) else None
        model: type[ParsedBuildProfile]
        if source == "server":
            model = ServerBuildProfile
        elif source == "external":
            model = ExternalBuildProfile
        else:
            raise CategorizedError(
                "invalid build profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"errors": [{"loc": ["source"], "msg": "unknown build source"}]},
            )
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise CategorizedError(
                "invalid build profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "errors": exc.errors(
                        include_url=False, include_input=False, include_context=False
                    )
                },
            ) from exc
