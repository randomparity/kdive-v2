"""Shared authz exception types."""

from __future__ import annotations


class AuthError(Exception):
    """A verified transport carried claims that cannot authorize the request."""
