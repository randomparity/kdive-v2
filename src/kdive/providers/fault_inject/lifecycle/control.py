"""Fault-inject Control plane."""

from __future__ import annotations

from kdive.domain.models import PowerAction


class FaultInjectControl:
    def power(self, domain_name: str, action: PowerAction) -> None:
        return None

    def force_crash(self, domain_name: str) -> None:
        return None
