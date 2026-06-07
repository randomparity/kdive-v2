"""Provider component-source capability validation (ADR-0065)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from kdive.components.references import ComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory

type ComponentKind = Literal["rootfs", "kernel", "initrd", "config", "patch", "vmlinux"]
type ComponentSourceKind = Literal["local", "artifact", "catalog"]


@dataclass(frozen=True, slots=True)
class ComponentSourceCapabilities:
    provider: str
    accepted_component_sources: Mapping[str, frozenset[str]]


def reject_unsupported_component_source(
    caps: ComponentSourceCapabilities,
    *,
    component_kind: str,
    ref: ComponentRef,
) -> None:
    """Raise a configuration error when ``ref`` is not advertised for a component kind."""
    accepted = caps.accepted_component_sources.get(component_kind, frozenset())
    if ref.kind in accepted:
        return

    raise CategorizedError(
        "provider does not accept this component source",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "provider": caps.provider,
            "component_kind": component_kind,
            "source_kind": ref.kind,
            "accepted_source_kinds": sorted(accepted),
        },
    )
