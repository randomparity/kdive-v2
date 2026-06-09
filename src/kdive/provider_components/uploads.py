"""Shared upload declaration value types."""

from __future__ import annotations

from typing import NamedTuple


class ManifestEntry(NamedTuple):
    """One declared artifact: its name, base64 SHA-256, and byte size."""

    name: str
    sha256: str
    size_bytes: int
