"""The single parse/validation failure type for the inventory package (ADR-0112).

Every ``systems.toml`` failure path — missing file, malformed TOML, or schema
validation error — is surfaced as :class:`InventoryError`, so callers fault-isolate
one exception type rather than discriminating ``OSError`` / ``TOMLDecodeError`` /
``ValidationError`` individually.
"""

from __future__ import annotations


class InventoryError(ValueError):
    """A ``systems.toml`` parse/validation failure naming the offending entry + field.

    Args:
        entry: The offending document entry (e.g. ``"image[base]"`` or the file path).
        field: The offending field within that entry (e.g. ``"base_image"``).
        msg: A human-readable description of the failure.
    """

    def __init__(self, entry: str, field: str, msg: str) -> None:
        self.entry = entry
        self.field = field
        super().__init__(f"{entry}.{field}: {msg}")
