"""Operator/admin ``images.*`` handler package."""

from __future__ import annotations

from kdive.mcp.tools.ops.images.build_publish import build, publish
from kdive.mcp.tools.ops.images.delete import delete
from kdive.mcp.tools.ops.images.retention import extend, prune_expired
from kdive.mcp.tools.ops.images.upload import ImageUploadRequest, upload

__all__ = [
    "ImageUploadRequest",
    "build",
    "delete",
    "extend",
    "prune_expired",
    "publish",
    "upload",
]
