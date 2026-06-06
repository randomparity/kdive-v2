"""Prebuilt rootfs image catalog: models + bundled-data loader (ADR-0048 §3, ported from v1).

A curated, per-distro list of known-good images selectable by a stable name. The data
lives in ``catalog_data.json`` beside this module and is validated on load. Fetch/caching
to a libvirt-readable path is the next spec's install/boot work and is not here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory

DEFAULT_CATALOG_PATH = Path(__file__).with_name("catalog_data.json")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class CatalogEntry(BaseModel):
    """One curated image; ``name`` is the value a ``catalog``-kind rootfs ref carries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    distro: str
    release: str
    architecture: str
    url: str
    checksum: str
    size_bytes: int = Field(gt=0)
    image_format: Literal["qcow2"]
    readiness_marker: str
    ssh_user: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    access_method: Literal["ssh", "serial", "ssh_and_serial", "none"] = "ssh"
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _SAFE_NAME.match(value):
            raise ValueError("catalog entry name must be filesystem safe")
        return value

    @field_validator("checksum")
    @classmethod
    def _validate_checksum(cls, value: str) -> str:
        if not _SHA256.match(value):
            raise ValueError("checksum must be 'sha256:' followed by 64 lowercase hex chars")
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("catalog url must be https://")
        return value

    @field_validator("readiness_marker", "ssh_user", "distro", "release", "architecture")
    @classmethod
    def _validate_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must be non-empty")
        return value


class RootfsCatalog(BaseModel):
    """A validated set of curated entries with unique names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: list[CatalogEntry]

    @model_validator(mode="after")
    def _validate_unique_names(self) -> RootfsCatalog:
        names = [entry.name for entry in self.entries]
        if len(names) != len(set(names)):
            raise ValueError("catalog entry names must be unique")
        return self

    def lookup(self, name: str) -> CatalogEntry | None:
        """Return the entry with ``name``, or ``None``."""
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None


def load_catalog(path: Path = DEFAULT_CATALOG_PATH) -> RootfsCatalog:
    """Read and validate the bundled catalog JSON.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` if the data file is unreadable,
            not JSON, or fails validation (a packaging defect).
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return RootfsCatalog.model_validate(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CategorizedError(
            f"rootfs catalog data is unusable: {path}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
