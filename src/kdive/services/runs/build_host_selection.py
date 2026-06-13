"""Build-host selection and capacity admission for the ``runs.build`` tool boundary.

Resolves a :class:`~kdive.profiles.build.ServerBuildProfile`'s ``build_host`` name to a
live :class:`~kdive.db.build_hosts.BuildHost` row, validates it is available and
compatible with the profile's kernel-source provenance, and acquires one capacity lease
under the ``BUILD_HOST`` advisory lock so the lease and the subsequent ``BUILD`` job
enqueue commit atomically.

The caller must already hold an open transaction and the ``RUN`` advisory lock; this
function takes the ``BUILD_HOST`` lock inside that transaction (``RUN → BUILD_HOST`` in
the global lock order).
"""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.build_hosts import BuildHost, get_by_name, try_acquire_lease
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile, is_git_source


async def resolve_and_admit(
    conn: AsyncConnection,
    parsed_profile: ServerBuildProfile,
    run_id: UUID,
) -> BuildHost:
    """Resolve the build host for a server-lane Run and admit it under capacity.

    Caller MUST already hold an open transaction + the RUN advisory lock; this takes
    the BUILD_HOST lock (after RUN in the global order) and inserts the lease so the
    lease + the BUILD-job enqueue commit atomically.

    For ``kind='local'`` hosts no lease row is inserted (local builds are single-slot
    by convention, not tracked in ``build_host_leases``).

    Args:
        conn: An async psycopg connection with an open transaction. The RUN advisory
            lock must already be held on this connection.
        parsed_profile: The validated server-build profile for the Run being admitted.
        run_id: The Run's primary key, used as the lease owner.

    Returns:
        The resolved :class:`~kdive.db.build_hosts.BuildHost`.

    Raises:
        CategorizedError: ``NOT_FOUND`` when the named host is absent from the catalog;
            ``CONFIGURATION_ERROR`` when the host is disabled, unreachable, or its
            transport kind is incompatible with the profile's kernel-source provenance;
            ``CAPACITY_EXHAUSTED`` when the host exists and is available but all
            concurrent-build slots are occupied.
    """
    name = parsed_profile.build_host or "worker-local"
    host = await get_by_name(conn, name)
    if host is None:
        raise CategorizedError(
            f"build host '{name}' not found",
            category=ErrorCategory.NOT_FOUND,
            details={"build_host": name},
        )

    if not host.enabled or host.state == "unreachable":
        raise CategorizedError(
            f"build host '{name}' is not available",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": name, "enabled": host.enabled, "state": host.state},
        )

    git = is_git_source(parsed_profile)
    if host.kind == "local" and git:
        raise CategorizedError(
            "a local build host requires a warm-tree kernel_source_ref, not a git ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": name, "host_kind": host.kind},
        )
    if host.kind != "local" and not git:
        raise CategorizedError(
            "a remote build host requires a git kernel_source_ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": name, "host_kind": host.kind},
        )

    if host.kind != "local":
        ok = await try_acquire_lease(conn, host, run_id)
        if not ok:
            raise CategorizedError(
                f"build host '{name}' is at capacity",
                category=ErrorCategory.CAPACITY_EXHAUSTED,
                details={"build_host": name},
            )

    return host
