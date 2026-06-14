# External-Build Artifact Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent upload locally-built kernel artifacts (and a rootfs reference) so an external-build Run lands `succeeded` with a validated, well-formed `BuildOutput` — without server-side `make`.

**Architecture:** A parallel build lane selected by `BuildProfile.source`. The agent gets short-TTL presigned PUTs (`artifacts.create_upload`), uploads bytes straight to the object store, and finalizes synchronously with `runs.complete_build` (validate → write the write-once `artifacts` rows + the `run_steps` ledger → `created → running → succeeded`, under the per-Run advisory lock). A persisted upload manifest anchors integrity; an owner-agnostic reconciler reaper prefix-sweeps abandoned uploads. Rootfs is a System/provisioning input resolving a source kind (`path`/`upload`/`url`/`catalog`).

**Tech Stack:** Python 3.13, Pydantic v2, psycopg (async), boto3 (S3/MinIO), FastMCP, pytest. Tooling: `uv`, `ruff`, `ty`, `just`.

---

## Reading order & ground truth

Implement against these refs (already read during planning; re-open as needed):

- Object store: `src/kdive/store/objectstore.py` — `ObjectStore`, `_artifact_key`, `register_artifact_row`, `StoredArtifact`, `_infrastructure_error`.
- Build profile: `src/kdive/profiles/build.py` — `BuildProfile` (frozen, `extra="forbid"`), `parse`.
- Runs surface + lifecycle: `src/kdive/mcp/tools/runs.py` — `_build_locked`, `_finalize_build`, `_existing_build_result`, `build_run`, `register`.
- Artifacts surface: `src/kdive/mcp/tools/artifacts.py` — `register`, the `list`/`get` precedent that returns `list[ToolResponse]`.
- Provider build: `src/kdive/providers/local_libvirt/build.py` — `parse_gnu_build_id`, `BuildOutput`, `_NT_GNU_BUILD_ID`.
- Provisioning: `src/kdive/profiles/provisioning.py` (`LibvirtProfile.rootfs_image_ref`), `src/kdive/providers/local_libvirt/provisioning.py` (`render_domain_xml`).
- Reconciler: `src/kdive/reconciler/loop.py` — `reconcile_once`, the `_isolated` repair pattern, `ReconcileReport`.
- Schema/migrations: `src/kdive/db/schema/0001_init.sql` (tables `runs`, `run_steps`, `systems`, `artifacts`), `src/kdive/db/migrate.py` (forward-only, checksum-immutable, `NNNN_*.sql`).
- Domain: `src/kdive/domain/models.py` (`Run`, `Artifact`, `System`), `src/kdive/domain/state.py` (`RunState`), `src/kdive/domain/errors.py` (`ErrorCategory`).
- Envelope: `src/kdive/mcp/responses.py` (`ToolResponse`, `refs`/`data` are `dict[str,str]`).
- Rootfs catalog note: the standalone v1 catalog port was superseded by the provider fixture
  catalog in `fixtures/local-libvirt/manifest.yaml` and `kdive.provider_components.catalog`.

**Error taxonomy (existing `ErrorCategory` only — never invent strings):** missing/skipped upload → `CONFIGURATION_ERROR`; defective uploaded artifact (checksum/size mismatch, bad magic, `build_id` mismatch) → `BUILD_FAILURE`; object-store/presign failure → `INFRASTRUCTURE_FAILURE`.

**Commands (CI runs these recipes individually — see memory):**
- Lint: `just lint` (or `ruff check src tests`)
- Types: `just type` (or `ty check`)
- Unit tests: `just test`
- Live MinIO/stack tests: `just test-live-stack`
- Run a single test: `uv run pytest tests/path/test_x.py::test_name -v`

**Conventions to honor:** absolute imports only; ≤100 lines/function, complexity ≤8; Google-style docstrings on public APIs; the repo's doc-style guard bans a set of inflated adjectives and the time-boxed-iteration term (use "Milestone") — keep prose plain and factual; commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; conventional-commit subjects; never push to main.

---

## Shared contracts (defined once; tasks reference these exact names/signatures)

These are introduced by the tasks noted; later tasks must use them verbatim.

**Object store (Tasks 1–3), in `src/kdive/store/objectstore.py`:**

```python
class HeadResult(NamedTuple):
    size_bytes: int
    checksum_sha256: str | None  # base64 of the SHA-256 digest, or None if not stored
    etag: str

class PresignedUpload(NamedTuple):
    url: str
    required_headers: dict[str, str]  # headers the agent MUST send on the PUT

def artifact_key(tenant: str, kind: str, object_id: str, name: str) -> str: ...   # public wrapper of _artifact_key
def owner_prefix(tenant: str, kind: str, object_id: str) -> str: ...               # "{tenant}/{kind}/{object_id}/"

class ObjectStore:
    def head(self, key: str) -> HeadResult | None: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...
    def presign_put(self, key: str, *, sha256: str, size_bytes: int,
                    sensitivity: Sensitivity, retention_class: str,
                    expires_in: int) -> PresignedUpload: ...
    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
```

- `sha256` everywhere in this feature is **base64-encoded** SHA-256 (the `x-amz-checksum-sha256` wire form), so the signed PUT condition, the manifest, and the `head` comparison are byte-identical — no hex↔base64 conversion.
- `head` passes `ChecksumMode="ENABLED"` so `ChecksumSHA256` is returned.

**Build profile (Task 4), in `src/kdive/profiles/build.py`:**

```python
class ServerBuildProfile(BaseModel):     # source="server"; the existing fields
class ExternalBuildProfile(BaseModel):   # source="external"; no source-tree fields
type ParsedBuildProfile = ServerBuildProfile | ExternalBuildProfile
class BuildProfile:
    @classmethod
    def parse(cls, data: Mapping[str, object]) -> ParsedBuildProfile: ...  # dispatch on source (default "server")
```

**Upload manifest (Task 5), new module `src/kdive/db/upload_manifest.py`:**

```python
class ManifestEntry(NamedTuple):
    name: str
    sha256: str
    size_bytes: int

class UploadManifest(NamedTuple):
    entries: tuple[ManifestEntry, ...]
    prefix: str
    deadline: datetime

async def replace_manifest(conn, *, owner_kind: str, owner_id: UUID, prefix: str,
                           entries: Sequence[ManifestEntry], ttl: timedelta) -> None: ...
async def get_manifest(conn, owner_kind: str, owner_id: UUID) -> UploadManifest | None: ...
async def delete_manifest(conn, owner_kind: str, owner_id: UUID) -> None: ...
```

`owner_kind` values are the existing object-store kinds: `"runs"` and `"systems"`.

**Validator (Task 7), in `src/kdive/providers/local_libvirt/build.py`:** reuses `BuildOutput`.

```python
class ValidatedUpload(NamedTuple):
    output: BuildOutput
    heads: dict[str, HeadResult]   # name -> head, returned so finalize needs no second HEAD

def validate_external_artifacts(store, *, manifest: Sequence[ManifestEntry],
                                keys: Mapping[str, str],          # name -> object key
                                declared_build_id: str | None) -> ValidatedUpload: ...
def extract_build_id_ranged(store, key: str) -> str: ...          # ELF64-LE ranged build-id read
```

Returning `heads` from the single validation pass keeps `runs.complete_build` fully injectable — the tool writes the write-once `artifacts` rows from `ValidatedUpload.heads`, never calling the object store directly (so a unit test that injects a fake validator does no S3 IO).

**Shared constants (Task 6), in `src/kdive/mcp/tools/artifacts.py`:**

```python
_BUILD_ARTIFACT_NAMES = frozenset({"kernel", "initrd", "vmlinux"})
_ROOTFS_NAME = "rootfs"
_TENANT = "local"  # mirrors providers/local_libvirt/build.py
```

TTL/cap come from env with defaults (Task 6): `KDIVE_UPLOAD_TTL_SECONDS` (default 86400), `KDIVE_MAX_UPLOAD_BYTES` (default 8 GiB = 8589934592).

---

## File structure

**Create:**
- `src/kdive/db/upload_manifest.py` — owner-scoped manifest read/replace/delete (Task 5).
- `src/kdive/db/schema/0006_upload_manifests.sql` — the `upload_manifests` table (Task 5).
- `fixtures/local-libvirt/manifest.yaml`, `fixtures/local-libvirt/rootfs/*.yaml`, and
  `src/kdive/components/catalog.py` — provider fixture catalog for rootfs references.
- `tests/store/test_objectstore_ingestion.py` — pure + MinIO tests for the new store methods (Tasks 1–3, 12).
- `tests/profiles/test_build_profile_source.py` — discriminated profile (Task 4).
- `tests/db/test_upload_manifest.py` — manifest storage (Task 5).
- `tests/mcp/test_create_upload_tool.py` — `artifacts.create_upload` (Task 6).
- `tests/providers/local_libvirt/test_validate_external_artifacts.py` — validator (Task 7).
- `tests/mcp/test_complete_build_tool.py` — `runs.complete_build` + source gate (Task 8).
- `tests/provider_components/test_catalog.py`, `tests/provider_components/test_default_fixture_catalog.py` —
  provider fixture catalog coverage.
- `tests/profiles/test_rootfs_source.py` — `RootfsSource` schema (Task 10).
- `tests/providers/local_libvirt/test_rootfs_resolve.py` — provisioning resolver (Task 10).
- `tests/reconciler/test_upload_reaper.py` — prefix reaper (Task 11).
- `tests/adversarial/test_complete_build_concurrency.py` — concurrent finalize + reaper-vs-live (Task 13).
- `tests/integration/live_stack/test_presigned_upload.py` — real MinIO round-trip rejection/success (Task 12).

**Modify:**
- `src/kdive/store/objectstore.py` — add the 6 methods + 2 key helpers (Tasks 1–3).
- `src/kdive/profiles/build.py` — discriminated profile (Task 4).
- `src/kdive/providers/local_libvirt/build.py` — validator + ranged build-id (Task 7).
- `src/kdive/mcp/tools/artifacts.py` — `create_upload` + registration (Task 6).
- `src/kdive/mcp/tools/runs.py` — `complete_build` + `runs.build` source gate + registration (Task 8).
- `src/kdive/profiles/provisioning.py` — `RootfsSource` field on `LibvirtProfile` (Task 10).
- `src/kdive/providers/local_libvirt/provisioning.py` — resolve rootfs to a disk path in `render_domain_xml` (Task 10).
- `src/kdive/mcp/tools/systems.py` — commit the rootfs `artifacts` row + manifest delete when provisioning consumes an `upload` rootfs (Task 10).
- `src/kdive/reconciler/loop.py` — add the upload reaper repair to `reconcile_once`/`ReconcileReport` (Task 11).
- `src/kdive/__main__.py` — inject an `ObjectStore` into the `Reconciler` (Task 11). *(Verify the wiring site; `reconcile_once` currently takes only `reaper`.)*

---

# Milestone A — Build-artifact ingestion lane

Produces a working capability on its own: an external-build Run reaches `succeeded` with validated artifacts. Tasks 1–8, 11–13.

---

### Task 1: Object store — `head` + `HeadResult`

**Files:**
- Modify: `src/kdive/store/objectstore.py`
- Test: `tests/store/test_objectstore_ingestion.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/store/test_objectstore_ingestion.py`:

```python
"""Tests for the ingestion-lane object-store methods (ADR-0048)."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import HeadResult, ObjectStore


class _HeadClient:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self._response = response

    def head_object(self, **_kwargs: object) -> dict[str, object]:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
    )


def test_head_returns_size_checksum_and_etag() -> None:
    store = ObjectStore(
        _HeadClient(
            {
                "ContentLength": 42,
                "ChecksumSHA256": "Zm9vYmFy",
                "ETag": '"abc123"',
            }
        ),
        "bucket",
    )
    result = store.head("t/runs/r1/kernel")
    assert result == HeadResult(size_bytes=42, checksum_sha256="Zm9vYmFy", etag="abc123")


def test_head_missing_object_returns_none() -> None:
    store = ObjectStore(_HeadClient(_not_found()), "bucket")
    assert store.head("t/runs/r1/kernel") is None


def test_head_without_checksum_metadata_yields_none_checksum() -> None:
    store = ObjectStore(_HeadClient({"ContentLength": 7, "ETag": '"e"'}), "bucket")
    result = store.head("t/runs/r1/kernel")
    assert result is not None and result.checksum_sha256 is None


def test_head_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(
        _HeadClient(EndpointConnectionError(endpoint_url="http://x")), "bucket"
    )
    with pytest.raises(CategorizedError) as excinfo:
        store.head("t/runs/r1/kernel")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_objectstore_ingestion.py -v`
Expected: FAIL (`ImportError: cannot import name 'HeadResult'`).

- [ ] **Step 3: Implement `HeadResult` + `head`**

In `src/kdive/store/objectstore.py`, add `HeadResult` next to `StoredArtifact`:

```python
class HeadResult(NamedTuple):
    """An object's stored size, base64 SHA-256 checksum (if any), and bare etag."""

    size_bytes: int
    checksum_sha256: str | None
    etag: str
```

Add the method to `ObjectStore` (after `get_artifact`):

