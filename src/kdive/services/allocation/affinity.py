"""Per-project resource affinity predicate (ADR-0112, M2.6 Task 4.2).

A project may place only on a **global** resource (``owner_project IS NULL``) or one it
owns (``owner_project == project``) or one that lists the project in its
``affinity_allowlist``. Every pre-existing discovered + config-declared resource is global
(Phase-1 backfill leaves ``owner_project`` NULL with an empty allowlist), so the predicate
is a strict no-op for current behavior — no allocation that works today regresses.

The same predicate gates both layers: it filters the any-available candidate set in
``placement.py`` (so a disallowed scoped instance is never selected and an any-available
request falls through to a legal global one) and backstops the explicit ``resource_id``
path in ``admission.py``.
"""

from __future__ import annotations

from kdive.domain.models import Resource


def project_may_place(resource: Resource, project: str) -> bool:
    """Report whether ``project`` is allowed to place on ``resource``.

    A global resource (``owner_project is None``) admits any project; a scoped resource
    admits only its owner or a project on its ``affinity_allowlist``.

    Args:
        resource: The candidate resource host.
        project: The placing project.

    Returns:
        ``True`` if the affinity predicate permits placement, else ``False``.
    """
    if resource.owner_project is None:
        return True
    return project == resource.owner_project or project in resource.affinity_allowlist
