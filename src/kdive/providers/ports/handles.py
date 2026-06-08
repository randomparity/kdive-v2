"""Shared provider handle and record value types."""

from __future__ import annotations

from typing import NewType, TypedDict

SystemHandle = NewType("SystemHandle", str)
TransportHandle = NewType("TransportHandle", str)


class OwnedInfra(TypedDict):
    """Infrastructure a provider owns, for the reconciler discovery port."""

    system_id: str
    domain_name: str
