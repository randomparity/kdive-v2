"""Domain-owned JSON document aliases for persisted profile-shaped columns."""

from __future__ import annotations

from collections.abc import Mapping

type JsonObject = Mapping[str, object]

type SerializedProvisioningProfile = JsonObject
type SerializedBuildProfile = JsonObject
type SerializedExpectedBootFailure = JsonObject
