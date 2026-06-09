"""Stable error taxonomy and the typed failure carrier (ADR-0001).

The PoC's stable :class:`ErrorCategory` is reused so failure strings stay
comparable across the rewrite. This module carries the categories current tool and
provider seams can emit, plus the distributed categories introduced by the service
architecture. The PoC's ``test_failure`` is intentionally absent because there is no
test plane emitting it.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """The closed set of failure categories a tool may report.

    Values are stable wire strings — handlers pick the most specific category and
    never invent new strings (``m0-walking-skeleton.md``).
    """

    # Reused from the PoC taxonomy.
    CONFIGURATION_ERROR = "configuration_error"
    MISSING_DEPENDENCY = "missing_dependency"
    BUILD_FAILURE = "build_failure"
    BOOT_TIMEOUT = "boot_timeout"
    READINESS_FAILURE = "readiness_failure"
    DEBUG_ATTACH_FAILURE = "debug_attach_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    STALE_HANDLE = "stale_handle"
    TRANSPORT_CONFLICT = "transport_conflict"
    NOT_IMPLEMENTED = "not_implemented"

    # Distributed categories for the async worker / provider seams.
    ALLOCATION_DENIED = "allocation_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    LEASE_EXPIRED = "lease_expired"
    QUEUE_TIMEOUT = "queue_timeout"
    PROVISIONING_FAILURE = "provisioning_failure"
    INSTALL_FAILURE = "install_failure"
    TRANSPORT_FAILURE = "transport_failure"
    CONTROL_FAILURE = "control_failure"
    AUTHORIZATION_DENIED = "authorization_denied"


class CategorizedError(Exception):
    """An error carrying the :class:`ErrorCategory` a failure response needs.

    Raised by domain and provider code so a handler maps any failure onto a
    typed failure response without per-exception special-casing.
    """

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        details: dict[str, object] | None = None,
    ) -> None:
        """Build a categorized error.

        Args:
            message: Human-readable failure description.
            category: The taxonomy category this failure maps to.
            details: Optional structured context (must be free of secret material;
                it may be surfaced in responses and logs).
        """
        super().__init__(message)
        self.category = category
        self.details = details or {}