```python
    def head(self, key: str) -> HeadResult | None:
        """Return the object's size/checksum/etag, or ``None`` if it does not exist.

        Requests ``ChecksumMode="ENABLED"`` so a checksum written at PUT is returned.

        Raises:
            CategorizedError: any non-404 store error
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.head_object(
                Bucket=self._bucket, Key=key, ChecksumMode="ENABLED"
            )
        except ClientError as err:
            status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return None
            raise _infrastructure_error("head_object", key, err) from err
        except BotoCoreError as err:
            raise _infrastructure_error("head_object", key, err) from err
        return HeadResult(
            size_bytes=int(resp["ContentLength"]),
            checksum_sha256=resp.get("ChecksumSHA256"),
            etag=_normalize_etag(resp["ETag"]),
        )
```

Add `HeadResult` to `__all__` if the module declares one (it does not currently; skip).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_objectstore_ingestion.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint, type, commit**

```bash
uv run ruff check src/kdive/store/objectstore.py tests/store/test_objectstore_ingestion.py
uv run ty check
git add src/kdive/store/objectstore.py tests/store/test_objectstore_ingestion.py
git commit -m "feat(store): add ObjectStore.head for ingestion validation"
```

---

### Task 2: Object store — `get_range`, `presign_put`, key helpers

**Files:**
- Modify: `src/kdive/store/objectstore.py`
- Test: `tests/store/test_objectstore_ingestion.py`

- [ ] **Step 1: Write the failing tests** (append to the ingestion test file)

```python
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import (  # noqa: E501  (extend the existing import)
    PresignedUpload,
    artifact_key,
    owner_prefix,
)


def test_artifact_key_and_owner_prefix_match_layout() -> None:
    assert artifact_key("local", "runs", "r1", "kernel") == "local/runs/r1/kernel"
    assert owner_prefix("local", "runs", "r1") == "local/runs/r1/"


class _RangeClient:
    def get_object(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["Range"] == "bytes=0-3"

        class _Body:
            def read(self) -> bytes:
                return b"\x7fELF"

        return {"Body": _Body()}


def test_get_range_requests_byte_range() -> None:
    store = ObjectStore(_RangeClient(), "bucket")
    assert store.get_range("t/runs/r1/vmlinux", start=0, length=4) == b"\x7fELF"


class _PresignClient:
    def __init__(self) -> None:
        self.params: dict[str, object] | None = None

    def generate_presigned_url(
        self, op: str, *, Params: dict[str, object], ExpiresIn: int, HttpMethod: str
    ) -> str:
        assert op == "put_object" and HttpMethod == "PUT"
        self.params = Params
        return f"https://store/put?exp={ExpiresIn}"


def test_presign_put_signs_checksum_and_metadata() -> None:
    client = _PresignClient()
    store = ObjectStore(client, "bucket")
    out = store.presign_put(
        "local/runs/r1/kernel",
        sha256="Zm9vYmFy",
        size_bytes=10,
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="build",
        expires_in=900,
    )
    assert isinstance(out, PresignedUpload)
    assert out.url == "https://store/put?exp=900"
    assert client.params is not None
    assert client.params["ChecksumSHA256"] == "Zm9vYmFy"
    assert client.params["Metadata"] == {
        "sensitivity": "sensitive",
        "retention-class": "build",
    }
    # The agent must echo the signed conditions back as headers.
    assert out.required_headers["x-amz-checksum-sha256"] == "Zm9vYmFy"
    assert out.required_headers["x-amz-meta-sensitivity"] == "sensitive"
    assert out.required_headers["x-amz-meta-retention-class"] == "build"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_objectstore_ingestion.py -v`
Expected: FAIL (import errors for `PresignedUpload`/`artifact_key`/`owner_prefix`).

- [ ] **Step 3: Implement helpers + methods**

In `src/kdive/store/objectstore.py`:

Add the public key helpers (after `_artifact_key`):

```python
def artifact_key(tenant: str, kind: str, object_id: str, name: str) -> str:
    """Public, validated ``{tenant}/{kind}/{object_id}/{name}`` key (ingestion + reaper)."""
    return _artifact_key(tenant, kind, object_id, name)


def owner_prefix(tenant: str, kind: str, object_id: str) -> str:
    """The validated ``{tenant}/{kind}/{object_id}/`` key prefix for an owner's objects."""
    base = "/".join(
        (
            _validate_component("tenant", tenant),
            _validate_component("kind", kind),
            _validate_component("object_id", object_id),
        )
    )
    return base + "/"
```

Add `PresignedUpload` next to `HeadResult`:

```python
class PresignedUpload(NamedTuple):
    """A presigned PUT URL plus the headers the client must send for it to validate."""

    url: str
    required_headers: dict[str, str]
```

Add the two methods to `ObjectStore`:

```python
    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        """Return ``length`` bytes of ``key`` starting at ``start`` (an HTTP ranged GET).

        Raises:
            CategorizedError: the ranged read fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        end = start + length - 1
        try:
            resp = self._client.get_object(
                Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end}"
            )
            return resp["Body"].read()
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("get_range", key, err) from err

    def presign_put(
        self,
        key: str,
        *,
        sha256: str,
        size_bytes: int,
        sensitivity: Sensitivity,
        retention_class: str,
        expires_in: int,
    ) -> PresignedUpload:
        """Mint a presigned PUT that signs the checksum + object metadata into the URL.

        The agent must send the returned ``required_headers`` (the signed
        ``x-amz-checksum-sha256`` and ``x-amz-meta-*`` metadata); S3 rejects a PUT whose
        checksum disagrees with the signed value, and the metadata lands on the object so
        the later install fetch (`get_artifact`) reads its sensitivity. ``size_bytes`` is
        recorded by the caller's manifest and capped before this is called; presigned-PUT
        length enforcement is asserted by the `live_stack` test (ADR-0048 §2).

        Raises:
            CategorizedError: presigning fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        metadata = {"sensitivity": sensitivity.value, "retention-class": retention_class}
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ChecksumSHA256": sha256,
                    "Metadata": metadata,
                },
                ExpiresIn=expires_in,
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("presign_put", key, err) from err
        headers = {
            "x-amz-checksum-sha256": sha256,
            "x-amz-meta-sensitivity": sensitivity.value,
            "x-amz-meta-retention-class": retention_class,
        }
        return PresignedUpload(url=url, required_headers=headers)
```

Add the import: `Sensitivity` is already imported from `kdive.domain.models`. Good.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_objectstore_ingestion.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
uv run ruff check src/kdive/store/objectstore.py tests/store/test_objectstore_ingestion.py
uv run ty check
git add src/kdive/store/objectstore.py tests/store/test_objectstore_ingestion.py
git commit -m "feat(store): add get_range, presign_put, and public key helpers"
```

---

### Task 3: Object store — `list_prefix` + `delete` (reaper primitives)

**Files:**
- Modify: `src/kdive/store/objectstore.py`
- Test: `tests/store/test_objectstore_ingestion.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
class _ListClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self._pages = pages
        self.deleted: list[str] = []

    def get_paginator(self, op: str) -> object:
        assert op == "list_objects_v2"
        pages = self._pages

        class _Paginator:
            def paginate(self, **_kwargs: object):
                yield from pages

        return _Paginator()

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.deleted.append(str(kwargs["Key"]))
        return {}


def test_list_prefix_flattens_pages() -> None:
    client = _ListClient(
        [
            {"Contents": [{"Key": "p/a"}, {"Key": "p/b"}]},
            {"Contents": [{"Key": "p/c"}]},
            {},  # empty page (no Contents) tolerated
        ]
    )
    store = ObjectStore(client, "bucket")
    assert store.list_prefix("p/") == ["p/a", "p/b", "p/c"]


def test_delete_calls_delete_object() -> None:
    client = _ListClient([])
    store = ObjectStore(client, "bucket")
    store.delete("p/a")
    assert client.deleted == ["p/a"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/store/test_objectstore_ingestion.py -k "list_prefix or delete_calls" -v`
Expected: FAIL (`AttributeError: 'ObjectStore' object has no attribute 'list_prefix'`).

- [ ] **Step 3: Implement**

Add to `ObjectStore`:

```python
    def list_prefix(self, prefix: str) -> list[str]:
        """Return every object key under ``prefix`` (paginated), or ``[]``.

        Raises:
            CategorizedError: the listing fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        keys: list[str] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("list_objects_v2", prefix, err) from err
        return keys

    def delete(self, key: str) -> None:
        """Delete ``key`` (idempotent — deleting an absent key is not an error).

        Raises:
            CategorizedError: the delete fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("delete_object", key, err) from err
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/store/test_objectstore_ingestion.py -v`
Expected: PASS (all ingestion store tests).

- [ ] **Step 5: Lint, type, commit**

```bash
uv run ruff check src/kdive/store/objectstore.py tests/store/test_objectstore_ingestion.py
uv run ty check
git add src/kdive/store/objectstore.py tests/store/test_objectstore_ingestion.py
git commit -m "feat(store): add list_prefix and delete for the upload reaper"
```

---

### Task 4: `BuildProfile` source discrimination

**Files:**
- Modify: `src/kdive/profiles/build.py`, `src/kdive/providers/local_libvirt/build.py`, `src/kdive/mcp/tools/runs.py`
- Test: `tests/profiles/test_build_profile_source.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/profiles/test_build_profile_source.py`:

```python
"""Source-discriminated build profile (ADR-0048 §2)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import (
    BuildProfile,
    ExternalBuildProfile,
    ServerBuildProfile,
)


def test_parse_defaults_to_server_and_preserves_existing_documents() -> None:
    parsed = BuildProfile.parse(
        {"schema_version": 1, "kernel_source_ref": "git#v6.9", "config_ref": "cfg"}
    )
    assert isinstance(parsed, ServerBuildProfile)
    assert parsed.source == "server"
    assert parsed.config_ref == "cfg"


def test_parse_external_requires_no_source_tree_fields() -> None:
    parsed = BuildProfile.parse({"schema_version": 1, "source": "external"})
    assert isinstance(parsed, ExternalBuildProfile)
    assert parsed.source == "external"


def test_external_profile_rejects_server_fields() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse(
            {"schema_version": 1, "source": "external", "config_ref": "cfg"}
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_server_profile_still_requires_config_ref() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse({"schema_version": 1, "kernel_source_ref": "git#v6.9"})
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_unknown_source_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse({"schema_version": 1, "source": "bogus"})
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/profiles/test_build_profile_source.py -v`
Expected: FAIL (`ImportError: cannot import name 'ExternalBuildProfile'`).

- [ ] **Step 3: Rewrite the profile module**

Replace the `BuildProfile` class in `src/kdive/profiles/build.py` with the discriminated variants plus a `parse` dispatcher. Keep `NonEmptyStr`, the `_reject_coerced_version` validator (shared via a base), and the redaction-preserving error mapping. New body (from the imports down, replacing the `BuildProfile` class):

```python
from typing import Annotated, Literal  # already imported


class _BuildProfileBase(BaseModel):
    """Shared config + version guard for both build-lane profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _reject_coerced_version(cls, value: object) -> object:
        """Reject a non-``int`` version before ``Literal`` coercion accepts it."""
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("schema_version must be an integer")
        return value


class ServerBuildProfile(_BuildProfileBase):
    """Server-build lane: names a source tree, a config, and an optional patch."""

    source: Literal["server"] = "server"
    kernel_source_ref: NonEmptyStr
    config_ref: NonEmptyStr
    patch_ref: NonEmptyStr | None = None


class ExternalBuildProfile(_BuildProfileBase):
    """External-build lane: the discriminator alone — no source-tree fields."""

    source: Literal["external"]


type ParsedBuildProfile = ServerBuildProfile | ExternalBuildProfile


class BuildProfile:
    """Parse boundary that dispatches a build-profile document on ``source``."""

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> ParsedBuildProfile:
        """Validate a build-profile document, mapping any failure to ``configuration_error``.

        Dispatches on ``source`` (default ``"server"``, so existing server documents
        without the field still parse). The error details carry field locations but never
        the submitted values (redaction guarantee, ADR-0029).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for any structural failure,
                including an unknown ``source``.
        """
        source = data.get("source", "server") if isinstance(data, Mapping) else None
        model: type[ParsedBuildProfile]
        if source == "server":
            model = ServerBuildProfile
        elif source == "external":
            model = ExternalBuildProfile
        else:
            raise CategorizedError(
                "invalid build profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"errors": [{"loc": ["source"], "msg": "unknown build source"}]},
            )
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise CategorizedError(
                "invalid build profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "errors": exc.errors(
                        include_url=False, include_input=False, include_context=False
                    )
                },
            ) from exc
