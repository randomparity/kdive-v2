"""Shared MCP request payload models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DEFAULT_KIND = "local-libvirt"
_DEFAULT_COST_CLASS = "local"


class ToolPayload(BaseModel):
    """Base model for MCP JSON payloads."""

    model_config = ConfigDict(extra="forbid")


class SelectorPayload(ToolPayload):
    """The size/window shape shared by estimation and allocation admission.

    ``vcpus`` / ``memory_gb`` are optional on the base so a shape-named allocation request
    (which carries no raw sizing) validates; :class:`EstimateRequestPayload` re-declares
    them required (estimate is custom-only, ADR-0067), and :class:`AllocationRequestPayload`
    enforces the shape-XOR-custom rule.
    """

    vcpus: int | None = None
    memory_gb: int | None = None
    window: object | None = None


class ResourceById(ToolPayload):
    mode: Literal["id"]
    resource_id: str


class ResourceByKind(ToolPayload):
    mode: Literal["kind"] = "kind"
    kind: str = _DEFAULT_KIND


type ResourceSelector = ResourceById | ResourceByKind


class AllocationRequestPayload(SelectorPayload):
    """An allocation request sized by a named shape XOR a full custom triple (ADR-0067).

    Exactly one sizing source is supplied: ``shape`` (a catalog name resolved to the
    priced tuple at admission), or the full custom triple ``{vcpus, memory_gb, disk_gb}``.
    Both sides set, neither set, or a *partial* custom triple is a structural error here,
    so a partial size can never reach a NULL ``requested_disk_gb`` snapshot that would
    silently disable the size unification.
    """

    shape: str | None = None
    disk_gb: int | None = None
    resource: ResourceSelector = Field(default_factory=ResourceByKind, discriminator="mode")
    pcie_devices: list[str] = Field(
        default_factory=list,
        description="PCIe match specs ('vendor:device' or 'class=NN') to resolve + claim.",
    )

    @model_validator(mode="after")
    def _shape_xor_custom_triple(self) -> AllocationRequestPayload:
        custom = (self.vcpus, self.memory_gb, self.disk_gb)
        custom_set = [v is not None for v in custom]
        if self.shape is not None:
            if any(custom_set):
                raise ValueError("supply a shape or a custom size, not both")
            return self
        if not all(custom_set):
            raise ValueError(
                "supply a shape, or the full custom triple {vcpus, memory_gb, disk_gb}"
            )
        return self


class EstimateRequestPayload(SelectorPayload):
    vcpus: int
    memory_gb: int
    window: object
    cost_class: str = _DEFAULT_COST_CLASS
