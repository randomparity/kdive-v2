# Object-store Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the S3-compatible artifact-storage client (`put`/`get` plus an
`artifacts`-row constructor) for the M0 walking skeleton.

**Architecture:** A synchronous `ObjectStore` wraps a boto3 S3 client and a bucket.
`put_artifact` writes under the key `{tenant}/{kind}/{object_id}/{name}` with
sensitivity/retention as object metadata; `get_artifact` does an `If-Match`
conditional GET and maps a missing/mismatched object to `stale_handle`. A pure
`register_artifact_row` builds the (uncommitted) `Artifact` row from the put result,
keeping `kdive.store` free of any `kdive.db` dependency. Every failure is a typed
`CategorizedError`. See
[`docs/superpowers/specs/2026-06-03-object-store-client-design.md`](../specs/2026-06-03-object-store-client-design.md)
and [ADR-0017](../../adr/0017-object-store-client-interface.md).

**Tech Stack:** Python 3.13, boto3/botocore, pydantic models (`kdive.domain`),
pytest + testcontainers (generic `DockerContainer` running MinIO), `uv`, `ruff`,
`ty`.

---

## Conventions (read once)

- Guardrails after every code change: `uv run ruff check`, `uv run ruff format`,
  `uv run ty check src tests`, `uv run python -m pytest -q`. All green before commit.
  `ty` checks `tests/` too — every test function is annotated `-> None` and every
  param is typed.
- The MinIO tests skip when Docker is unreachable (unless `KDIVE_REQUIRE_DOCKER=1`),
  exactly like `tests/db/conftest.py`. Run them locally with Docker up; if Docker is
  absent they skip (not fail) and the pure tests still run.
- boto3/botocore ship no stubs but `ty` resolves them; `S3Client = Any` needs no
  ignore (verified). Do not add `boto3-stubs`.

---

## Task 1: Module skeleton — types, key validation, etag normalization

**Files:**
- Create: `src/kdive/store/objectstore.py`
- Create: `tests/store/__init__.py`
- Create: `tests/store/test_objectstore.py`

- [ ] **Step 1: Create the empty test package marker**

Create `tests/store/__init__.py` with no content (an empty file), mirroring
`tests/db/__init__.py`.

- [ ] **Step 2: Write the failing tests (pure — no container)**

Create `tests/store/test_objectstore.py`:

Import only what each task uses, so every commit passes `ruff` F401 (CI runs
`ruff check .` over `tests/` too). Later tasks add their imports as noted.

```python
"""Behavior and edge tests for the object-store client (ADR-0017).

The MinIO-backed tests use the session ``minio_store`` fixture and gate on Docker
exactly as the db tests do; the pure tests (key validation, etag normalization,
``register_artifact_row``, env config) run without a container.
"""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import ObjectStore, _normalize_etag


def test_normalize_etag_strips_surrounding_quotes() -> None:
    assert _normalize_etag('"abc123"') == "abc123"
    assert _normalize_etag("abc123") == "abc123"


@pytest.mark.parametrize(
    ("tenant", "kind", "object_id", "name"),
    [
        ("", "vmcore", "oid", "core"),
        ("t", "vmcore", "oid", ""),
        ("t", "with/slash", "oid", "core"),
        ("t", "vmcore", "oid", "bad\nname"),
    ],
)
def test_put_artifact_rejects_invalid_key_component(
    tenant: str, kind: str, object_id: str, name: str
) -> None:
    store = ObjectStore(object(), "bucket")  # client never touched: validation precedes it
    with pytest.raises(CategorizedError) as excinfo:
        store.put_artifact(
            tenant,
            kind,
            object_id,
            name,
            data=b"x",
            sensitivity=Sensitivity.REDACTED,
            retention_class="vmcore",
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/store/test_objectstore.py -q`
Expected: FAIL — `ImportError` / `ModuleNotFoundError` (the module does not exist yet).

- [ ] **Step 4: Write the minimal module to make these pass**

Create `src/kdive/store/objectstore.py`:

