"""Unit tests for the per-project affinity predicate (ADR-0112, Task 4.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from kdive.domain.models import Resource, ResourceKind
from kdive.domain.state import ResourceStatus
from kdive.services.allocation.affinity import project_may_place

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _resource(*, owner_project: str | None, allowlist: list[str]) -> Resource:
    return Resource(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=ResourceKind.LOCAL_LIBVIRT,
        pool="local-libvirt",
        cost_class="local",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
        owner_project=owner_project,
        affinity_allowlist=allowlist,
    )


def test_global_resource_admits_any_project() -> None:
    resource = _resource(owner_project=None, allowlist=[])
    assert project_may_place(resource, "alpha")
    assert project_may_place(resource, "beta")


def test_scoped_resource_admits_owner() -> None:
    resource = _resource(owner_project="alpha", allowlist=[])
    assert project_may_place(resource, "alpha")


def test_scoped_resource_rejects_foreign_project() -> None:
    resource = _resource(owner_project="alpha", allowlist=[])
    assert not project_may_place(resource, "beta")


def test_allowlisted_project_admitted() -> None:
    resource = _resource(owner_project="alpha", allowlist=["beta"])
    assert project_may_place(resource, "beta")


def test_empty_allowlist_does_not_admit_foreign() -> None:
    resource = _resource(owner_project="alpha", allowlist=[])
    assert not project_may_place(resource, "gamma")