```

Keep the module docstring; update its first paragraph to mention the two source variants.

- [ ] **Step 4: Update the builder + handler to the narrowed type**

In `src/kdive/providers/local_libvirt/build.py`:
- Change the `Builder` Protocol and `LocalLibvirtBuild.build` signatures from `profile: BuildProfile` to `profile: ServerBuildProfile`.
- Update the import: `from kdive.profiles.build import ServerBuildProfile` (drop `BuildProfile` if now unused there).

In `src/kdive/mcp/tools/runs.py`, `build_handler` (the server-lane job handler) parses then narrows:

```python
    parsed = BuildProfile.parse(run.build_profile)
    if not isinstance(parsed, ServerBuildProfile):
        # The runs.build gate (Task 8) prevents an external Run from enqueueing a build
        # job; this is the defensive backstop if one ever reaches the handler.
        raise CategorizedError(
            "external-source run reached the server build handler",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    profile = parsed
```

Add `ServerBuildProfile` to the `runs.py` import from `kdive.providers.local_libvirt.build` (or from `kdive.profiles.build`). It's defined in profiles; import `from kdive.profiles.build import BuildProfile, ServerBuildProfile`.

*(The `runs.build` external-source rejection itself is added in Task 8 — keep this task to the type plumbing so the suite stays green.)*

- [ ] **Step 5: Run, lint, type, commit**

```bash
uv run pytest tests/profiles/test_build_profile_source.py tests/mcp/test_runs_tools.py -v
uv run ruff check src/kdive/profiles/build.py src/kdive/providers/local_libvirt/build.py src/kdive/mcp/tools/runs.py
uv run ty check
```
Expected: all PASS (existing `test_runs_tools.py` still green — server profiles default to `source="server"`).

```bash
git add src/kdive/profiles/build.py src/kdive/providers/local_libvirt/build.py src/kdive/mcp/tools/runs.py tests/profiles/test_build_profile_source.py
git commit -m "feat(profiles): make BuildProfile source-discriminated (server/external)"
```

---

### Task 5: Upload-manifest storage (migration + module)

**Files:**
- Create: `src/kdive/db/schema/0006_upload_manifests.sql`, `src/kdive/db/upload_manifest.py`
- Test: `tests/db/test_upload_manifest.py`

- [ ] **Step 1: Write the migration**

Create `src/kdive/db/schema/0006_upload_manifests.sql`:

```sql
-- 0006_upload_manifests.sql — owner-scoped upload manifests for external ingestion
-- (ADR-0048 §4/§6). Additive, forward-only (ADR-0015). One row per in-flight upload
-- owner (a CREATED Run or a DEFINED System); holds the declared (name, sha256,
-- size_bytes) set complete_build compares stored objects against, the object-key prefix
-- the reaper lists, and the deadline the reaper keys off. The row is replaced on a
-- re-mint (one call, full set) and deleted when the owner finalizes or is reaped. It is
-- NOT the write-once artifacts row — no artifacts-row state changes.
CREATE TABLE upload_manifests (
    owner_kind text NOT NULL CONSTRAINT upload_manifests_owner_kind_check
                   CHECK (owner_kind IN ('runs', 'systems')),
    owner_id   uuid NOT NULL,
    prefix     text NOT NULL,
    manifest   jsonb NOT NULL,
    deadline   timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT upload_manifests_pkey PRIMARY KEY (owner_kind, owner_id)
);
CREATE TRIGGER upload_manifests_set_updated_at BEFORE UPDATE ON upload_manifests
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
```

- [ ] **Step 2: Write the failing tests**

Create `tests/db/test_upload_manifest.py` (mirror `tests/db/` conftest fixtures — uses `migrated_url`/`pg_conn`; confirm the fixture names in `tests/db/conftest.py` and reuse them):

```python
"""Owner-scoped upload-manifest storage (ADR-0048 §4)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from kdive.db.upload_manifest import (
    ManifestEntry,
    delete_manifest,
    get_manifest,
    replace_manifest,
)

pytestmark = pytest.mark.anyio  # match the db tests' async marker; see tests/db/conftest.py


async def test_replace_then_get_round_trips(pg_conn) -> None:
    owner_id = uuid4()
    entries = [ManifestEntry("kernel", "Zm9v", 10), ManifestEntry("vmlinux", "YmFy", 20)]
    await replace_manifest(
        pg_conn,
        owner_kind="runs",
        owner_id=owner_id,
        prefix=f"local/runs/{owner_id}/",
        entries=entries,
        ttl=timedelta(hours=1),
    )
    got = await get_manifest(pg_conn, "runs", owner_id)
    assert got is not None
    assert got.entries == tuple(entries)
    assert got.prefix == f"local/runs/{owner_id}/"
    assert got.deadline is not None


async def test_replace_is_full_set_replacement(pg_conn) -> None:
    owner_id = uuid4()
    await replace_manifest(
        pg_conn, owner_kind="runs", owner_id=owner_id,
        prefix=f"local/runs/{owner_id}/",
        entries=[ManifestEntry("kernel", "a", 1), ManifestEntry("vmlinux", "b", 2)],
        ttl=timedelta(hours=1),
    )
    await replace_manifest(
        pg_conn, owner_kind="runs", owner_id=owner_id,
        prefix=f"local/runs/{owner_id}/",
        entries=[ManifestEntry("kernel", "a", 1)],
        ttl=timedelta(hours=1),
    )
    got = await get_manifest(pg_conn, "runs", owner_id)
    assert got is not None and [e.name for e in got.entries] == ["kernel"]


async def test_get_absent_returns_none(pg_conn) -> None:
    assert await get_manifest(pg_conn, "runs", uuid4()) is None


async def test_delete_removes_row(pg_conn) -> None:
    owner_id = uuid4()
    await replace_manifest(
        pg_conn, owner_kind="runs", owner_id=owner_id,
        prefix=f"local/runs/{owner_id}/",
        entries=[ManifestEntry("kernel", "a", 1)], ttl=timedelta(hours=1),
    )
    await delete_manifest(pg_conn, "runs", owner_id)
    assert await get_manifest(pg_conn, "runs", owner_id) is None
```

*Before writing: open `tests/db/conftest.py` and use its actual connection fixture name + async marker (the snippet assumes `pg_conn` + `pytest.mark.anyio`; adjust to match).*

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/db/test_upload_manifest.py -v`
Expected: FAIL (`ModuleNotFoundError: kdive.db.upload_manifest`). (If Docker/Postgres is unavailable the db tests skip — run on a host with the db fixtures, as the existing `tests/db` suite requires.)

- [ ] **Step 4: Implement the module**

Create `src/kdive/db/upload_manifest.py`:

```python
"""Owner-scoped upload-manifest storage for external-build ingestion (ADR-0048 §4).

A manifest is the declared ``(name, sha256, size_bytes)`` set an agent commits at
``artifacts.create_upload`` for one owner (a CREATED Run or a DEFINED System), plus the
object-key ``prefix`` the reaper lists and the ``deadline`` it keys off. It is replaced
wholesale on a re-mint (one call, full set) and deleted when the owner finalizes or is
reaped. It is not the write-once ``artifacts`` row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class ManifestEntry(NamedTuple):
    """One declared artifact: its name, base64 SHA-256, and byte size."""

    name: str
    sha256: str
    size_bytes: int


class UploadManifest(NamedTuple):
    """A persisted manifest: the declared entries, the key prefix, and the deadline."""

    entries: tuple[ManifestEntry, ...]
    prefix: str
    deadline: datetime


async def replace_manifest(
    conn: AsyncConnection,
    *,
    owner_kind: str,
    owner_id: UUID,
    prefix: str,
    entries: Sequence[ManifestEntry],
    ttl: timedelta,
) -> None:
    """Upsert the owner's manifest, stamping ``deadline = now() + ttl`` in Postgres.

    Full-set replace: a re-mint overwrites the prior manifest, prefix, and deadline.
    """
    payload = [{"name": e.name, "sha256": e.sha256, "size_bytes": e.size_bytes} for e in entries]
    await conn.execute(
        "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
        "VALUES (%s, %s, %s, %s, now() + %s) "
        "ON CONFLICT (owner_kind, owner_id) DO UPDATE SET "
        "  prefix = EXCLUDED.prefix, manifest = EXCLUDED.manifest, deadline = EXCLUDED.deadline",
        (owner_kind, owner_id, prefix, Jsonb(payload), ttl),
    )


async def get_manifest(
    conn: AsyncConnection, owner_kind: str, owner_id: UUID
) -> UploadManifest | None:
    """Return the owner's manifest, or ``None`` if none is recorded."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT prefix, manifest, deadline FROM upload_manifests "
            "WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    entries = tuple(
        ManifestEntry(e["name"], e["sha256"], int(e["size_bytes"])) for e in row["manifest"]
    )
    return UploadManifest(entries=entries, prefix=row["prefix"], deadline=row["deadline"])


async def delete_manifest(conn: AsyncConnection, owner_kind: str, owner_id: UUID) -> None:
    """Delete the owner's manifest row (idempotent — absent is fine)."""
    await conn.execute(
        "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
        (owner_kind, owner_id),
    )
```

- [ ] **Step 5: Run, lint, type, commit**

```bash
uv run pytest tests/db/test_upload_manifest.py -v
uv run ruff check src/kdive/db/upload_manifest.py tests/db/test_upload_manifest.py
uv run ty check
git add src/kdive/db/schema/0006_upload_manifests.sql src/kdive/db/upload_manifest.py tests/db/test_upload_manifest.py
git commit -m "feat(db): add owner-scoped upload-manifest storage + migration"
```

---

### Task 6: `artifacts.create_upload` tool

**Files:**
- Modify: `src/kdive/mcp/tools/artifacts.py`
- Test: `tests/mcp/test_create_upload_tool.py`

This tool mints one presigned PUT per declared artifact and persists the manifest. It returns `list[ToolResponse]` (one per artifact) — the rigid `ToolResponse` envelope has no list field, so this follows the `artifacts.list` precedent rather than the spec's single-envelope `uploads[]` sketch. Each envelope: `object_id` = object key, `refs={"upload_url": ...}`, `data` = `{name, expires_in, x-amz-checksum-sha256, x-amz-meta-sensitivity, x-amz-meta-retention-class}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_create_upload_tool.py`:

```python
"""artifacts.create_upload — presign + manifest persistence (ADR-0048 §4)."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import artifacts as artifacts_tools
from kdive.security.rbac import Role
from kdive.store.objectstore import PresignedUpload

_DT = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeStore:
    """A presign-only store fake; records the keys + sizes it was asked to sign."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def presign_put(self, key, *, sha256, size_bytes, sensitivity, retention_class, expires_in):
        self.calls.append((key, sha256, size_bytes))
        assert sensitivity is Sensitivity.SENSITIVE and retention_class == "build"
        return PresignedUpload(
            url=f"https://store/{key}",
            required_headers={"x-amz-checksum-sha256": sha256},
        )


def _ctx(role: Role | None = Role.OPERATOR) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u1", agent_session="s", projects=("proj",), roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_external_run(pool: AsyncConnectionPool) -> UUID:
    """Insert a CREATED Run with an external build profile; return its id.

    Reuse the seeding helpers in tests/mcp/_seed.py if present; otherwise insert the
    Resource → Allocation(active) → System(ready) → Investigation → Run chain directly,
    as tests/mcp/test_runs_tools.py does.
    """
    ...  # see test_runs_tools.py for the exact insert chain; Run.build_profile = {"schema_version":1,"source":"external"}


async def test_create_upload_mints_presigned_puts_and_persists_manifest(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run(pool)
        store = _FakeStore()
        responses = await artifacts_tools.create_upload(
            pool,
            _ctx(),
            owner_kind="run",
            owner_id=str(run_id),
            artifacts=[
                {"name": "kernel", "sha256": "aaa", "size_bytes": 100},
                {"name": "vmlinux", "sha256": "bbb", "size_bytes": 200},
            ],
            store=store,
        )
    assert [r.object_id for r in responses] == [
        f"local/runs/{run_id}/kernel",
        f"local/runs/{run_id}/vmlinux",
    ]
    assert responses[0].refs["upload_url"].startswith("https://store/")
    assert responses[0].suggested_next_actions == ["runs.complete_build"]
    assert {c[0] for c in store.calls} == {
        f"local/runs/{run_id}/kernel",
        f"local/runs/{run_id}/vmlinux",
    }
```

Add focused failure-path tests (complete bodies):

```python
async def test_create_upload_rejects_non_external_run(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        # Seed a CREATED run whose profile is source="server".
        run_id = ...  # seed with build_profile={"schema_version":1,"kernel_source_ref":"x","config_ref":"c"}
        out = await artifacts_tools.create_upload(
            pool, _ctx(), owner_kind="run", owner_id=str(run_id),
            artifacts=[{"name": "kernel", "sha256": "a", "size_bytes": 1}], store=_FakeStore(),
        )
    assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value


async def test_create_upload_rejects_unknown_artifact_name_for_run(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run(pool)
        out = await artifacts_tools.create_upload(
            pool, _ctx(), owner_kind="run", owner_id=str(run_id),
            artifacts=[{"name": "rootfs", "sha256": "a", "size_bytes": 1}], store=_FakeStore(),
        )
    assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value


async def test_create_upload_rejects_oversize_before_minting(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run(pool)
        store = _FakeStore()
        out = await artifacts_tools.create_upload(
            pool, _ctx(), owner_kind="run", owner_id=str(run_id),
            artifacts=[{"name": "kernel", "sha256": "a", "size_bytes": 10**13}], store=store,
        )
    assert out[0].error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert store.calls == []  # no URL minted


async def test_create_upload_requires_operator(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run(pool)
        with pytest.raises(Exception):  # AuthorizationError from require_role
            await artifacts_tools.create_upload(
                pool, _ctx(role=Role.VIEWER), owner_kind="run", owner_id=str(run_id),
                artifacts=[{"name": "kernel", "sha256": "a", "size_bytes": 1}], store=_FakeStore(),
            )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcp/test_create_upload_tool.py -v`
Expected: FAIL (`AttributeError: module ... has no attribute 'create_upload'`).

- [ ] **Step 3: Implement `create_upload`**

In `src/kdive/mcp/tools/artifacts.py`, add the constants, the core function, and registration. Add imports at top:

```python
import os
from datetime import timedelta
from typing import Any

from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.models import Sensitivity
from kdive.mcp.tools import _docmeta
from kdive.security.rbac import Role, require_role
from kdive.store.objectstore import ObjectStore, artifact_key, object_store_from_env, owner_prefix
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.domain.state import RunState, SystemState
from kdive.db.repositories import RUNS, SYSTEMS
```

Add constants + env helpers:

```python
_TENANT = "local"
_BUILD_ARTIFACT_NAMES = frozenset({"kernel", "initrd", "vmlinux"})
_ROOTFS_NAME = "rootfs"
_RETENTION_CLASS = "build"
_DEFAULT_UPLOAD_TTL_SECONDS = 86400
_DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024


def _upload_ttl() -> timedelta:
    return timedelta(seconds=int(os.environ.get("KDIVE_UPLOAD_TTL_SECONDS", _DEFAULT_UPLOAD_TTL_SECONDS)))


def _max_upload_bytes() -> int:
    return int(os.environ.get("KDIVE_MAX_UPLOAD_BYTES", _DEFAULT_MAX_UPLOAD_BYTES))


def _presign_ttl_seconds() -> int:
    # Presigned URLs are short-TTL; bounded by the upload deadline but capped at 1h.
    return min(3600, int(_upload_ttl().total_seconds()))
```

Define a minimal store Protocol for injection + the core function:

```python
class _PresignStore(Protocol):
    def presign_put(self, key, *, sha256, size_bytes, sensitivity, retention_class, expires_in): ...


def _allowed_names(owner_kind: str) -> frozenset[str]:
    return _BUILD_ARTIFACT_NAMES if owner_kind == "run" else frozenset({_ROOTFS_NAME})


async def _owner_accepts_upload(conn, owner_kind: str, owner_id: UUID) -> bool:
    """True iff the owner is in its pre-upload state (CREATED external Run / DEFINED System)."""
    if owner_kind == "run":
        run = await RUNS.get(conn, owner_id)
        if run is None or run.state is not RunState.CREATED:
            return False
        parsed = BuildProfile.parse(run.build_profile)
        return isinstance(parsed, ExternalBuildProfile)
    system = await SYSTEMS.get(conn, owner_id)
    return system is not None and system.state is SystemState.DEFINED


async def create_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    owner_kind: str,
    owner_id: str,
    artifacts: list[dict[str, Any]],
    store: _PresignStore | None = None,
) -> list[ToolResponse]:
    """Mint a presigned PUT per declared artifact and persist the owner's manifest.

    Replaces the owner's manifest with the declared set (one call, full set). Returns one
    envelope per artifact; an error returns a single failure envelope. Requires operator.
    """
    store = store or object_store_from_env()
    uid = _as_uuid(owner_id)
    if uid is None or owner_kind not in ("run", "system"):
        return [_config_error(owner_id)]
    kind = "runs" if owner_kind == "run" else "systems"
    next_action = "runs.complete_build" if owner_kind == "run" else "systems.provision"

    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            project = await _owner_project(conn, kind, uid)
            if project is None or project not in ctx.projects:
                return [_config_error(owner_id)]
            require_role(ctx, project, Role.OPERATOR)

            allowed = _allowed_names(owner_kind)
            cap = _max_upload_bytes()
            entries: list[ManifestEntry] = []
            for art in artifacts:
                name, sha256, size = art.get("name"), art.get("sha256"), art.get("size_bytes")
                if name not in allowed or not isinstance(sha256, str) or not isinstance(size, int):
                    return [_config_error(owner_id, data={"reason": "bad_artifact_declaration"})]
                if size <= 0 or size > cap:
                    return [_config_error(owner_id, data={"reason": "size_out_of_range"})]
                entries.append(ManifestEntry(name=name, sha256=sha256, size_bytes=size))
            if not entries:
                return [_config_error(owner_id, data={"reason": "no_artifacts_declared"})]

            prefix = owner_prefix(_TENANT, kind, str(uid))
            lock_scope = LockScope.RUN if owner_kind == "run" else LockScope.SYSTEM
            # Take the per-owner lock and re-check state INSIDE it before minting + persisting,
            # so a concurrent complete_build/provision that finalizes the owner cannot interleave
            # between the check and the manifest write — which would otherwise strand a manifest
            # (and its objects) the reaper, scoped to pre-finalize owners, never sweeps.
            try:
                async with conn.transaction(), advisory_xact_lock(conn, lock_scope, uid):
                    if not await _owner_accepts_upload(conn, owner_kind, uid):
                        return [_config_error(owner_id, data={"reason": "owner_not_accepting_upload"})]
                    uploads = [
                        (
                            e,
                            artifact_key(_TENANT, kind, str(uid), e.name),
                            store.presign_put(
                                artifact_key(_TENANT, kind, str(uid), e.name),
                                sha256=e.sha256,
                                size_bytes=e.size_bytes,
                                sensitivity=Sensitivity.SENSITIVE,
                                retention_class=_RETENTION_CLASS,
                                expires_in=_presign_ttl_seconds(),
                            ),
                        )
                        for e in entries
                    ]
                    await upload_manifest.replace_manifest(
                        conn, owner_kind=kind, owner_id=uid, prefix=prefix,
                        entries=entries, ttl=_upload_ttl(),
                    )
            except CategorizedError as exc:  # presign failure rolls back the manifest write
                return [ToolResponse.failure(owner_id, exc.category)]

    return [
        ToolResponse.success(
            key,
            "upload_ready",
            suggested_next_actions=[next_action],
            refs={"upload_url": presigned.url},
            data={"name": entry.name, "expires_in": str(_presign_ttl_seconds()), **presigned.required_headers},
        )
        for entry, key, presigned in uploads
    ]
```

Add the `_owner_project` helper (reuses the System/Run → project resolution):

```python
async def _owner_project(conn, kind: str, owner_id: UUID) -> str | None:
    table = "runs" if kind == "runs" else "systems"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT project FROM {table} WHERE id = %s", (owner_id,))  # noqa: S608 - table from a 2-value whitelist
        row = await cur.fetchone()
    return row["project"] if row else None
```

Update `_config_error` to accept `data` (mirror runs.py): `def _config_error(object_id, *, data=None): return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})`. Import `Protocol`, `UUID`, `timedelta`, `AsyncConnectionPool`, `RequestContext`, `bind_context` as needed (some already present).

Register the tool in `register(app, pool)` (after the existing `artifacts.get` tool):

```python
    @app.tool(
        name="artifacts.create_upload",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_create_upload_tool(
        owner_kind: Annotated[str, Field(description="'run' (build artifacts) or 'system' (rootfs).")],
        owner_id: Annotated[str, Field(description="The owning Run or System id.")],
        artifacts: Annotated[
            list[dict[str, Any]],
            Field(description="Declared artifacts: [{name, sha256 (base64), size_bytes}]."),
        ],
    ) -> list[ToolResponse]:
        """Mint presigned PUTs for an owner's declared artifacts. Requires operator."""
        return await create_upload(
            pool, current_context(), owner_kind=owner_kind, owner_id=owner_id, artifacts=artifacts
        )
```

*(`artifacts.register` is already in `_PLANE_REGISTRARS` — no `app.py` edit needed.)*

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/mcp/test_create_upload_tool.py -v`
Expected: PASS. (DB-backed; needs the `migrated_url` fixture / Postgres.)

- [ ] **Step 5: Lint, type, commit**

```bash
uv run ruff check src/kdive/mcp/tools/artifacts.py tests/mcp/test_create_upload_tool.py
uv run ty check
git add src/kdive/mcp/tools/artifacts.py tests/mcp/test_create_upload_tool.py
git commit -m "feat(artifacts): add create_upload presign + manifest tool"
```

---

### Task 7: `validate_external_artifacts` (provider)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Test: `tests/providers/local_libvirt/test_validate_external_artifacts.py`

Validates required-set, existence+size, integrity vs manifest, shape magic, and ranged `build_id` — returns a `BuildOutput`. Uses the injected store's `head`/`get_range`.

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/local_libvirt/test_validate_external_artifacts.py`:

```python
"""External-artifact validation (ADR-0048 §5)."""

from __future__ import annotations

import struct

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.db.upload_manifest import ManifestEntry
from kdive.providers.local_libvirt.build import validate_external_artifacts
from kdive.store.objectstore import HeadResult

_BZIMAGE_HEAD = b"\x00" * 0x202 + b"HdrS"  # bzImage magic at offset 0x202


def _elf_with_build_id(build_id: bytes) -> bytes:
    """Build a minimal ELF64-LE blob carrying a .note.gnu.build-id section.

    Layout: ELF header → one PT-less section header table with .shstrtab + the note
    section → the note bytes. Kept just complete enough for extract_build_id_ranged.
    """
    ...  # construct: e_ident, e_shoff, e_shentsize=64, e_shnum, section headers, note payload


class _FakeStore:
    def __init__(self, blobs: dict[str, bytes], heads: dict[str, HeadResult]) -> None:
        self._blobs = blobs
        self._heads = heads

    def head(self, key: str) -> HeadResult | None:
        return self._heads.get(key)

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        return self._blobs[key][start : start + length]


def test_missing_kernel_is_configuration_error() -> None:
    store = _FakeStore({}, {})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(store, manifest=[], keys={}, declared_build_id=None)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_missing_object_is_configuration_error() -> None:
    store = _FakeStore({}, {})  # manifest declares kernel but HEAD misses
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "csum", 6)],
            keys={"kernel": "k"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_checksum_mismatch_is_build_failure() -> None:
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD}, {"k": HeadResult(size_bytes=len(_BZIMAGE_HEAD), checksum_sha256="OTHER", etag="e")}
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store, manifest=[ManifestEntry("kernel", "csum", len(_BZIMAGE_HEAD))],
            keys={"kernel": "k"}, declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_bad_kernel_magic_is_build_failure() -> None:
    bad = b"\x00" * 0x300
    store = _FakeStore({"k": bad}, {"k": HeadResult(len(bad), "csum", "e")})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store, manifest=[ManifestEntry("kernel", "csum", len(bad))],
            keys={"kernel": "k"}, declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_happy_path_kernel_only_returns_build_output() -> None:
    store = _FakeStore({"k": _BZIMAGE_HEAD}, {"k": HeadResult(len(_BZIMAGE_HEAD), "csum", "e")})
    out = validate_external_artifacts(
        store, manifest=[ManifestEntry("kernel", "csum", len(_BZIMAGE_HEAD))],
        keys={"kernel": "k"}, declared_build_id=None,
    )
    assert out.output.kernel_ref == "k" and out.output.debuginfo_ref == "" and out.output.build_id == ""
    assert set(out.heads) == {"kernel"}


def test_build_id_mismatch_is_build_failure() -> None:
    blob = _elf_with_build_id(bytes.fromhex("dead"))
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "v": blob},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)), ManifestEntry("vmlinux", "cv", len(blob))],
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id="beef",  # != dead
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_vmlinux_without_declared_build_id_is_configuration_error() -> None:
    blob = _elf_with_build_id(bytes.fromhex("dead"))
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "v": blob},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)), ManifestEntry("vmlinux", "cv", len(blob))],
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_matching_build_id_passes_and_pairs_vmlinux() -> None:
    """The anti-mispairing guard's success path: a correct build_id is accepted."""
    blob = _elf_with_build_id(bytes.fromhex("deadbeef"))
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "v": blob},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
            ManifestEntry("vmlinux", "cv", len(blob)),
        ],
        keys={"kernel": "k", "vmlinux": "v"},
        declared_build_id="DEADBEEF",  # case-insensitive vs the lowercase-hex note
    )
    assert out.output.kernel_ref == "k" and out.output.debuginfo_ref == "v"
    assert out.output.build_id == "deadbeef"  # normalized to the value parse_gnu_build_id returns


