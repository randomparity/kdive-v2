"""Tests for the three-check destructive-op gate (ADR-0006, ADR-0020)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.security.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.rbac import Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(role: Role = Role.ADMIN) -> RequestContext:
    return RequestContext(
        principal="alice", agent_session=None, projects=("proj",), roles={"proj": role}
    )


def _allocation(scope: dict[str, Any]) -> Allocation:
    return Allocation.model_validate(
        dict(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=uuid4(),
            state=AllocationState.ACTIVE,
            capability_scope=scope,
        )
    )


def _op(opt_in: bool = True) -> DestructiveOp:
    return DestructiveOp(kind="force_crash", profile_opt_in=opt_in)


def test_all_three_present_is_allowed() -> None:
    assert (
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation({"destructive_ops": ["force_crash"]}), _op(True)
        )
        is None
    )


def test_scope_absent_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.ADMIN), _allocation({}), _op(True))
    assert exc.value.missing == ["capability_scope"]


def test_not_admin_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.OPERATOR), _allocation({"destructive_ops": ["force_crash"]}), _op(True)
        )
    assert exc.value.missing == ["admin_role"]


def test_opt_in_false_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation({"destructive_ops": ["force_crash"]}), _op(False)
        )
    assert exc.value.missing == ["profile_opt_in"]


def test_opt_in_defaults_false() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN),
            _allocation({"destructive_ops": ["force_crash"]}),
            DestructiveOp(kind="force_crash"),
        )
    assert exc.value.missing == ["profile_opt_in"]


def test_all_three_absent_lists_all() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation({}), _op(False))
    assert exc.value.missing == ["capability_scope", "admin_role", "profile_opt_in"]


def test_scope_with_non_list_value_fails_closed() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation({"destructive_ops": "force_crash"}), _op(True)
        )
    assert exc.value.missing == ["capability_scope"]
