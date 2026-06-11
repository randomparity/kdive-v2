"""Identifier-label allowlist for metric and span attributes (ADR-0090 §4).

Redaction (``observability.redaction``) scrubs secret *values*; identifiers are a
*separate* disclosure surface. Raw tenant / ``principal`` / project / secret-ref
identifiers must not travel as metric labels or span attributes — high-cardinality
labels are a metrics-cost footgun, and per ADR-0089 who-and-what-exists is itself
reconnaissance data. Identifiers instead travel as log attributes (already access-
controlled by the log path). This module is the fixed, reviewed key set that bounds
what a metric/trace label may carry; widening it is an explicit edit here.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The reviewed set of keys permitted as metric labels / span attributes. Everything
#: else is dropped before export. Deliberately excludes ``principal``, ``tenant``,
#: ``project``, ``object_id``, and ``secret_ref`` (those go on the log path only).
ALLOWED_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "service.name",
        "service.namespace",
        "process",
        "tool",
        "job_kind",
        "provider",
        "outcome",
        "status_code",
        "transition_kind",
    }
)


def filter_label_keys[V](attributes: Mapping[str, V]) -> dict[str, V]:
    """Return only the allowlisted label keys from ``attributes``.

    Args:
        attributes: Candidate metric/span attributes.

    Returns:
        A new dict containing only keys in :data:`ALLOWED_LABEL_KEYS`; identifier
        keys are dropped so they never become free-cardinality labels.
    """
    return {key: value for key, value in attributes.items() if key in ALLOWED_LABEL_KEYS}
