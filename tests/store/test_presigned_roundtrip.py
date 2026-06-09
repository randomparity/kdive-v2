"""Real-MinIO presigned-PUT checksum enforcement (ADR-0048 §2, §7).

Proves the load-bearing assumption: MinIO rejects a presigned PUT whose body checksum
disagrees with the signed ``x-amz-checksum-sha256``, and accepts a matching upload. Runs
against the ``minio_store`` testcontainer (Docker-gated; skips without Docker).
"""

from __future__ import annotations

import base64
import hashlib

import httpx

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import PresignPutRequest


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_presigned_put_rejects_checksum_mismatch(minio_store, key_ns: str) -> None:
    payload = b"correct-bytes"
    wrong = _b64_sha256(b"different")
    key = f"{key_ns}/runs/r1/kernel"
    presigned = minio_store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=wrong,
            size_bytes=len(payload),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
            expires_in=300,
        )
    )
    resp = httpx.put(presigned.url, content=payload, headers=presigned.required_headers)
    assert resp.status_code >= 400  # the signed checksum disagrees with the body


def test_presigned_put_accepts_matching_upload(minio_store, key_ns: str) -> None:
    payload = b"correct-bytes"
    checksum = _b64_sha256(payload)
    key = f"{key_ns}/runs/r1/kernel"
    presigned = minio_store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=checksum,
            size_bytes=len(payload),
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
            expires_in=300,
        )
    )
    resp = httpx.put(presigned.url, content=payload, headers=presigned.required_headers)
    assert resp.status_code < 300
    head = minio_store.head(key)
    assert head is not None
    assert head.checksum_sha256 == checksum
    assert head.size_bytes == len(payload)
