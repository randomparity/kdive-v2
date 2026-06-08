"""Shared MCP request payload models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_KIND = "local-libvirt"
_DEFAULT_COST_CLASS = "local"


class ToolPayload(BaseModel):
    """Base model for MCP JSON payloads."""

    model_config = ConfigDict(extra="forbid")


class SelectorPayload(ToolPayload):
    """The size/window shape shared by estimation and allocation admission."""

    vcpus: int
    memory_gb: int
    window: object | None = None


class ResourceById(ToolPayload):
    mode: Literal["id"]
    resource_id: str


class ResourceByKind(ToolPayload):
    mode: Literal["kind"] = "kind"
    kind: str = _DEFAULT_KIND


type ResourceSelector = ResourceById | ResourceByKind


class AllocationRequestPayload(SelectorPayload):
    resource: ResourceSelector = Field(default_factory=ResourceByKind, discriminator="mode")
    pcie_devices: list[str] = Field(
        default_factory=list,
        description="PCIe match specs ('vendor:device' or 'class=NN') to resolve + claim.",
    )


class EstimateRequestPayload(SelectorPayload):
    window: object
    cost_class: str = _DEFAULT_COST_CLASS
