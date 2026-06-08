"""Shared JSON document aliases for profile-shaped API and persistence boundaries."""

from __future__ import annotations

from collections.abc import Mapping

type JsonObjectInput = Mapping[str, object]
type JsonObject = Mapping[str, object]

type ProvisioningProfileInput = JsonObjectInput
type SerializedProvisioningProfile = JsonObject

type BuildProfileInput = JsonObjectInput
type SerializedBuildProfile = JsonObject

type ExpectedBootFailureInput = JsonObjectInput
type SerializedExpectedBootFailure = JsonObject