def test_initrd_is_validated_and_returned_in_keys() -> None:
    """An uploaded initrd is HEAD/checksum-validated (magic-exempt) and survives validation."""
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "i": b"\x1f\x8b" + b"\x00" * 40},  # initrd: no magic check
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "i": HeadResult(42, "ci", "e")},
    )
    out = validate_external_artifacts(
        store,
        manifest=[ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)), ManifestEntry("initrd", "ci", 42)],
        keys={"kernel": "k", "initrd": "i"},
        declared_build_id=None,
    )
    assert out.output.kernel_ref == "k"  # initrd validated without error
    assert set(out.heads) == {"kernel", "initrd"}  # both heads returned for the finalize rows
```

*Implementer note: finish `_elf_with_build_id` so it round-trips through `extract_build_id_ranged` (Step 3). Use `struct` to write a valid ELF64-LE header (`e_shoff`, `e_shentsize=64`, `e_shnum`), a `.shstrtab` section, and a `SHT_NOTE` section named `.note.gnu.build-id` whose payload is `namesz=4, descsz=len(id), type=3, "GNU\\0", <id>`.*

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_validate_external_artifacts.py -v`
Expected: FAIL (`ImportError: cannot import name 'validate_external_artifacts'`).

- [ ] **Step 3: Implement validator + ranged build-id**

