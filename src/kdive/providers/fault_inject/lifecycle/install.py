"""Fault-inject Install and Boot planes."""

from __future__ import annotations

from uuid import UUID

from kdive.providers.ports import InstallRequest


class FaultInjectInstall:
    def install(self, request: InstallRequest) -> None:
        del request
        return None

    def boot(self, system_id: UUID) -> None:
        return None
