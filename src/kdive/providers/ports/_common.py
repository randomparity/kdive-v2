"""Shared helpers for provider port contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from kdive.domain.errors import CategorizedError, ErrorCategory


class ProviderModel(BaseModel):
    """Frozen wire shape for provider-returned records."""

    model_config = ConfigDict(extra="forbid")


def config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)