In `src/kdive/providers/local_libvirt/build.py`, add a store Protocol and the functions. Add imports: `import struct`, `from collections.abc import Mapping, Sequence`, `from kdive.db.upload_manifest import ManifestEntry`, `from kdive.store.objectstore import HeadResult`.

```python
_ELF_MAGIC = b"\x7fELF"
_BZIMAGE_MAGIC = b"HdrS"
_BZIMAGE_MAGIC_OFFSET = 0x202
_SHT_NOTE = 7


class _ValidatorStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...


class ValidatedUpload(NamedTuple):
    """Validation result: the recorded ``BuildOutput`` plus the per-name ``HeadResult``s.

    The heads (etag/size/checksum per uploaded object) are returned so the finalize step
    writes the write-once ``artifacts`` rows from this one validation pass — no second
    HEAD, and no second object-store handle — which keeps ``complete_build`` injectable.
    """

    output: BuildOutput
    heads: dict[str, HeadResult]


def _build_failure(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.BUILD_FAILURE, details=details)


def validate_external_artifacts(
    store: _ValidatorStore,
    *,
    manifest: Sequence[ManifestEntry],
    keys: Mapping[str, str],
    declared_build_id: str | None,
) -> ValidatedUpload:
    """Validate uploaded build artifacts; return the ``BuildOutput`` plus per-name heads.

    Order (ADR-0048 §5): require ``kernel``; then per declared artifact HEAD existence +
    size, checksum vs the manifest, and leading-byte magic; then, if a ``vmlinux`` is
    present, verify the declared ``build_id`` against its ranged ``.note.gnu.build-id``.

    ``declared_build_id`` is the GNU build-id as hex (the value ``parse_gnu_build_id``
    yields); the comparison is case-insensitive and the returned ``BuildOutput.build_id``
    is the normalized lowercase-hex value extracted from the file, not the raw input.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (a missing/skipped upload, or a vmlinux
            with no declared build_id); ``BUILD_FAILURE`` (checksum/size/magic/build_id
            defect); ``INFRASTRUCTURE_FAILURE`` propagated from the store.
    """
    by_name = {e.name: e for e in manifest}
    if "kernel" not in by_name or "kernel" not in keys:
        raise CategorizedError(
            "external build is missing the required kernel artifact",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    heads: dict[str, HeadResult] = {}
    for name, entry in by_name.items():
        key = keys[name]
        head = store.head(key)
        if head is None:
            raise CategorizedError(
                f"declared artifact {name!r} was never uploaded",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"name": name},
            )
        if head.size_bytes != entry.size_bytes or head.checksum_sha256 != entry.sha256:
            raise _build_failure("uploaded artifact disagrees with its manifest", name=name)
        _check_magic(store, name, key)
        heads[name] = head

    build_id = ""
    if "vmlinux" in by_name:
        if not declared_build_id:
            raise CategorizedError(
                "a vmlinux upload requires a declared build_id",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        actual = extract_build_id_ranged(store, keys["vmlinux"])  # lowercase hex
        if actual != declared_build_id.lower():
            raise _build_failure("declared build_id does not match the uploaded vmlinux")
        build_id = actual  # record the normalized lowercase-hex value, not the raw input

    output = BuildOutput(
        kernel_ref=keys["kernel"],
        debuginfo_ref=keys.get("vmlinux", ""),
        build_id=build_id,
    )
    return ValidatedUpload(output=output, heads=heads)


def _check_magic(store: _ValidatorStore, name: str, key: str) -> None:
    if name == "vmlinux":
        if store.get_range(key, start=0, length=4) != _ELF_MAGIC:
            raise _build_failure("vmlinux is not an ELF file", name=name)
    elif name == "kernel":
        magic = store.get_range(key, start=_BZIMAGE_MAGIC_OFFSET, length=4)
        if magic != _BZIMAGE_MAGIC:
            raise _build_failure("kernel is not a bzImage", name=name)
    # initrd has no cheap universal magic; checksum + size already gate it.


def extract_build_id_ranged(store: _ValidatorStore, key: str) -> str:
    """Extract a vmlinux's GNU build-id via ranged ELF64-LE reads (no full download).

    Reads the ELF header (``e_shoff``/``e_shentsize``/``e_shnum``/``e_shstrndx``), the
    section header table, the section-name string table, and the ``.note.gnu.build-id``
    section bytes — then feeds them to :func:`parse_gnu_build_id`.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if the ELF is malformed or carries no build-id.
    """
    header = store.get_range(key, start=0, length=64)
    if header[:4] != _ELF_MAGIC or header[4] != 2 or header[5] != 1:  # ELFCLASS64, ELFDATA2LSB
        raise _build_failure("vmlinux is not a 64-bit little-endian ELF")
    e_shoff = struct.unpack_from("<Q", header, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", header, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", header, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", header, 0x3E)[0]
    if e_shoff == 0 or e_shnum == 0 or e_shentsize < 64:
        raise _build_failure("vmlinux has no usable section header table")
    sht = store.get_range(key, start=e_shoff, length=e_shentsize * e_shnum)
    shstr = _read_section(store, sht, e_shentsize, e_shstrndx)
    for i in range(e_shnum):
        off = i * e_shentsize
        sh_name = struct.unpack_from("<I", sht, off)[0]
        sh_type = struct.unpack_from("<I", sht, off + 4)[0]
        if sh_type != _SHT_NOTE:
            continue
        name = shstr[sh_name : shstr.index(b"\x00", sh_name)]
        if name == b".note.gnu.build-id":
            notes = _read_section(store, sht, e_shentsize, i)
            return parse_gnu_build_id(notes)
    raise _build_failure("vmlinux carries no .note.gnu.build-id section")


def _read_section(store: _ValidatorStore, sht: bytes, e_shentsize: int, index: int) -> bytes:
    off = index * e_shentsize
    sh_offset = struct.unpack_from("<Q", sht, off + 0x18)[0]
    sh_size = struct.unpack_from("<Q", sht, off + 0x20)[0]
    return store.get_range("", start=sh_offset, length=sh_size) if False else store.get_range  # placeholder
```

*Fix the final helper — it must take the key. Correct `_read_section` to accept `key`:*

```python
def _read_section(store: _ValidatorStore, key: str, sht: bytes, e_shentsize: int, index: int) -> bytes:
    off = index * e_shentsize
    sh_offset = struct.unpack_from("<Q", sht, off + 0x18)[0]
    sh_size = struct.unpack_from("<Q", sht, off + 0x20)[0]
    return store.get_range(key, start=sh_offset, length=sh_size)
```

and call it as `_read_section(store, key, sht, e_shentsize, e_shstrndx)` / `(store, key, sht, e_shentsize, i)`. Add `validate_external_artifacts`, `extract_build_id_ranged`, `ValidatedUpload` to `__all__`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/providers/local_libvirt/test_validate_external_artifacts.py -v`
Expected: PASS (after finishing `_elf_with_build_id`).

- [ ] **Step 5: Lint, type, commit**

```bash
uv run ruff check src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_validate_external_artifacts.py
uv run ty check
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_validate_external_artifacts.py
git commit -m "feat(build): add external-artifact validation with ranged build-id check"
```

---

### Task 8: `runs.complete_build` + `runs.build` source gate

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py`
- Test: `tests/mcp/test_complete_build_tool.py`

`complete_build` consults the `(run_id, "build")` short-read **first** (idempotent retry returns the prior success), then applies the `CREATED`/`source=external` guard, validates uploads, writes the write-once `artifacts` rows + the ledger result (incl. `cmdline`), and drives `created → running → succeeded` under the per-Run lock. `runs.build` rejects an external-source Run.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_complete_build_tool.py` (reuse the seeding chain from `test_runs_tools.py`):

```python
"""runs.complete_build + the symmetric source gate (ADR-0048 §4/§6)."""

from __future__ import annotations

# imports + _pool + _ctx + seeding helpers mirror tests/mcp/test_runs_tools.py
from kdive.domain.errors import ErrorCategory
from kdive.domain.state import RunState
from kdive.mcp.tools import runs as runs_tools
from kdive.providers.local_libvirt.build import BuildOutput, ValidatedUpload
from kdive.store.objectstore import HeadResult


class _FakeValidator:
    """Injected validator: returns a ValidatedUpload (output + a head per declared key)."""

    def __init__(self, output: BuildOutput | Exception) -> None:
        self._output = output
        self.calls = 0

    def validate(self, run_id, manifest, keys, declared_build_id) -> ValidatedUpload:
        self.calls += 1
        if isinstance(self._output, Exception):
            raise self._output
        heads = {name: HeadResult(size_bytes=1, checksum_sha256="c", etag="e") for name in keys}
        return ValidatedUpload(output=self._output, heads=heads)


async def test_complete_build_finalizes_external_run(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run_with_manifest(pool)  # CREATED external Run + upload_manifests row + objects
        validator = _FakeValidator(BuildOutput("local/runs/%s/kernel" % run_id, "", ""))
        resp = await runs_tools.complete_build(
            pool, _ctx(), str(run_id), build_id=None, cmdline="dhash_entries=1",
            validator=validator,
        )
    assert resp.status == "succeeded"
    async with _pool(migrated_url) as pool, pool.connection() as conn:
        run = await RUNS.get(conn, run_id)
    assert run.state is RunState.SUCCEEDED and run.kernel_ref.endswith("/kernel")


async def test_complete_build_is_idempotent(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run_with_manifest(pool)
        validator = _FakeValidator(BuildOutput("local/runs/%s/kernel" % run_id, "", ""))
        first = await runs_tools.complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="c", validator=validator)
        second = await runs_tools.complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="c", validator=validator)
    assert first.status == second.status == "succeeded"
    assert validator.calls == 1  # the short-read short-circuited the retry


async def test_complete_build_rejects_server_run(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_server_run(pool)  # source="server"
        resp = await runs_tools.complete_build(
            pool, _ctx(), str(run_id), build_id=None, cmdline="c", validator=_FakeValidator(BuildOutput("k", "", "")),
        )
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value


async def test_complete_build_maps_validation_build_failure(migrated_url: str) -> None:
    from kdive.domain.errors import CategorizedError
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run_with_manifest(pool)
        bad = CategorizedError("bad", category=ErrorCategory.BUILD_FAILURE)
        resp = await runs_tools.complete_build(
            pool, _ctx(), str(run_id), build_id=None, cmdline="c", validator=_FakeValidator(bad),
        )
    assert resp.error_category == ErrorCategory.BUILD_FAILURE.value


async def test_build_run_rejects_external_source(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run(pool)  # CREATED external Run, no manifest needed
        resp = await runs_tools.build_run(pool, _ctx(), str(run_id))
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcp/test_complete_build_tool.py -v`
Expected: FAIL (`AttributeError: ... 'complete_build'`).

- [ ] **Step 3: Implement the source gate in `build_run`**

In `src/kdive/mcp/tools/runs.py`, `build_run`, after the existing `BuildProfile.parse` try/except, add the gate:

```python
            try:
                parsed = BuildProfile.parse(run.build_profile)
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)
            if parsed.source != "server":
                return _config_error(run_id, data={"reason": "external_source_uses_complete_build"})
            return await _build_locked(conn, ctx, run)
```

- [ ] **Step 4: Implement `complete_build`**

Add to `runs.py`. Imports: `from kdive.db import upload_manifest`, `from kdive.db.repositories import ARTIFACTS`, `from kdive.domain.models import Sensitivity`, `from kdive.providers.local_libvirt.build import BuildOutput, ValidatedUpload, validate_external_artifacts`, `from kdive.store.objectstore import HeadResult, StoredArtifact, object_store_from_env, register_artifact_row`, `from kdive.profiles.build import ExternalBuildProfile`, `from typing import Protocol`. (`HeadResult` is used only in `_finalize_external_build`'s signature; the validator returns the heads, so there is no second HEAD in the tool — the path is fully injectable via `validator`.)

Define the injected validator port + the default, then the core function:

```python
class _CompleteBuildValidator(Protocol):
    def validate(self, run_id, manifest, keys, declared_build_id) -> ValidatedUpload: ...


class _StoreBackedValidator:
    """Default validator: builds an ObjectStore from env and runs the provider validator."""

    def validate(self, run_id, manifest, keys, declared_build_id) -> ValidatedUpload:
        store = object_store_from_env()
        return validate_external_artifacts(
            store, manifest=manifest, keys=keys, declared_build_id=declared_build_id
        )


async def complete_build(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    build_id: str | None,
    cmdline: str,
    validator: _CompleteBuildValidator | None = None,
) -> ToolResponse:
    """Validate an external Run's uploads and finalize it ``created → succeeded``.

    Idempotent: a recorded ``(run_id, "build")`` ledger row short-circuits to the prior
    success **before** the CREATED/source guard, so a retry after a dropped connection
    returns success, not an illegal-transition error. Requires operator.
    """
    validator = validator or _StoreBackedValidator()
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)

            recorded = await _existing_build_result(conn, uid)
            if recorded is not None:
                return _complete_envelope(uid, recorded)

            parsed = BuildProfile.parse(run.build_profile)
            if not isinstance(parsed, ExternalBuildProfile):
                return _config_error(run_id, data={"reason": "not_external_source"})
            if run.state is not RunState.CREATED:
                return _config_error(run_id, data={"current_status": run.state.value})

            manifest_row = await upload_manifest.get_manifest(conn, "runs", uid)
            if manifest_row is None:
                return _config_error(run_id, data={"reason": "no_upload_manifest"})
            keys = {e.name: f"{manifest_row.prefix}{e.name}" for e in manifest_row.entries}

            try:
                validated = await asyncio.to_thread(
                    validator.validate, uid, list(manifest_row.entries), keys, build_id
                )
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)

            return await _finalize_external_build(
                conn, ctx, run, validated.output, cmdline, keys, validated.heads
            )


