"""Typed pydantic model for the ``systems.toml`` v2 inventory document (ADR-0112).

The model mirrors the ``systems.toml`` schema v2: a list of ``[[image]]`` entries
(each with a discriminated :data:`ImageSource` union), and per-provider instance
lists (``[[remote_libvirt]]`` / ``[[local_libvirt]]`` / ``[[fault_inject]]`` /
``[[build_host]]``).

Parse-time validation enforces three structural invariants:

1. image identity ``(provider, name, arch)`` is unique;
2. instance ``name`` is unique within each provider kind;
3. every instance ``base_image`` cross-reference names a declared ``[[image]]``.

Remote-libvirt is temporarily stricter: only one ``[[remote_libvirt]]`` instance is accepted
until provider operations carry selected Resource identity into remote config resolution.

:meth:`InventoryDoc.parse` is the sanctioned entry point: it wraps
:meth:`~pydantic.BaseModel.model_validate` and re-raises pydantic's structural
``ValidationError`` (e.g. a bad discriminator) as :class:`InventoryError`, so callers
always see one exception type.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, ValidationError

from kdive.inventory.errors import InventoryError


class S3Source(BaseModel):
    """An image realized from an object in the S3-compatible store."""

    kind: Literal["s3"]
    object_key: str
    digest: str | None = None
    """Required to reach ``registered``; a HEAD only confirms object existence."""


class BuildSource(BaseModel):
    """An image built in-tree from a base plus optional build components."""

    kind: Literal["build"]
    base: str
    components: list[str] = Field(default_factory=list)


class StagedSource(BaseModel):
    """An image backed by an operator-staged provider volume (no S3 object)."""

    kind: Literal["staged"]
    volume: str


ImageSource = Annotated[S3Source | BuildSource | StagedSource, Field(discriminator="kind")]
"""Discriminated union of image realization sources, keyed on the ``kind`` literal."""


class ImageEntry(BaseModel):
    """A single ``[[image]]`` declaration."""

    provider: str
    name: str
    arch: str
    format: str
    root_device: str
    visibility: Literal["public", "private"]
    capabilities: list[str] = Field(default_factory=list)
    source: ImageSource

    @property
    def identity(self) -> tuple[str, str, str]:
        """The stable identity tuple ``(provider, name, arch)``."""
        return (self.provider, self.name, self.arch)


class _Instance(BaseModel):
    """Shared fields for a provider instance declaration."""

    name: str
    cost_class: str
    concurrent_allocation_cap: int = 1


class RemoteLibvirtInstance(_Instance):
    """A ``[[remote_libvirt]]`` provider instance."""

    uri: str
    gdb_addr: str
    gdbstub_range: str
    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str
    base_image: str
    shapes: list[str] = Field(default_factory=list)


class LocalLibvirtInstance(_Instance):
    """A ``[[local_libvirt]]`` provider instance."""

    host_uri: str


class FaultInjectInstance(_Instance):
    """A ``[[fault_inject]]`` provider instance."""

    vcpus: int
    memory_mb: int
    seed: int = 0


class BuildHostInstance(BaseModel):
    """A ``[[build_host]]`` declaration."""

    name: str
    kind: str
    base_image_volume: str | None = None
    workspace_root: str
    max_concurrent: int = 1


class InventoryDoc(BaseModel):
    """The parsed ``systems.toml`` v2 document."""

    schema_version: Literal[2]
    image: list[ImageEntry] = Field(default_factory=list)
    remote_libvirt: list[RemoteLibvirtInstance] = Field(default_factory=list)
    local_libvirt: list[LocalLibvirtInstance] = Field(default_factory=list)
    fault_inject: list[FaultInjectInstance] = Field(default_factory=list)
    build_host: list[BuildHostInstance] = Field(default_factory=list)

    def _check_image_identities(self) -> None:
        seen: set[tuple[str, str, str]] = set()
        for img in self.image:
            if img.identity in seen:
                raise InventoryError(
                    f"image[{img.name}]",
                    "identity",
                    f"duplicate (provider,name,arch) {img.identity}",
                )
            seen.add(img.identity)

    def _check_base_image_refs(self) -> None:
        declared = {img.name for img in self.image}
        for inst in self.remote_libvirt:
            if inst.base_image not in declared:
                raise InventoryError(
                    f"remote_libvirt[{inst.name}]",
                    "base_image",
                    f"names undeclared image {inst.base_image!r}",
                )

    def _check_instance_name_uniqueness(self) -> None:
        groups: tuple[tuple[str, list[str]], ...] = (
            ("remote_libvirt", [i.name for i in self.remote_libvirt]),
            ("local_libvirt", [i.name for i in self.local_libvirt]),
            ("fault_inject", [i.name for i in self.fault_inject]),
            ("build_host", [i.name for i in self.build_host]),
        )
        for kind, names in groups:
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise InventoryError(kind, "name", f"duplicate instance names {dupes}")

    def _check_remote_libvirt_singleton(self) -> None:
        if len(self.remote_libvirt) <= 1:
            return
        names = sorted(inst.name for inst in self.remote_libvirt)
        raise InventoryError(
            "remote_libvirt",
            "instances",
            "multiple instances are not supported until per-op remote resource selection is wired "
            f"{names}",
        )

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Self:
        """Validate ``data`` into an :class:`InventoryDoc`.

        First runs pydantic structural validation, re-raising pydantic's
        ``ValidationError`` (e.g. an unknown source discriminator, a missing
        required field, or a bad ``schema_version``) as :class:`InventoryError`.
        Then runs the semantic checks (image-identity uniqueness, ``base_image``
        cross-reference, per-kind instance-name uniqueness) directly, so their
        :class:`InventoryError` propagates with its precise ``entry``/``field``
        intact rather than being flattened by a pydantic after-validator.

        Either way the caller observes exactly one exception type.

        Args:
            data: The decoded TOML mapping.

        Returns:
            The validated document.

        Raises:
            InventoryError: On any structural or semantic validation failure.
        """
        try:
            doc = cls.model_validate(data)
        except ValidationError as exc:
            raise InventoryError("inventory", "schema", str(exc)) from exc
        doc._check_image_identities()
        doc._check_base_image_refs()
        doc._check_instance_name_uniqueness()
        doc._check_remote_libvirt_singleton()
        return doc