```python
"""S3-compatible artifact storage for kdive (ADR-0013, ADR-0017).

Writes bulk artifacts under the key scheme ``{tenant}/{kind}/{object_id}/{name}``
with their sensitivity/retention recorded as object metadata, and reads them back
with an etag-consistency check. The client is synchronous (boto3); async callers
offload via ``asyncio.to_thread``. It is policy-neutral — it never decides whether a
fetched object may reach a response (the handler's redaction gate does, using the
returned sensitivity).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import boto3
from botocore.exceptions import ClientError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Artifact, Sensitivity

# boto3 ships no inline types and boto3-stubs is not a dependency; alias the S3
# client type to Any at this single site rather than add a stubs package.
S3Client = Any

_ENDPOINT_URL_ENV = "KDIVE_S3_ENDPOINT_URL"
_BUCKET_ENV = "KDIVE_S3_BUCKET"
_REGION_ENV = "KDIVE_S3_REGION"
_DEFAULT_REGION = "us-east-1"

# A missing object (404) and an etag mismatch (412) are the one stale_handle case.
_STALE_STATUSES = frozenset({404, 412})


class StoredArtifact(NamedTuple):
    """A put result: the row's ``key``/``etag`` plus the class written to the object."""

    key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str


class FetchedArtifact(NamedTuple):
    """A fetched object's bytes and the class read back from its metadata."""

    data: bytes
    sensitivity: Sensitivity
    retention_class: str


def _normalize_etag(raw: str) -> str:
    """Strip S3's surrounding double quotes from an ETag, leaving the bare value."""
    return raw.strip('"')


def _validate_component(label: str, value: str) -> str:
    """Return ``value`` if it is a safe single key segment, else raise.

    Raises:
        CategorizedError: ``value`` is empty or contains ``/`` or a control
            character (:attr:`ErrorCategory.CONFIGURATION_ERROR`).
    """
    if not value:
        raise CategorizedError(
            f"artifact key component {label!r} must not be empty",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if "/" in value or any(ord(char) < 0x20 for char in value):
        raise CategorizedError(
            f"artifact key component {label!r} has an illegal character: {value!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def _artifact_key(tenant: str, kind: str, object_id: str, name: str) -> str:
    """Build the validated ``{tenant}/{kind}/{object_id}/{name}`` object key."""
    return "/".join(
        (
            _validate_component("tenant", tenant),
            _validate_component("kind", kind),
            _validate_component("object_id", object_id),
            _validate_component("name", name),
        )
    )


def _infrastructure_error(op: str, key: str, err: ClientError) -> CategorizedError:
    """Map an unexpected S3 ``ClientError`` to a typed infrastructure failure."""
    code = err.response.get("Error", {}).get("Code", "unknown")
    return CategorizedError(
        f"object-store {op} for {key!r} failed: {code}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"key": key, "s3_error_code": code},
    )


class ObjectStore:
    """A synchronous S3-compatible artifact store bound to one bucket."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put_artifact(
        self,
        tenant: str,
        kind: str,
        object_id: str,
        name: str,
        *,
        data: bytes,
        sensitivity: Sensitivity,
        retention_class: str,
    ) -> StoredArtifact:
        """Write ``data`` under the key scheme; return its key, etag, and class.

        The object carries ``sensitivity`` and ``retention_class`` as user metadata.
        Async callers must offload this call via ``asyncio.to_thread``.

        Raises:
            CategorizedError: a key component is invalid
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or the put fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        key = _artifact_key(tenant, kind, object_id, name)
        try:
            resp = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                Metadata={
                    "sensitivity": sensitivity.value,
                    "retention-class": retention_class,
                },
            )
        except ClientError as err:
            raise _infrastructure_error("put_object", key, err) from err
        return StoredArtifact(
            key, _normalize_etag(resp["ETag"]), sensitivity, retention_class
        )

    def get_artifact(self, key: str, etag: str) -> FetchedArtifact:
        """Fetch the object at ``key`` iff its etag still matches ``etag``.

        ``etag`` is the bare value from :class:`StoredArtifact`; the conditional GET
        re-quotes it for the ``If-Match`` header. Async callers must offload via
        ``asyncio.to_thread``.

        Raises:
            CategorizedError: the object is missing or the etag no longer matches
                (:attr:`ErrorCategory.STALE_HANDLE`); the object lacks interpretable
                sensitivity metadata, or the get otherwise fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.get_object(
                Bucket=self._bucket, Key=key, IfMatch=f'"{etag}"'
            )
        except ClientError as err:
            status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in _STALE_STATUSES:
                raise CategorizedError(
                    f"artifact {key!r} is gone or its etag no longer matches",
                    category=ErrorCategory.STALE_HANDLE,
                    details={"key": key, "http_status": status},
                ) from err
            raise _infrastructure_error("get_object", key, err) from err
        metadata = resp["Metadata"]
        try:
            sensitivity = Sensitivity(metadata["sensitivity"])
            retention_class = metadata["retention-class"]
        except (KeyError, ValueError) as err:
            raise CategorizedError(
                f"artifact {key!r} has absent or invalid sensitivity metadata",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"key": key},
            ) from err
        return FetchedArtifact(resp["Body"].read(), sensitivity, retention_class)


def register_artifact_row(
    stored: StoredArtifact, *, owner_kind: str, owner_id: UUID
) -> Artifact:
    """Build the ``artifacts`` row for a stored object (no database access).

    The sensitivity/retention come from ``stored`` so the row matches the object by
    construction. The caller inserts and commits it after the object write
    (ADR-0005 write-before-commit). Timestamps are advisory — the DB overwrites them
    on insert (ADR-0016).
    """
    now = datetime.now(UTC)
    return Artifact(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        owner_kind=owner_kind,
        owner_id=owner_id,
        object_key=stored.key,
        etag=stored.etag,
        sensitivity=stored.sensitivity,
        retention_class=stored.retention_class,
    )


def object_store_from_env() -> ObjectStore:
    """Build an :class:`ObjectStore` from the ``KDIVE_S3_*`` environment.

    Reads ``KDIVE_S3_ENDPOINT_URL``, ``KDIVE_S3_BUCKET``, and ``KDIVE_S3_REGION``
    (default ``us-east-1`` — boto3 signs with SigV4 and needs a region). Credentials
    come from boto3's default chain (the standard ``AWS_*`` vars).

    Raises:
        CategorizedError: ``KDIVE_S3_ENDPOINT_URL`` or ``KDIVE_S3_BUCKET`` is unset
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`).
    """
    endpoint_url = os.environ.get(_ENDPOINT_URL_ENV)
    if not endpoint_url:
        raise CategorizedError(
            f"{_ENDPOINT_URL_ENV} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    bucket = os.environ.get(_BUCKET_ENV)
    if not bucket:
        raise CategorizedError(
            f"{_BUCKET_ENV} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    region = os.environ.get(_REGION_ENV) or _DEFAULT_REGION
    client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
    return ObjectStore(client, bucket)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/store/test_objectstore.py -q`
Expected: PASS (5 cases: 1 normalize + 4 parametrized validation).

- [ ] **Step 6: Run the guardrails**

Run: `uv run ruff check && uv run ruff format && uv run ty check src tests`
Expected: all green, no warnings.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/store/objectstore.py tests/store/__init__.py tests/store/test_objectstore.py
git commit -m "feat(store): object-store client skeleton with key validation"
```

---

## Task 2: `register_artifact_row` (pure)

**Files:**
- Modify: `tests/store/test_objectstore.py` (append)
- (implementation already written in Task 1 — this task tests it)

- [ ] **Step 1: Extend the imports, then write the failing test**

First add the names this task needs. Change the top imports so they read:

```python
from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import (
    ObjectStore,
    StoredArtifact,
    _normalize_etag,
    register_artifact_row,
)
```

Then append to `tests/store/test_objectstore.py`:

```python
def test_register_artifact_row_maps_stored_and_owner() -> None:
    stored = StoredArtifact("t/vmcore/oid/core", "etag123", Sensitivity.REDACTED, "vmcore")
    owner_id = uuid4()

    row = register_artifact_row(stored, owner_kind="system", owner_id=owner_id)

    assert row.object_key == "t/vmcore/oid/core"
    assert row.etag == "etag123"
    assert row.sensitivity is Sensitivity.REDACTED
    assert row.retention_class == "vmcore"
    assert row.owner_kind == "system"
    assert row.owner_id == owner_id
    # id is minted; created_at/updated_at are populated (advisory pre-insert).
    assert row.id is not None
```

- [ ] **Step 2: Run it to verify it passes**

Run: `uv run python -m pytest tests/store/test_objectstore.py::test_register_artifact_row_maps_stored_and_owner -q`
Expected: PASS (the implementation landed in Task 1).

> Note: this is a verification-of-existing-behavior test, so it passes immediately;
> the red→green discipline was satisfied for this function by the type/shape design
> in the spec. If it does *not* pass, fix `register_artifact_row` before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/store/test_objectstore.py
git commit -m "test(store): cover register_artifact_row mapping"
```

---

## Task 3: `object_store_from_env` config

**Files:**
- Modify: `tests/store/test_objectstore.py` (append)

- [ ] **Step 1: Extend the imports, then write the failing tests**

Add `object_store_from_env` to the `kdive.store.objectstore` import so it reads:

```python
from kdive.store.objectstore import (
    ObjectStore,
    StoredArtifact,
    _normalize_etag,
    object_store_from_env,
    register_artifact_row,
)
```

Then append to `tests/store/test_objectstore.py`:

```python
def test_object_store_from_env_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("KDIVE_S3_BUCKET", "bucket")

    with pytest.raises(CategorizedError) as excinfo:
        object_store_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_object_store_from_env_requires_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)

    with pytest.raises(CategorizedError) as excinfo:
        object_store_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_object_store_from_env_defaults_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "bucket")
    monkeypatch.delenv("KDIVE_S3_REGION", raising=False)

    store = object_store_from_env()

    assert store._client.meta.region_name == "us-east-1"
```

- [ ] **Step 2: Run them to verify they pass**

Run: `uv run python -m pytest tests/store/test_objectstore.py -q -k object_store_from_env`
Expected: PASS (3 cases). `boto3.client(...)` builds a client offline (no network),
so the region-default test needs no Docker.

- [ ] **Step 3: Run the guardrails**

Run: `uv run ruff check && uv run ruff format && uv run ty check src tests`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/store/test_objectstore.py
git commit -m "test(store): cover object_store_from_env config and region default"
```

---

## Task 4: MinIO fixture + the integration tests (the acceptance)

**Files:**
- Create: `tests/store/conftest.py`
- Modify: `tests/store/test_objectstore.py` (append)

- [ ] **Step 1: Write the MinIO fixture**

Create `tests/store/conftest.py`:

```python
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
from botocore.exceptions import ClientError

from kdive.store.objectstore import ObjectStore

# MinIO's official images are archived; this is the last tag actually pushed to
# Docker Hub (the source-only 2025-10-15 patch was never published as an image).
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
        except (ClientError, OSError) as exc:
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
            f"http://{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(_MINIO_PORT)}"
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
```

- [ ] **Step 2: Write the failing integration tests**

Append to `tests/store/test_objectstore.py`:

```python
def test_put_get_round_trip(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        key_ns,
        "vmcore",
        "sys-1",
        "core.bin",
        data=b"payload-bytes",
        sensitivity=Sensitivity.REDACTED,
        retention_class="vmcore",
    )

    assert '"' not in stored.etag  # stored etag is the bare value
    fetched = minio_store.get_artifact(stored.key, stored.etag)
    assert fetched.data == b"payload-bytes"


def test_put_uses_the_key_scheme(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        key_ns,
        "vmcore",
        "oid",
        "core",
        data=b"x",
        sensitivity=Sensitivity.REDACTED,
        retention_class="vmcore",
    )
    assert stored.key == f"{key_ns}/vmcore/oid/core"


def test_sensitivity_persisted_as_object_metadata(
    minio_store: ObjectStore, key_ns: str
) -> None:
    stored = minio_store.put_artifact(
        key_ns,
        "transcript",
        "sys-1",
        "gdb.log",
        data=b"raw-transcript",
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="transcript",
    )

    fetched = minio_store.get_artifact(stored.key, stored.etag)
    assert fetched.sensitivity is Sensitivity.SENSITIVE
    assert fetched.retention_class == "transcript"

    raw = minio_store._client.head_object(Bucket=minio_store._bucket, Key=stored.key)
    assert raw["Metadata"]["sensitivity"] == "sensitive"
    assert raw["Metadata"]["retention-class"] == "transcript"


def test_get_with_stale_etag_raises_stale_handle(
    minio_store: ObjectStore, key_ns: str
) -> None:
    stored = minio_store.put_artifact(
        key_ns,
        "vmcore",
        "sys-1",
        "core.bin",
        data=b"payload",
        sensitivity=Sensitivity.REDACTED,
        retention_class="vmcore",
    )

    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(stored.key, "0" * 32)
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_get_missing_object_raises_stale_handle(
    minio_store: ObjectStore, key_ns: str
) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(f"{key_ns}/vmcore/none/missing", "abc123")
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_get_object_without_metadata_raises_infrastructure_failure(
    minio_store: ObjectStore, key_ns: str
) -> None:
    key = f"{key_ns}/vmcore/sys-1/bare"
    resp = minio_store._client.put_object(
        Bucket=minio_store._bucket, Key=key, Body=b"no-metadata"
    )
    etag = resp["ETag"].strip('"')

    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(key, etag)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
```

- [ ] **Step 3: Run the integration tests (Docker required)**

Run: `uv run python -m pytest tests/store/test_objectstore.py -q`
Expected: all PASS with Docker up. If Docker is down, the six `minio_store` tests
SKIP and the pure tests PASS — that is the gated behavior, not a failure.

This step verifies the load-bearing conditional-GET contract: if MinIO ignored
`If-Match` on GET, `test_get_with_stale_etag_raises_stale_handle` would fail (no
error raised); if it required a different quoting, `test_put_get_round_trip` would
fail (false `STALE_HANDLE`). Both passing is the verification of record.

- [ ] **Step 4: Run the full guardrail suite**

Run: `uv run ruff check && uv run ruff format && uv run ty check src tests && uv run python -m pytest -q`
Expected: all green, no warnings.

- [ ] **Step 5: Commit**

```bash
git add tests/store/conftest.py tests/store/test_objectstore.py
git commit -m "test(store): MinIO round-trip, stale-handle, and metadata coverage"
```

---

## Task 5: Final verification pass

- [ ] **Step 1: Confirm the whole suite and guardrails are green**

Run: `uv run ruff check && uv run ruff format --check && uv run ty check src tests && uv run python -m pytest -q`
Expected: all green; the env-gated libvirt/gdb/drgn tests remain gated/skipped and
are untouched.

- [ ] **Step 2: Confirm no stray files outside the planned set**

Run: `git status --short`
Expected: only `src/kdive/store/objectstore.py`, `tests/store/__init__.py`,
`tests/store/conftest.py`, `tests/store/test_objectstore.py` were added (the docs
landed in earlier commits).

- [ ] **Step 3: Verify the MinIO image is required-test-safe in CI**

CI (`/.github/workflows/ci.yml`) runs `pytest -m "not live_vm"` with
`KDIVE_REQUIRE_DOCKER=1`, so the six `minio_store` tests are **required** — they
hard-fail (not skip) if the pinned image cannot be pulled on the GitHub-hosted
runner. The pinned tag is from MinIO's now-archived repo; archived tags remain
pullable, but the PR's CI run is the verification of record. When watching CI: if the
store tests error on `container.start()` / image pull, switch `_MINIO_IMAGE` to a
maintained equivalent (a Chainguard MinIO image or a localstack S3 fixture per
ADR-0017) and re-push — do not weaken `KDIVE_REQUIRE_DOCKER` or mark the tests
skippable.

---

## Self-review notes (verification against the spec)

- **Spec coverage:** `put_artifact` (Task 1/4), `get_artifact` (Task 1/4),
  `register_artifact_row` (Task 1/2), `object_store_from_env` (Task 1/3), key
  scheme + validation (Task 1/4), etag normalization & `If-Match` quoting (Task 4),
  sensitivity-as-metadata + the metadata-less failure (Task 4), stale-handle on both
  miss and mismatch (Task 4), region default (Task 3), test isolation via `key_ns`
  (Task 4). Every spec test bullet maps to a test here.
- **Out of scope (spec non-goals, no task):** `artifacts` row insert/commit,
  lifecycle config, redaction enforcement, write-once guard, reconciler GC,
  streaming/multipart upload, artifact authorization.
- **Type consistency:** `StoredArtifact(key, etag, sensitivity, retention_class)`,
  `FetchedArtifact(data, sensitivity, retention_class)`,
  `register_artifact_row(stored, *, owner_kind, owner_id)`, and `ObjectStore(client,
  bucket)` are used identically across all tasks and match the spec's signatures.
