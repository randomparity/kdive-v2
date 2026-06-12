"""Default build-config catalog references and fetch seams."""

from __future__ import annotations

from collections.abc import Callable

import psycopg

import kdive.config as config
from kdive.build_configs.catalog import get_build_config_sync
from kdive.config.core_settings import DATABASE_URL
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.references import CatalogComponentRef
from kdive.store.objectstore import object_store_from_env

# The implicit config ref a build resolves when a profile names none: the seeded ``kdump``
# catalog fragment. Shared so the validation-time and execution-time substitutions cannot
# diverge. ``provider`` is decorative for build configs (the catalog is keyed by name alone)
# but the ref model requires it; ``system`` matches the seed tenant.
DEFAULT_CONFIG_REF = CatalogComponentRef(kind="catalog", provider="system", name="kdump")

# A synchronous catalog fetch the build path injects: name -> verified fragment bytes. It must
# be synchronous because ``build()`` runs off the event loop via ``asyncio.to_thread``.
type CatalogConfigFetch = Callable[[str], bytes]


def build_config_fetch_from_env() -> CatalogConfigFetch:
    """A synchronous ``name -> verified fragment bytes`` catalog fetch for the build path.

    Opens a short-lived sync ``psycopg`` connection and the env-configured object store per
    call (the build runs in a thread and owns no async pool). Looks the name up in
    ``build_config_catalog``, fetches the object, and verifies its sha256 against the row before
    returning the bytes. An unknown name is a ``CONFIGURATION_ERROR``; a sha mismatch surfaces as
    the repository's ``INFRASTRUCTURE_FAILURE``. The DB/object-store are resolved lazily inside
    the fetch so constructing the builder (``from_env``) opens no connection.
    """

    def _fetch(name: str) -> bytes:
        with psycopg.connect(config.require(DATABASE_URL)) as conn:
            entry = get_build_config_sync(conn, name)
        if entry is None:
            raise CategorizedError(
                "unknown build-config catalog entry",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"name": name},
            )
        fetched = object_store_from_env().get_artifact(entry.object_key, None)
        data = fetched.data
        entry.verify_bytes(data)
        return data

    return _fetch


__all__ = ["CatalogConfigFetch", "DEFAULT_CONFIG_REF", "build_config_fetch_from_env"]
