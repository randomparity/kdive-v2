"""Disposable-MinIO fixtures for the object-store tests (ADR-0017).

``minio_store`` starts one MinIO container per session and yields a configured
:class:`ObjectStore` against a fresh bucket. When the Docker daemon is unreachable
the fixture skips, unless ``KDIVE_REQUIRE_DOCKER=1`` (set in CI), which turns the
skip into a hard failure so a broken runner cannot mask the suite. ``key_ns`` gives
each test a unique key prefix so tests sharing the session bucket cannot collide.

MinIO's official image is archived (final tag pinned below); if it stops resolving,
swap in localstack or a Chainguard MinIO rebuild (ADR-0017).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from kdive.store.objectstore import ObjectStore

# MinIO's official images are archived; the last tag actually pushed to Docker Hub
# is RELEASE.2025-09-07T16-13-09Z (the later source-only 2025-10-15 patch was never
# published as an image). Pinned for the disposable test container; if it stops
# resolving, swap to a Chainguard MinIO rebuild or a localstack S3 fixture (ADR-0017).
_MINIO_IMAGE = "minio/minio:RELEASE.2025-09-07T16-13-09Z"
_MINIO_PORT = 9000
_ROOT_USER = "kdive-test"
_ROOT_PASSWORD = "kdive-test-secret"  # disposable local test container credential
_BUCKET = "kdive-test"
_REGION = "us-east-1"
_READY_TIMEOUT_S = 60.0


def _await_ready(client: Any) -> None:
    """Poll ``list_buckets`` until MinIO answers or the timeout elapses."""
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.list_buckets()
            return
        except (BotoCoreError, ClientError, OSError) as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(f"MinIO not ready within {_READY_TIMEOUT_S}s: {last_exc}")


@pytest.fixture(scope="session")
def minio_store() -> Iterator[ObjectStore]:
    require_docker = os.environ.get("KDIVE_REQUIRE_DOCKER") == "1"
    try:
        from testcontainers.core.container import DockerContainer
    except ImportError as exc:  # pragma: no cover - dev dep always present
        if require_docker:
            raise
        pytest.skip(f"testcontainers not installed: {exc}")

    container = (
        DockerContainer(_MINIO_IMAGE)
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", _ROOT_USER)
        .with_env("MINIO_ROOT_PASSWORD", _ROOT_PASSWORD)
        .with_exposed_ports(_MINIO_PORT)
    )
    try:
        container.start()
    except Exception as exc:  # Docker daemon unreachable / image pull failure.
        if require_docker:
            raise
        pytest.skip(f"Docker unavailable for testcontainers: {exc}")
    try:
        endpoint = (
            f"http://{container.get_container_host_ip()}:{container.get_exposed_port(_MINIO_PORT)}"
        )
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=_REGION,
            aws_access_key_id=_ROOT_USER,
            aws_secret_access_key=_ROOT_PASSWORD,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        _await_ready(client)
        client.create_bucket(Bucket=_BUCKET)
        yield ObjectStore(client, _BUCKET)
    finally:
        container.stop()


@pytest.fixture
def key_ns() -> str:
    """A per-test unique key prefix (used as the ``tenant`` component)."""
    return uuid4().hex
