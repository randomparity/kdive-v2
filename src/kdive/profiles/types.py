"""Shared JSON document aliases for profile-shaped API input boundaries."""

from __future__ import annotations

from collections.abc import Mapping

type JsonObjectInput = Mapping[str, object]

type ProvisioningProfileInput = JsonObjectInput

type BuildProfileInput = JsonObjectInput

type ExpectedBootFailureInput = JsonObjectInput