def _complete_envelope(run_id: UUID, result: dict[str, Any]) -> ToolResponse:
    """Build the success envelope from a ledger ``result`` (used live and on replay)."""
    refs = {"kernel": result["kernel_ref"]}
    if result.get("debuginfo_ref"):
        refs["vmlinux"] = result["debuginfo_ref"]
    if result.get("initrd_ref"):
        refs["initrd"] = result["initrd_ref"]
    return ToolResponse.success(
        str(run_id), "succeeded", suggested_next_actions=["runs.get"], refs=refs
    )


async def _finalize_external_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    output: BuildOutput,
    cmdline: str,
    keys: dict[str, str],
    heads: dict[str, HeadResult],
) -> ToolResponse:
    """Write artifact rows + ledger + drive created→succeeded under the per-Run lock.

    The external lane collapses ``created → succeeded`` in one locked transaction via
    guarded raw ``UPDATE``s (``WHERE state='created'``), bypassing ``can_transition`` the
    same way the server lane's ``_finalize_build`` does for ``running → succeeded`` — so no
    ``state.py`` edge change is needed. One write-once ``artifacts`` row is written per
    uploaded object, keyed by its **own** object key (``keys[name]``) — so an ``initrd`` is
    recorded against its real key, never the kernel's or vmlinux's.
    """
    result = {
        "kernel_ref": output.kernel_ref,
        "debuginfo_ref": output.debuginfo_ref,
        "initrd_ref": keys.get("initrd", ""),
        "build_id": output.build_id,
        "cmdline": cmdline,
    }
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(run.id))
        state = RunState(row["state"])
        if state is RunState.SUCCEEDED:  # a racing complete won; idempotent success
            recorded = await _existing_build_result(conn, run.id)
            return _complete_envelope(run.id, recorded or result)
        if state is not RunState.CREATED:
            return _config_error(str(run.id), data={"current_status": state.value})
        # One write-once artifacts row per uploaded object, keyed by its own object key
        # (sensitivity/retention are the SENSITIVE/build class the presign set on the object).
        for name, head in heads.items():
            stored = StoredArtifact(keys[name], head.etag, Sensitivity.SENSITIVE, "build")
            await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="runs", owner_id=run.id)
            )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result)),
        )
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = 'succeeded' "
            "WHERE id = %s AND state = 'created'",
            (output.kernel_ref, output.debuginfo_ref or None, run.id),
        )
        await audit.record(
            conn, ctx, tool="runs.complete_build", object_kind="runs", object_id=run.id,
            transition="created->succeeded", args={"run_id": str(run.id)}, project=run.project,
        )
        await upload_manifest.delete_manifest(conn, "runs", run.id)
    return _complete_envelope(run.id, result)
```

`heads` is keyed by artifact name and contains every uploaded object (kernel + optional initrd/vmlinux); `keys[name]` is each object's real key from the manifest, so the row loop records all of them — including `initrd`, which `BuildOutput` does not carry. Note `runs.created → succeeded` is **not** a legal direct edge in `state.py` (`CREATED → RUNNING → SUCCEEDED`). It does not need to be:

No `_TRANSITIONS[RunState]` change is required: `complete_build` writes the state with a raw `UPDATE ... SET state='succeeded' WHERE state='created'`, which bypasses `can_transition` exactly as `_build_locked`/`_finalize_build` use raw guarded UPDATEs. The state-machine guard only applies to `StatefulRepository.update_state`, which this path does not call. Confirm by re-reading `_finalize_build` (raw UPDATE) — same pattern. The audit transition string `created->succeeded` is allowed (audit records the actual transition).

> **Cmdline handoff (load-bearing for the demo, must be wired in the next spec).** This lane records `cmdline` in the build **ledger** `result` (`run_steps`), **not** in `run.build_profile`. The existing install path reads the cmdline from `run.build_profile` via `_cmdline_for` (`runs.py:446`), so for an external Run that helper returns `_DEFAULT_CMDLINE` and the agent-supplied cmdline (e.g. `dhash_entries=1` — the dcache demo's trigger) does **not** reach boot until install is wired to read it from the build ledger. Recording-only is correct for this spec's scope (install/boot is out of scope, ADR-0048 §7), but the next spec's install work **must** read the external Run's cmdline from the `(run_id, "build")` ledger `result["cmdline"]` (or teach `_cmdline_for` to fall back to it), or the demo boots with the default cmdline and never reproduces the bug. The lane does not mutate `build_profile` because it is an immutable request input (ADR-0003/0011) and `ExternalBuildProfile` is `extra="forbid"`, which `BuildProfile.parse` would then reject.

- [ ] **Step 5: Register the tool**

In `runs.py` `register(app, pool)`:

```python
    @app.tool(
        name="runs.complete_build",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_complete_build(
        run_id: Annotated[str, Field(description="The external-build Run to finalize.")],
        cmdline: Annotated[str, Field(description="Kernel debug args, e.g. 'dhash_entries=1'.")],
        build_id: Annotated[str | None, Field(description="GNU build-id as hex (e.g. from `readelf -n vmlinux`); required iff a vmlinux was uploaded. Case-insensitive.")] = None,
    ) -> ToolResponse:
        """Validate an external Run's uploads and finalize it to succeeded. Operator only."""
        return await complete_build(pool, current_context(), run_id, build_id=build_id, cmdline=cmdline)
```

- [ ] **Step 6: Run, lint, type, commit**

```bash
uv run pytest tests/mcp/test_complete_build_tool.py tests/mcp/test_runs_tools.py -v
uv run ruff check src/kdive/mcp/tools/runs.py tests/mcp/test_complete_build_tool.py
uv run ty check
git add src/kdive/mcp/tools/runs.py tests/mcp/test_complete_build_tool.py
git commit -m "feat(runs): add complete_build ingestion finalize + external-source gate"
```

---

### Task 11: Reconciler upload reaper

**Files:**
- Modify: `src/kdive/reconciler/loop.py`, `src/kdive/__main__.py`
- Test: `tests/reconciler/test_upload_reaper.py`

Adds an owner-agnostic repair: for each `upload_manifests` row past its `deadline` whose owner is still pre-finalize (`runs.state='created'` or `systems.state='defined'`), list the prefix and delete only objects with **no committed `artifacts` row**, then delete the manifest row.

- [ ] **Step 1: Write the failing test**

Create `tests/reconciler/test_upload_reaper.py` (reuse `tests/reconciler/conftest.py`):

```python
"""Owner-agnostic upload reaper (ADR-0048 §6)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from kdive.db import upload_manifest
from kdive.db.locks import LockScope
from kdive.reconciler.loop import _reap_one_owner, _repair_abandoned_uploads


class _FakeStore:
    def __init__(self, objects: dict[str, list[str]]) -> None:
        self._objects = objects  # prefix -> [keys]
        self.deleted: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        return list(self._objects.get(prefix, []))

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def test_reaps_uncommitted_objects_past_deadline_for_created_run(pg_conn) -> None:
    run_id = await _seed_created_run(pg_conn)  # CREATED Run; helper per tests/reconciler patterns
    prefix = f"local/runs/{run_id}/"
    await upload_manifest.replace_manifest(
        pg_conn, owner_kind="runs", owner_id=run_id, prefix=prefix,
        entries=[upload_manifest.ManifestEntry("kernel", "a", 1)],
        ttl=timedelta(seconds=-1),  # already past deadline
    )
    store = _FakeStore({prefix: [f"{prefix}kernel", f"{prefix}stray"]})
    count = await _repair_abandoned_uploads(pg_conn, store)
    assert count == 1
    assert sorted(store.deleted) == [f"{prefix}kernel", f"{prefix}stray"]
    assert await upload_manifest.get_manifest(pg_conn, "runs", run_id) is None


async def test_exempts_committed_object(pg_conn) -> None:
    system_id = await _seed_defined_system(pg_conn)
    prefix = f"local/systems/{system_id}/"
    await _insert_artifact_row(pg_conn, owner_kind="systems", owner_id=system_id, object_key=f"{prefix}rootfs")
    await upload_manifest.replace_manifest(
        pg_conn, owner_kind="systems", owner_id=system_id, prefix=prefix,
        entries=[upload_manifest.ManifestEntry("rootfs", "a", 1)], ttl=timedelta(seconds=-1),
    )
    store = _FakeStore({prefix: [f"{prefix}rootfs"]})
    await _repair_abandoned_uploads(pg_conn, store)
    assert store.deleted == []  # committed object exempt


async def test_skips_owner_not_past_deadline(pg_conn) -> None:
    run_id = await _seed_created_run(pg_conn)
    prefix = f"local/runs/{run_id}/"
    await upload_manifest.replace_manifest(
        pg_conn, owner_kind="runs", owner_id=run_id, prefix=prefix,
        entries=[upload_manifest.ManifestEntry("kernel", "a", 1)], ttl=timedelta(hours=1),
    )
    store = _FakeStore({prefix: [f"{prefix}kernel"]})
    assert await _repair_abandoned_uploads(pg_conn, store) == 0
    assert store.deleted == []


async def test_skips_finalized_owner(pg_conn) -> None:
    run_id = await _seed_succeeded_run(pg_conn)  # state='succeeded'
    prefix = f"local/runs/{run_id}/"
    await upload_manifest.replace_manifest(
        pg_conn, owner_kind="runs", owner_id=run_id, prefix=prefix,
        entries=[upload_manifest.ManifestEntry("kernel", "a", 1)], ttl=timedelta(seconds=-1),
    )
    store = _FakeStore({prefix: [f"{prefix}kernel"]})
    assert await _repair_abandoned_uploads(pg_conn, store) == 0


async def test_reap_one_owner_declines_renewed_manifest(pg_conn) -> None:
    """The locked re-read fences a deadline renewed (a create_upload re-mint) after select."""
    run_id = await _seed_created_run(pg_conn)
    prefix = f"local/runs/{run_id}/"
    await upload_manifest.replace_manifest(
        pg_conn, owner_kind="runs", owner_id=run_id, prefix=prefix,
        entries=[upload_manifest.ManifestEntry("kernel", "a", 1)], ttl=timedelta(hours=1),  # future
    )
    store = _FakeStore({prefix: [f"{prefix}kernel"]})
    # A stale candidate select could pick this owner; the locked re-read (future deadline)
    # declines to reap, so a concurrently re-minted manifest's objects are never deleted.
    assert await _reap_one_owner(pg_conn, store, "runs", run_id, LockScope.RUN) is False
    assert store.deleted == []
```

*Implementer: add the small `_seed_*`/`_insert_artifact_row` helpers per the existing `tests/reconciler` style.*

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/reconciler/test_upload_reaper.py -v`
Expected: FAIL (`ImportError: cannot import name '_repair_abandoned_uploads'`).

- [ ] **Step 3: Implement the repair + wire into `reconcile_once`**

In `src/kdive/reconciler/loop.py`:

Add an object-store Protocol near `InfraReaper`:

```python
@runtime_checkable
class UploadStore(Protocol):
    """The narrow object-store port the upload reaper consumes."""

    def list_prefix(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
```

Add the repair:

```python
_UPLOAD_PRE_FINALIZE = {"runs": "created", "systems": "defined"}


async def _repair_abandoned_uploads(conn: AsyncConnection, store: UploadStore) -> int:
    """Prefix-reap uncommitted objects of pre-finalize owners past their upload deadline.

    Candidate-selects ``upload_manifests`` rows with ``deadline < now()`` whose owner is
    still pre-finalize (a ``created`` Run or a ``defined`` System), then reaps each under
    its per-owner advisory lock. Returns the number of owners reaped (ADR-0048 §6).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT m.owner_kind, m.owner_id FROM upload_manifests m "
            "WHERE m.deadline < now() AND ("
            "  (m.owner_kind = 'runs' AND EXISTS ("
            "     SELECT 1 FROM runs r WHERE r.id = m.owner_id AND r.state = 'created')) "
            "  OR (m.owner_kind = 'systems' AND EXISTS ("
            "     SELECT 1 FROM systems s WHERE s.id = m.owner_id AND s.state = 'defined')))"
        )
        candidates = await cur.fetchall()
    reaped = 0
    for cand in candidates:
        scope = LockScope.RUN if cand["owner_kind"] == "runs" else LockScope.SYSTEM
        if await _reap_one_owner(conn, store, cand["owner_kind"], cand["owner_id"], scope):
            reaped += 1
    return reaped


