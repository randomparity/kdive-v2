"""Tests for the job-handler registry (ADR-0018)."""

from __future__ import annotations

import pytest

from kdive.domain.models import JobKind
from kdive.jobs.models import DuplicateHandler, HandlerRegistry


async def _noop(conn: object, job: object) -> str | None:
    return None


def test_get_returns_registered_handler() -> None:
    reg = HandlerRegistry()
    reg.register(JobKind.BUILD, _noop)
    assert reg.get(JobKind.BUILD) is _noop


def test_get_unregistered_returns_none() -> None:
    assert HandlerRegistry().get(JobKind.PROVISION) is None


def test_register_duplicate_raises() -> None:
    reg = HandlerRegistry()
    reg.register(JobKind.BUILD, _noop)
    with pytest.raises(DuplicateHandler):
        reg.register(JobKind.BUILD, _noop)
