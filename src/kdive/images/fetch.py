"""Fetch a registered catalog rootfs object to a checksum-verified local cache (ADR-0092).

This wires what was a `not wired yet` stub: object-store-backed rootfs materialization. The
resolver returns a registered row; this downloads its ``object_key``, verifies the content
SHA-256 against the row's ``digest``, and caches it locally keyed by digest so a repeat boot of
the same image reuses the bytes. The object GET is offloaded via ``asyncio.to_thread`` (boto3 is
synchronous).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Protocol

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.catalog import resolve_rootfs
from kdive.provider_components import artifacts as artifact_types


class RootfsObjectStore(Protocol):
    """The narrow object-store capability the rootfs fetch needs (an :class:`ObjectStore`)."""

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact: ...


def _cache_path(cache_dir: Path, digest: str) -> Path:
    """A digest-keyed cache path so a repeat boot of the same image reuses the bytes."""
    return cache_dir / f"{digest.removeprefix('sha256:')}.qcow2"


async def fetch_registered_rootfs(
    conn: AsyncConnection,
    store: RootfsObjectStore,
    *,
    provider: str,
    name: str,
    project: str,
    cache_dir: Path,
) -> Path:
    """Resolve a registered rootfs row and return a checksum-verified local cache path.

    Resolves the registered image visible to ``project`` (private shadows public), downloads its
    ``object_key``, verifies the content SHA-256 against the row's ``digest``, and writes it to a
    digest-keyed file under ``cache_dir`` (reused on a cache hit).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no registered image resolves;
            ``INFRASTRUCTURE_FAILURE`` when the downloaded bytes do not match the row's digest.
    """
    row = await resolve_rootfs(conn, provider, name, project=project)
    if row is None:
        raise CategorizedError(
            "unknown registered rootfs catalog entry",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name},
        )
    # A registered row always has an object_key and a digest (the DB CHECK and the publish path
    # guarantee it), so both are present here.
    object_key = row.object_key
    digest = row.digest
    if object_key is None or digest is None:  # Defensive: a registered row carries both.
        raise CategorizedError(
            "registered rootfs row is missing its object_key or digest",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"provider": provider, "name": name},
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(cache_dir, digest)
    if cached.is_file():
        return cached

    fetched = await asyncio.to_thread(store.get_artifact, object_key, None)
    actual = "sha256:" + hashlib.sha256(fetched.data).hexdigest()
    if actual != digest:
        raise CategorizedError(
            "fetched rootfs object digest does not match the catalog row",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"provider": provider, "name": name, "object_key": object_key},
        )
    # Write through a temp sibling then atomically rename so a partial/corrupt download never
    # surfaces as a cache hit (and a mismatch above leaves the cache empty).
    tmp = cached.with_suffix(".qcow2.partial")
    tmp.write_bytes(fetched.data)
    tmp.replace(cached)
    return cached