async def _reap_one_owner(
    conn: AsyncConnection, store: UploadStore, owner_kind: str, owner_id: UUID, scope: LockScope
) -> bool:
    """Re-validate under the per-owner lock, then prefix-reap + delete the manifest.

    The lock serializes against a concurrent ``create_upload`` re-mint, ``complete_build``,
    or provision-consume on the **same** owner (which all take this lock), so a renewed
    deadline or a finalized owner is observed *before* any delete — mirroring
    :func:`_expire_one`'s locked re-read fence. Unlike :func:`_repair_leaked_domains`
    (whose ``destroy`` runs unlocked because libvirt can hang), the bounded S3 deletes run
    under the narrow per-owner lock so a re-mint cannot interleave between the re-check and
    the manifest delete. Deletes only objects with **no committed ``artifacts`` row**, so a
    referenced/consumed object (a slow-but-real rootfs) is never destroyed.
    """
    async with conn.transaction(), advisory_xact_lock(conn, scope, owner_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT prefix FROM upload_manifests "
                "WHERE owner_kind = %s AND owner_id = %s AND deadline < now()",
                (owner_kind, owner_id),
            )
            row = await cur.fetchone()
        if row is None:
            return False  # renewed (deadline pushed out) or already reaped since the select
        if not await _owner_pre_finalize(conn, owner_kind, owner_id):
            return False  # owner finalized between the candidate select and this lock
        for key in store.list_prefix(row["prefix"]):
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT 1 FROM artifacts WHERE object_key = %s", (key,))
                if await cur.fetchone() is None:
                    store.delete(key)
        await conn.execute(
            "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
    _log.info("reconciler: abandoned upload owner %s/%s reaped", owner_kind, owner_id)
    return True


async def _owner_pre_finalize(conn: AsyncConnection, owner_kind: str, owner_id: UUID) -> bool:
    """Report whether the owner is still in its pre-finalize state (locked re-read)."""
    table = "runs" if owner_kind == "runs" else "systems"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT 1 FROM {table} WHERE id = %s AND state = %s",  # noqa: S608 - 2-value whitelist
            (owner_id, _UPLOAD_PRE_FINALIZE[owner_kind]),
        )
        return await cur.fetchone() is not None
```

Thread an `UploadStore` through `reconcile_once`/`Reconciler` and `ReconcileReport`:
- Add field `abandoned_uploads: int` to `ReconcileReport`.
- `reconcile_once(pool, reaper, *, upload_store, ...)` gains an `upload_store: UploadStore | None = None` param; add the isolated repair only when present:
  ```python
  if upload_store is not None:
      await _isolated("abandoned_uploads", lambda conn: _repair_abandoned_uploads(conn, upload_store))
  ```
  Add `"abandoned_uploads": 0` to `counts` and to the returned `ReconcileReport`.
- `Reconciler.__init__` accepts `upload_store: UploadStore | None = None`; `run_once` forwards it.

- [ ] **Step 4: Wire the store at startup**

In `src/kdive/__main__.py`, where the `Reconciler` is constructed, build an `ObjectStore` from env (best-effort — if S3 env is absent the reaper stays off, like `NullReaper`):

```python
from kdive.store.objectstore import object_store_from_env
...
try:
    upload_store = object_store_from_env()
except CategorizedError:
    upload_store = None  # no object store configured; the upload reaper stays inactive
reconciler = Reconciler(pool, reaper, upload_store=upload_store)
```

*Re-read `__main__.py` for the exact construction site and imports before editing.*

- [ ] **Step 5: Run, lint, type, commit**

```bash
uv run pytest tests/reconciler/test_upload_reaper.py tests/reconciler -v
uv run ruff check src/kdive/reconciler/loop.py src/kdive/__main__.py tests/reconciler/test_upload_reaper.py
uv run ty check
git add src/kdive/reconciler/loop.py src/kdive/__main__.py tests/reconciler/test_upload_reaper.py
git commit -m "feat(reconciler): prefix-reap abandoned external uploads"
```

---

### Task 12: `live_stack` presigned round-trip test

**Files:**
- Create: `tests/integration/live_stack/test_presigned_upload.py`

Proves the store **rejects** a body whose checksum/length disagrees with the signed declaration, then accepts a matching upload — the ADR-0048 §2 verification item. Skips cleanly without the stack (mirror the existing `tests/store/conftest.py` Docker gate / `tests/integration/live_stack/conftest.py`).

- [ ] **Step 1: Write the test**

Create `tests/integration/live_stack/test_presigned_upload.py`:

```python
"""Live MinIO: presigned-PUT checksum/length enforcement (ADR-0048 §2, §7)."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from kdive.domain.models import Sensitivity

pytestmark = pytest.mark.live_stack  # confirm the marker used by tests/integration/live_stack


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_presigned_put_rejects_checksum_mismatch(minio_store, key_ns: str) -> None:
    payload = b"correct-bytes"
    wrong = _b64_sha256(b"different")
    key = f"{key_ns}/runs/r1/kernel"
    presigned = minio_store.presign_put(
        key, sha256=wrong, size_bytes=len(payload),
        sensitivity=Sensitivity.SENSITIVE, retention_class="build", expires_in=300,
    )
    resp = httpx.put(presigned.url, content=payload, headers=presigned.required_headers)
    assert resp.status_code >= 400  # the signed checksum disagrees with the body


def test_presigned_put_accepts_matching_upload(minio_store, key_ns: str) -> None:
    payload = b"correct-bytes"
    checksum = _b64_sha256(payload)
    key = f"{key_ns}/runs/r1/kernel"
    presigned = minio_store.presign_put(
        key, sha256=checksum, size_bytes=len(payload),
        sensitivity=Sensitivity.SENSITIVE, retention_class="build", expires_in=300,
    )
    resp = httpx.put(presigned.url, content=payload, headers=presigned.required_headers)
    assert resp.status_code < 300
    head = minio_store.head(key)
    assert head is not None and head.checksum_sha256 == checksum and head.size_bytes == len(payload)
```

*Implementer notes:* (1) confirm the live marker/fixtures — `minio_store`/`key_ns` live in `tests/store/conftest.py`; either move them to a shared conftest or import the session fixture. (2) `httpx` is already a dep (FastMCP); if not, use `urllib.request`. (3) If MinIO does not enforce `Content-Length` on a presigned PUT, the checksum rejection still holds and the size cap is covered by the pre-mint cap in `create_upload` — note this in the test docstring rather than asserting length enforcement separately.

- [ ] **Step 2: Run (with the stack)**

Run: `just test-live-stack` (or `KDIVE_REQUIRE_DOCKER=1 uv run pytest tests/integration/live_stack/test_presigned_upload.py -v`)
Expected: PASS with Docker; SKIP without.

- [ ] **Step 3: Lint, type, commit**

```bash
uv run ruff check tests/integration/live_stack/test_presigned_upload.py
uv run ty check
git add tests/integration/live_stack/test_presigned_upload.py
git commit -m "test(live): assert presigned-PUT checksum enforcement on MinIO"
```

---

### Task 13: Adversarial concurrency test

**Files:**
- Create: `tests/adversarial/test_complete_build_concurrency.py`

- [ ] **Step 1: Write the test**

Create `tests/adversarial/test_complete_build_concurrency.py` (mirror `tests/adversarial/conftest.py` + `test_idempotency_concurrency.py`):

```python
"""Concurrent complete_build serializes to one ledger row (ADR-0048 §6)."""

from __future__ import annotations

import asyncio

import pytest

from kdive.domain.state import RunState
from kdive.mcp.tools import runs as runs_tools
from kdive.providers.local_libvirt.build import BuildOutput


async def test_concurrent_complete_build_yields_one_ledger_row(migrated_url: str) -> None:
    async with _pool(migrated_url) as pool:
        run_id = await _seed_external_run_with_manifest(pool)
        validator = _CountingValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
        results = await asyncio.gather(
            runs_tools.complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="c", validator=validator),
            runs_tools.complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="c", validator=validator),
        )
    assert all(r.status == "succeeded" for r in results)
    async with _pool(migrated_url) as pool, pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,))
        assert (await cur.fetchone())[0] == 1
        run = await RUNS.get(conn, run_id)
    assert run.state is RunState.SUCCEEDED
```

`_CountingValidator` wraps `_FakeValidator` from Task 8 (both racers may validate; the per-Run lock + `ON CONFLICT DO NOTHING` + the `WHERE state='created'` fence collapse to one ledger row and one transition).

- [ ] **Step 2: Run**

Run: `uv run pytest tests/adversarial/test_complete_build_concurrency.py -v`
Expected: PASS.

- [ ] **Step 3: Lint, type, commit**

```bash
uv run ruff check tests/adversarial/test_complete_build_concurrency.py
uv run ty check
git add tests/adversarial/test_complete_build_concurrency.py
git commit -m "test(adversarial): concurrent complete_build collapses to one ledger row"
```

---

# Milestone B — Rootfs provisioning resolution

Produces a working capability on its own: a System can be provisioned from a `path`/`upload`/`url`/`catalog` rootfs reference, with the `upload` kind committing its `artifacts` row (so the reaper exempts it). Tasks 9–10. Builds on Milestone A's `create_upload(system)` + reaper(system) — both already owner-agnostic.

---

### Task 9: Provider fixture catalog

Superseded by ADR-0065. Rootfs catalog references are provider-scoped component references
backed by `fixtures/local-libvirt/manifest.yaml`, `fixtures/local-libvirt/rootfs/*.yaml`,
and `kdive.provider_components.catalog`. The removed standalone rootfs catalog package must not be
reintroduced.

### Task 10: Rootfs source-kind resolution in provisioning

**Files:**
- Modify: `src/kdive/profiles/provisioning.py`, `src/kdive/providers/local_libvirt/provisioning.py`, `src/kdive/mcp/tools/systems.py`
- Test: `tests/profiles/test_rootfs_source.py`, `tests/providers/local_libvirt/test_rootfs_resolve.py`

Replaces the bare `LibvirtProfile.rootfs_image_ref: NonEmptyStr` with a discriminated `rootfs: RootfsSource`. The resolver validates each kind and returns the libvirt disk path; `path` is back-compatible (today's direct file path). The `upload` kind, when consumed at provisioning, commits the write-once `artifacts` row + deletes the manifest so the reaper exempts it.

**Scope honesty:** fetching `url`/`catalog` images to a libvirt-readable path is the next spec's work. This task validates the reference and resolves the *intended* disk path; for `url`/`catalog` the resolved path is where the fetched image will land. Booting is out of scope (ADR-0048 §7).

- [ ] **Step 1: Write the failing schema tests**

Create `tests/profiles/test_rootfs_source.py`:

```python
"""Discriminated rootfs source on the libvirt profile (ADR-0048 §3)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile


def _profile(rootfs: dict) -> dict:
    return {
        "schema_version": 1, "arch": "x86_64", "vcpu": 2, "memory_mb": 2048, "disk_gb": 10,
        "boot_method": "direct-kernel", "kernel_source_ref": "git#v7.0",
        "provider": {"local-libvirt": {"rootfs": rootfs, "crashkernel": "256M"}},
    }


def test_path_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(_profile({"kind": "path", "path": "/var/lib/kdive/rootfs/x.qcow2"}))
    assert parsed.provider.local_libvirt.rootfs.kind == "path"


def test_catalog_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(_profile({"kind": "catalog", "name": "fedora-cloud-base-43-x86_64"}))
    assert parsed.provider.local_libvirt.rootfs.name == "fedora-cloud-base-43-x86_64"


def test_url_kind_requires_sha256() -> None:
    with pytest.raises(CategorizedError) as e:
        ProvisioningProfile.parse(_profile({"kind": "url", "url": "https://h/i.qcow2"}))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_upload_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(_profile({"kind": "upload"}))
    assert parsed.provider.local_libvirt.rootfs.kind == "upload"


def test_unknown_kind_rejected() -> None:
    with pytest.raises(CategorizedError) as e:
        ProvisioningProfile.parse(_profile({"kind": "bogus"}))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/profiles/test_rootfs_source.py -v`
Expected: FAIL (parse accepts the old `rootfs_image_ref` string, not the new `rootfs` object).

- [ ] **Step 3: Add `RootfsSource` to the profile**

In `src/kdive/profiles/provisioning.py`, add the discriminated models above `LibvirtProfile`:

```python
from typing import Annotated, Literal, Union  # extend imports
from pydantic import Field  # already imported


class _PathRootfs(_ProfileBase):
    kind: Literal["path"]
    path: NonEmptyStr


class _UploadRootfs(_ProfileBase):
    kind: Literal["upload"]


class _UrlRootfs(_ProfileBase):
    kind: Literal["url"]
    url: NonEmptyStr
    sha256: NonEmptyStr  # 'sha256:<64-hex>'; format checked at resolve


class _CatalogRootfs(_ProfileBase):
    kind: Literal["catalog"]
    name: NonEmptyStr


type RootfsSource = Annotated[
    Union[_PathRootfs, _UploadRootfs, _UrlRootfs, _CatalogRootfs],
    Field(discriminator="kind"),
]
```

Change `LibvirtProfile.rootfs_image_ref` to:

```python
    rootfs: RootfsSource
```

(Remove the old `rootfs_image_ref: NonEmptyStr` line.)

- [ ] **Step 4: Run the schema tests**

Run: `uv run pytest tests/profiles/test_rootfs_source.py -v`
Expected: PASS.

- [ ] **Step 5: Update the renderer + existing provisioning tests + write the resolver**

In `src/kdive/providers/local_libvirt/provisioning.py`, add a resolver that turns a
`RootfsSource` into a libvirt disk path, and use it in `render_domain_xml`. Catalog
references resolve through `kdive.provider_components.catalog`.

```python
_ROOTFS_DIR = "/var/lib/kdive/rootfs"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}\Z")


