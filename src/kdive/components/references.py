"""Provider component reference models (ADR-0065)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from kdive.domain.errors import CategorizedError, ErrorCategory

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}\Z")


class _ComponentRefBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalComponentRef(_ComponentRefBase):
    kind: Literal["local"]
    path: NonEmptyStr
    sha256: str | None = None

    @field_validator("path")
    @classmethod
    def _validate_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("local component path must be absolute")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.match(value):
            raise ValueError("sha256 must be 'sha256:<64 lowercase hex chars>'")
        return value


class ArtifactComponentRef(_ComponentRefBase):
    kind: Literal["artifact"]
    artifact_id: UUID
    sha256: str | None = None

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.match(value):
            raise ValueError("sha256 must be 'sha256:<64 lowercase hex chars>'")
        return value


class CatalogComponentRef(_ComponentRefBase):
    kind: Literal["catalog"]
    provider: NonEmptyStr
    name: NonEmptyStr


type ComponentRef = Annotated[
    LocalComponentRef | ArtifactComponentRef | CatalogComponentRef,
    Field(discriminator="kind"),
]


class _ComponentRefAdapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: ComponentRef


def parse_component_ref(data: Mapping[str, object]) -> ComponentRef:
    """Parse one component reference and map structural errors to KDIVE error taxonomy."""
    try:
        return _ComponentRefAdapter.model_validate({"ref": data}).ref
    except ValidationError as exc:
        raise CategorizedError(
            "invalid component reference",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"errors": exc.errors(include_url=False, include_input=False)},
        ) from exc
