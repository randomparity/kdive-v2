"""Provider/profile requirement validators (ADR-0065)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kdive.domain.errors import CategorizedError, ErrorCategory


class ConfigRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required: dict[str, str] = Field(default_factory=dict)


class CmdlineRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_tokens: list[str] = Field(default_factory=list)
    protected_prefixes: list[str] = Field(default_factory=list)


def validate_config_requirements(config_text: str, requirements: ConfigRequirements) -> None:
    values = _parse_config(config_text)
    missing = {
        key: value for key, value in requirements.required.items() if values.get(key) != value
    }
    if missing:
        raise CategorizedError(
            "kernel config does not satisfy profile requirements",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing_or_different": sorted(missing)},
        )


def validate_cmdline_requirements(
    cmdline: str,
    requirements: CmdlineRequirements,
    *,
    platform_cmdline: str,
) -> None:
    tokens = cmdline.split()
    missing = [token for token in requirements.required_tokens if token not in tokens]
    if missing:
        raise CategorizedError(
            "kernel command line does not include required tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing": missing},
        )

    platform = {
        prefix: _first_token_with_prefix(platform_cmdline, prefix)
        for prefix in requirements.protected_prefixes
    }
    supplied = {
        prefix: _first_token_with_prefix(cmdline, prefix)
        for prefix in requirements.protected_prefixes
    }
    overrides = [
        prefix
        for prefix, platform_token in platform.items()
        if platform_token is not None
        and supplied[prefix] is not None
        and supplied[prefix] != platform_token
    ]
    if overrides:
        raise CategorizedError(
            "kernel command line overrides protected platform tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"protected_prefixes": overrides},
        )


def _parse_config(config_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in config_text.splitlines():
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            key = line.removeprefix("# ").removesuffix(" is not set")
            values[key] = "n"
            continue
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _first_token_with_prefix(cmdline: str, prefix: str) -> str | None:
    for token in cmdline.split():
        if token.startswith(prefix):
            return token
    return None