def resolve_rootfs_path(rootfs, *, tenant: str, system_id: UUID) -> str:
    """Resolve a rootfs source to the libvirt-readable disk path (ADR-0048 §5).

    Validates the reference and returns the path libvirt attaches: ``path`` is the
    declared file; ``upload`` is the System-owned object's local staging path; ``url``
    and ``catalog`` resolve to a content-addressed/name-addressed staging path under the
    rootfs dir (fetch lands in the next spec). The reference is validated here; existence
    of an unfetched image is the next spec's concern.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a malformed url checksum or unknown
            catalog name.
    """
    kind = rootfs.kind
    if kind == "path":
        return rootfs.path
    if kind == "upload":
        return f"{_ROOTFS_DIR}/{tenant}-systems-{system_id}-rootfs.qcow2"
    if kind == "url":
        if not _SHA256.match(rootfs.sha256):
            raise CategorizedError(
                "rootfs url sha256 must be 'sha256:<64-hex>'",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return f"{_ROOTFS_DIR}/url-{rootfs.sha256.removeprefix('sha256:')}.qcow2"
    entry = load_catalog().lookup(rootfs.name)
    if entry is None:
        raise CategorizedError(
            f"unknown rootfs catalog name: {rootfs.name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": rootfs.name},
        )
    return f"{_ROOTFS_DIR}/{entry.name}.qcow2"


def validate_rootfs_reference(rootfs) -> None:
    """Validate a rootfs reference's resolvability (a synchronous tool-boundary check).

    Mirrors :func:`resolve_rootfs_path`'s checks (url sha256 format, catalog-name
    existence) but needs no ``system_id`` — so the ``systems.provision`` tool can reject a
    bad reference synchronously as ``configuration_error`` instead of dead-lettering the
    provision job. The ``upload``/``path`` kinds need no static check (an ``upload``
    object's existence is verified at provision-consume time, §5).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a malformed url checksum or unknown
            catalog name.
    """
    if rootfs.kind == "url" and not _SHA256.match(rootfs.sha256):
        raise CategorizedError(
            "rootfs url sha256 must be 'sha256:<64-hex>'",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if rootfs.kind == "catalog" and load_catalog().lookup(rootfs.name) is None:
        raise CategorizedError(
            f"unknown rootfs catalog name: {rootfs.name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": rootfs.name},
        )
```

Wire `validate_rootfs_reference` into the existing `validate_profile` (so it runs at **both** the tool boundary — `provision_system`/`reprovision` already call `validate_profile` — and at render time, matching the `domain_xml_params` pattern). In `validate_profile`, after the `domain_xml_params` check, add:

```python
    validate_rootfs_reference(profile.provider.local_libvirt.rootfs)
```

Add a `test_rootfs_resolve.py` case asserting `validate_rootfs_reference` raises `CONFIGURATION_ERROR` for an unknown catalog name and a malformed url sha256 (the same inputs the resolver rejects, but via the tool-boundary entry point).

In `render_domain_xml`, replace `ET.SubElement(disk, "source", file=section.rootfs_image_ref)` with:

```python
    rootfs_path = resolve_rootfs_path(section.rootfs, tenant="local", system_id=system_id)
    ET.SubElement(disk, "source", file=rootfs_path)
```

Update **every** test fixture that used `rootfs_image_ref="..."` to the new `rootfs={"kind":"path","path":"..."}` shape. This is a wide, mechanical change — run `rg -l rootfs_image_ref tests/` and fix each file; at the time of writing that is 12 files, and the highest-leverage ones are the **shared seeding helpers** `tests/mcp/_seed.py` and `tests/integration/_seed.py` (used by many suites — fixing them resolves most call sites at once), plus `tests/profiles/test_provisioning.py`, `tests/providers/local_libvirt/test_provisioning.py`, `tests/mcp/test_systems_tools.py`, `tests/adversarial/test_provider_xml.py`, `tests/adversarial/test_provider_state_races.py`, `tests/adversarial/test_debug_session_races.py`, `tests/mcp/test_control_tools.py`, `tests/mcp/test_debug_ops.py`, `tests/mcp/test_debug_tools.py`, and `tests/integration/test_live_stack.py`. After the change, `rg -l rootfs_image_ref tests/ src/` must return nothing.

Create `tests/providers/local_libvirt/test_rootfs_resolve.py`:

```python
"""Rootfs resolver (ADR-0048 §5)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _CatalogRootfs, _PathRootfs, _UploadRootfs, _UrlRootfs
from kdive.providers.local_libvirt.lifecycle.provisioning import (
    resolve_rootfs_path,
    validate_rootfs_reference,
)

_SID = uuid4()


def test_path_passthrough() -> None:
    r = _PathRootfs(kind="path", path="/img/x.qcow2")
    assert resolve_rootfs_path(r, tenant="local", system_id=_SID) == "/img/x.qcow2"


def test_upload_uses_system_keyed_path() -> None:
    r = _UploadRootfs(kind="upload")
    assert str(_SID) in resolve_rootfs_path(r, tenant="local", system_id=_SID)


def test_url_bad_checksum_rejected() -> None:
    r = _UrlRootfs(kind="url", url="https://h/i.qcow2", sha256="deadbeef")
    with pytest.raises(CategorizedError) as e:
        resolve_rootfs_path(r, tenant="local", system_id=_SID)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_unknown_catalog_rejected() -> None:
    r = _CatalogRootfs(kind="catalog", name="no-such")
    with pytest.raises(CategorizedError) as e:
        resolve_rootfs_path(r, tenant="local", system_id=_SID)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_rootfs_reference_rejects_bad_url_checksum_at_tool_boundary() -> None:
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(_UrlRootfs(kind="url", url="https://h/i.qcow2", sha256="nope"))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_rootfs_reference_rejects_unknown_catalog_at_tool_boundary() -> None:
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(_CatalogRootfs(kind="catalog", name="no-such"))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 6: Commit the rootfs `artifacts` row at provisioning**

In `src/kdive/mcp/tools/systems.py` `provision_handler` (the success path that drives `provisioning → ready`), when the profile's rootfs kind is `upload`, write the write-once `artifacts` row for the System-owned object and delete its manifest — so the reaper exempts it. Add a helper called from the handler after a successful provision, inside the per-System locked transaction that records readiness:

```python
async def _commit_uploaded_rootfs(conn, system, profile) -> None:
    """Commit the write-once artifacts row for an 'upload'-kind rootfs (ADR-0048 §6)."""
    rootfs = profile.provider.local_libvirt.rootfs
    if rootfs.kind != "upload":
        return
    from kdive.store.objectstore import artifact_key, object_store_from_env, register_artifact_row
    from kdive.db import upload_manifest
    from kdive.domain.models import Sensitivity
    key = artifact_key("local", "systems", str(system.id), "rootfs")
    head = await asyncio.to_thread(object_store_from_env().head, key)
    if head is None:
        raise CategorizedError(
            "upload-kind rootfs was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system.id)},
        )
    stored = StoredArtifact(key, head.etag, Sensitivity.SENSITIVE, "rootfs")
    await ARTIFACTS.insert(conn, register_artifact_row(stored, owner_kind="systems", owner_id=system.id))
    await upload_manifest.delete_manifest(conn, "systems", system.id)
```

Wire it into the provision handler's ready-transition transaction (re-read `provision_handler` lines 326+ for the exact commit point; add `ARTIFACTS`/`StoredArtifact` imports). *If the upload object is absent, provisioning fails with `configuration_error` — matching §5's "validated when resolved at provisioning."*

Add a `test_systems_tools.py` case: an `upload`-kind rootfs with a present object writes one `systems`-owned `artifacts` row and removes the manifest; with an absent object, provision fails `configuration_error`. (Use the systems-tools test harness already in the repo + a fake store.)

- [ ] **Step 7: Run, lint, type, commit**

```bash
uv run pytest tests/profiles/test_rootfs_source.py tests/providers/local_libvirt/ tests/mcp/test_systems_tools.py -v
uv run ruff check src/kdive/profiles/provisioning.py src/kdive/providers/local_libvirt/provisioning.py src/kdive/mcp/tools/systems.py tests/profiles/test_rootfs_source.py tests/providers/local_libvirt/test_rootfs_resolve.py
uv run ty check
git add -A
git commit -m "feat(provisioning): resolve rootfs source kinds; commit uploaded rootfs row"
```

---

## Final verification (run after all tasks)

- [ ] **Full guardrail sweep** (the recipes CI runs individually — see memory):

```bash
uv run ruff check src tests
uv run ty check
uv run pytest -q              # unit + db + adversarial (Docker-gated suites skip without it)
just test-live-stack         # with the MinIO/Postgres stack up
```

- [ ] **Migration check:** `uv run python -c "from kdive.db.migrate import discover_migrations; print([m.version for m in discover_migrations()])"` — confirm `0006` is discovered and ordered last.

- [ ] **Docs/reference drift gate (CI runs it):** `just docs-check` — regenerate any tool-surface reference if `create_upload`/`complete_build` are listed; commit the regenerated artifact.

- [ ] **Open the PR** (never push to main):

```bash
git push -u origin feat/external-build-artifact-ingestion
gh pr create --base main --title "feat: external-build artifact ingestion (ADR-0048)" --body "$(cat <<'EOF'
Implements ADR-0048 / the external-build ingestion spec: agents upload locally-built
kernel artifacts (and a rootfs reference) and finalize an external-build Run to
`succeeded` with validated, well-formed artifacts — no server-side make. Bootability is
out of scope (next spec).

- Object store: head / get_range / presign_put / list_prefix / delete + key helpers
- BuildProfile source-discrimination (server/external)
- artifacts.create_upload (presigned PUTs + persisted manifest)
- runs.complete_build (validate → write-once artifact rows + ledger → created→succeeded, idempotent) + symmetric runs.build gate
- Reconciler prefix-reaper for abandoned uploads
- Rootfs source kinds (path/upload/url/catalog) + ported catalog
- Unit, adversarial, and live_stack tests

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Merge with `--rebase` or `--merge` (never `--squash` — preserves bisectable history). Watch CI **and** `mergeStateStatus`: `gh pr checks --watch` plus `gh pr view --json mergeable,mergeStateStatus`; the exit condition is checks green **and** `MERGEABLE`/`CLEAN`.

---

## Self-review (completed during planning)

**Spec coverage** — every §2 scope item maps to a task: object-store methods (T1–3); BuildProfile discrimination (T4); create_upload (T6); complete_build (T8); provisioning rootfs source kind + commit row (T10); validate_external_artifacts + ranged build-id (T7); upload-manifest migration/storage (T5); reconciler prefix-reaper (T11); ported catalog (T9). §5 validation order (required-set → existence/size → integrity → magic → build_id) is T7. §6 error taxonomy uses only real `ErrorCategory` values. §7 unit/adversarial/live_stack tests are T7/T12/T13. §8 success criterion (validated, well-formed; not bootable) is the Milestone A deliverable.

**Type consistency** — `sha256` is base64 everywhere; `ManifestEntry`/`UploadManifest`/`HeadResult`/`PresignedUpload`/`BuildOutput`/`ValidatedUpload` names are used identically across tasks; the validator returns `ValidatedUpload(output, heads)` so `complete_build` writes artifact rows from `heads` and never calls the object store directly (fully injectable); `owner_kind` wire values are `"run"`/`"system"` at the tool boundary and `"runs"`/`"systems"` as object-store kinds (the tool maps between them in T6/T8/T10) — this is the one deliberate dual-vocabulary; keep the mapping at the tool boundary only.

**Known sharp edges flagged inline for the implementer** — (1) the `created → succeeded` collapse uses guarded raw `UPDATE`s (not `update_state`), so no `state.py` edit (T8); (2) the T8 finalize writes one write-once `artifacts` row per uploaded object keyed by its **own** object key (`keys[name]`) — `BuildOutput` carries only `kernel_ref`/`debuginfo_ref`, so `initrd` is recorded from `keys`/`heads`, not from `BuildOutput` (T8); (3) `_read_section` takes the object `key` (T7); (4) presigned-PUT length enforcement is asserted by the live test, with the pre-mint cap as the guaranteed backstop, and the `head`-vs-manifest recheck fails closed when a checksum is absent (T12); (5) re-read `__main__.py` and `provision_handler` for exact wiring sites before editing (T10/T11); (6) update existing `rootfs_image_ref` test fixtures to the new `rootfs` shape (T10); (7) `create_upload` takes the per-owner advisory lock and re-checks owner state in-lock before minting/persisting, so a concurrent finalize cannot strand a reaper-invisible manifest (T6); (8) the reaper reaps each owner under the **same** per-owner lock with a locked re-read of the deadline + pre-finalize state (`_reap_one_owner`), so a concurrent `create_upload` re-mint or finalize is observed before any delete — mirroring `_expire_one`, and deliberately holding the lock across the bounded S3 deletes (unlike `_repair_leaked_domains`, whose libvirt `destroy` can hang) (T11); (9) **cmdline handoff** — `complete_build` records the agent's `cmdline` in the build ledger `result`, not in `build_profile`; the existing `_cmdline_for` reads `build_profile`, so the next spec's install work must read the external Run's cmdline from the ledger or the demo boots with the default cmdline (T8).
