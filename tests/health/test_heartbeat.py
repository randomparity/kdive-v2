"""Tests for the affirmative liveness heartbeat."""

from __future__ import annotations

from kdive.health.heartbeat import Heartbeat


def test_heartbeat_starts_live() -> None:
    now = {"value": 100.0}
    heartbeat = Heartbeat(stale_after=10.0, now=lambda: now["value"])

    assert heartbeat.is_live() is True


def test_heartbeat_goes_stale_after_threshold() -> None:
    now = {"value": 100.0}
    heartbeat = Heartbeat(stale_after=10.0, now=lambda: now["value"])

    now["value"] = 109.999
    assert heartbeat.is_live() is True

    now["value"] = 110.0
    assert heartbeat.is_live() is False


def test_tick_refreshes_liveness_window() -> None:
    now = {"value": 100.0}
    heartbeat = Heartbeat(stale_after=10.0, now=lambda: now["value"])

    now["value"] = 109.0
    heartbeat.tick()
    now["value"] = 118.0
    assert heartbeat.is_live() is True

    now["value"] = 119.0
    assert heartbeat.is_live() is False
