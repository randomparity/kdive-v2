# Chunked External-Build Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent upload an external-build artifact larger than the 5 GiB single-PUT ceiling by splitting it client-side into ≤5 GiB chunks that `runs.complete_build` reassembles server-side into one object.

**Architecture:** The agent declares per-artifact ordered chunks; `create_upload` mints one checksum-pinned presigned PUT per chunk at `<name>.partNNNN`; `complete_build` HEAD-verifies each chunk, reassembles via `CreateMultipartUpload`+`UploadPartCopy`+`Complete` (server-side copy, no bytes through the server) under a deadline-refresh window guard, validates the single final object, and best-effort deletes chunks. Integrity moves to per-chunk SHA-256 pins; the whole-object hash is advisory. The reaper's `runs` branch is generalized to reclaim a finalized Run's leaked chunks.

**Tech Stack:** Python 3.13, `uv`, `boto3` S3 client, `psycopg`/Postgres (JSONB manifest, no DDL migration), `pytest`. Guardrails: `just lint`, `just type`, `just test`.

**Spec:** `docs/superpowers/specs/2026-06-13-chunked-external-upload-design.md`
**ADR:** `docs/adr/0104-chunked-external-upload-reassembly.md`

**Conventions (apply to every task):**
- TDD: failing test first, confirm it fails for the right reason, minimal impl, confirm green, refactor green.
- Run `just lint` + `just type` + the focused test before every commit. `ty` is whole-tree.
- Absolute imports only. Line length 100. Google-style docstrings on non-trivial public APIs.
- `ToolResponse` envelopes carry a literal `ErrorCategory` on failure; pick the most specific existing category (`CONFIGURATION_ERROR`, `BUILD_FAILURE`, `INFRASTRUCTURE_FAILURE`). Never invent error strings; `reason` data keys are literal.
- Conventional-commit subjects ≤72 chars, imperative, ending with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

**Constants (define once, in `config/core_settings.py` neighbours or `provider_components/uploads.py`):**
- `SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024` (5 GiB)
- `MAX_PART_BYTES = 5 * 1024 * 1024 * 1024` (5 GiB)
- `MIN_PART_BYTES = 5 * 1024 * 1024` (5 MiB)
- `MAX_PARTS = 10_000`

---

## File Structure

- `src/kdive/config/core_settings.py` — raise `MAX_UPLOAD_BYTES` default 5 GiB → 50 GiB.
- `src/kdive/provider_components/uploads.py` — `ChunkEntry`, `ManifestEntry.chunks`, the size constants.
- `src/kdive/provider_components/artifacts.py` — `chunk_key()` helper.
- `src/kdive/db/upload_manifest.py` — (de)serialize `chunks` in the JSONB payload.
- `src/kdive/store/objectstore.py` — four multipart primitives.
- `src/kdive/mcp/tools/catalog/artifacts/uploads.py` — chunked declaration validation + per-chunk presign.
- `src/kdive/provider_components/build_validation.py` — chunked head-verify (skip whole-object checksum).
- `src/kdive/providers/.../reassembly.py` (new small helper) — orchestrate `create/copy/complete/abort`.
- `src/kdive/mcp/tools/lifecycle/runs/build.py` — window guard, reassembly call, idempotent failure re-check, post-commit cleanup, deferred manifest delete.
- `src/kdive/reconciler/uploads.py` — `runs`-branch gate drop.

Tests mirror under `tests/`. Existing files to extend: `tests/provider_components/test_uploads.py` (or create), `tests/store/test_objectstore.py`, `tests/db/test_upload_manifest.py`, `tests/mcp/lifecycle/test_create_upload_tool.py`, `tests/providers/local_libvirt/test_validate_external_artifacts.py`, `tests/mcp/lifecycle/test_complete_build_tool.py`, `tests/reconciler/test_upload_reaper.py`, `tests/adversarial/test_complete_build_concurrency.py`.

---

## Task 1: Chunk value types + size constants

**Files:**
- Modify: `src/kdive/provider_components/uploads.py`
- Test: `tests/provider_components/test_uploads.py` (create if absent)

- [ ] **Step 1: Write the failing test**

```python
# tests/provider_components/test_uploads.py
from kdive.provider_components.uploads import (
    ChunkEntry,
    ManifestEntry,
    MAX_PART_BYTES,
    MIN_PART_BYTES,
    SINGLE_PUT_MAX_BYTES,
)


def test_manifest_entry_defaults_chunks_to_none():
    entry = ManifestEntry(name="vmlinux", sha256="abc", size_bytes=10)
    assert entry.chunks is None


def test_manifest_entry_carries_ordered_chunks():
    chunks = (ChunkEntry(sha256="c0", size_bytes=5), ChunkEntry(sha256="c1", size_bytes=5))
    entry = ManifestEntry(name="vmlinux", sha256="whole", size_bytes=10, chunks=chunks)
    assert entry.chunks == chunks
    assert entry.chunks[0].size_bytes == 5


def test_part_size_constants_are_ordered():
    assert MIN_PART_BYTES < MAX_PART_BYTES == SINGLE_PUT_MAX_BYTES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/provider_components/test_uploads.py -q`
Expected: FAIL (ImportError: cannot import name `ChunkEntry`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/provider_components/uploads.py
"""Shared upload declaration value types."""

from __future__ import annotations

from typing import NamedTuple

SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024
MAX_PART_BYTES = 5 * 1024 * 1024 * 1024
MIN_PART_BYTES = 5 * 1024 * 1024
MAX_PARTS = 10_000


class ChunkEntry(NamedTuple):
    """One declared chunk of a chunked artifact: its base64 SHA-256 and byte size."""

    sha256: str
    size_bytes: int


class ManifestEntry(NamedTuple):
    """One declared artifact: name, base64 SHA-256, byte size, and optional ordered chunks.

    ``chunks is None`` is a single-PUT artifact. When ``chunks`` is set, ``sha256`` is the
    advisory whole-object hash and ``size_bytes`` is the whole-object total (== the chunk
    size sum); integrity is anchored on the per-chunk ``sha256`` values (ADR-0104 §2).
    """

    name: str
    sha256: str
    size_bytes: int
    chunks: tuple[ChunkEntry, ...] | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/provider_components/test_uploads.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/provider_components/uploads.py tests/provider_components/test_uploads.py
git commit -m "feat(uploads): add ChunkEntry + ManifestEntry.chunks + part-size constants"
```

---

## Task 2: `chunk_key` helper

**Files:**
- Modify: `src/kdive/provider_components/artifacts.py` (add after `owner_prefix`, ~line 57)
- Test: `tests/provider_components/test_chunk_key.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/provider_components/test_chunk_key.py
import pytest

from kdive.domain.errors import CategorizedError
from kdive.provider_components.artifacts import chunk_key, owner_prefix


def test_chunk_key_is_zero_padded_one_based():
    prefix = owner_prefix("local", "runs", "11111111-1111-1111-1111-111111111111")
    assert chunk_key(prefix, "vmlinux", 1) == f"{prefix}vmlinux.part0001"
    assert chunk_key(prefix, "vmlinux", 42) == f"{prefix}vmlinux.part0042"


def test_chunk_key_rejects_non_positive_part_number():
    prefix = owner_prefix("local", "runs", "11111111-1111-1111-1111-111111111111")
    with pytest.raises(CategorizedError):
        chunk_key(prefix, "vmlinux", 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/provider_components/test_chunk_key.py -q`
Expected: FAIL (cannot import `chunk_key`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/provider_components/artifacts.py  (add below owner_prefix)
from kdive.domain.errors import CategorizedError, ErrorCategory  # if not already imported


def chunk_key(prefix: str, name: str, part_number: int) -> str:
    """The object key for chunk ``part_number`` of a chunked artifact: ``<prefix><name>.partNNNN``.

    ``prefix`` is an :func:`owner_prefix` result (trailing ``/``). ``part_number`` is 1-based
    and zero-padded to four digits. This is the single source of the chunk-key format used by
    both ``create_upload`` (mint) and reassembly (read) so the two sites cannot drift
    (ADR-0104 §1, spec §3).
    """
    if part_number < 1:
        raise CategorizedError(
            f"chunk part_number must be >= 1, got {part_number}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return f"{prefix}{name}.part{part_number:04d}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/provider_components/test_chunk_key.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/provider_components/artifacts.py tests/provider_components/test_chunk_key.py
git commit -m "feat(artifacts): add shared chunk_key helper for partNNNN keys"
```

---

## Task 3: Manifest JSONB round-trips chunks

**Files:**
- Modify: `src/kdive/db/upload_manifest.py:56-65` (`replace_manifest` payload) and `:90-92` (load)
- Test: `tests/db/test_upload_manifest.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_upload_manifest.py  (add; reuse the file's existing pool/owner fixtures)
import pytest

from kdive.db import upload_manifest
from kdive.provider_components.uploads import ChunkEntry, ManifestEntry


@pytest.mark.asyncio
async def test_manifest_round_trips_chunks(pool, run_owner_id):  # fixtures per existing tests
    entries = [
        ManifestEntry(
            name="vmlinux",
            sha256="whole",
            size_bytes=10,
            chunks=(ChunkEntry("c0", 6), ChunkEntry("c1", 4)),
        ),
        ManifestEntry(name="kernel", sha256="k", size_bytes=3),
    ]
    async with pool.connection() as conn:
        await upload_manifest.replace_manifest(
            conn,
            upload_manifest.UploadManifestReplaceRequest(
                owner_kind="runs",
                owner_id=run_owner_id,
                prefix="local/runs/x/",
                entries=entries,
                ttl=__import__("datetime").timedelta(hours=1),
            ),
        )
        loaded = await upload_manifest.get_manifest(conn, "runs", run_owner_id)
    by_name = {e.name: e for e in loaded.entries}
    assert by_name["vmlinux"].chunks == (ChunkEntry("c0", 6), ChunkEntry("c1", 4))
    assert by_name["kernel"].chunks is None
```

(If the existing test file has no shared `pool`/`run_owner_id` fixtures, mirror the setup an existing `test_upload_manifest.py` test uses to insert a Run and pool.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/db/test_upload_manifest.py -k chunks -q`
Expected: FAIL — loaded chunks are `None` (payload drops them).

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/db/upload_manifest.py — replace_manifest payload builder
payload = [
    {
        "name": e.name,
        "sha256": e.sha256,
        "size_bytes": e.size_bytes,
        **(
            {"chunks": [{"sha256": c.sha256, "size_bytes": c.size_bytes} for c in e.chunks]}
            if e.chunks is not None
            else {}
        ),
    }
    for e in request.entries
]
```

```python
# src/kdive/db/upload_manifest.py — get_manifest entry rebuild
entries = tuple(
    ManifestEntry(
        e["name"],
        e["sha256"],
        int(e["size_bytes"]),
        chunks=(
            tuple(ChunkEntry(c["sha256"], int(c["size_bytes"])) for c in e["chunks"])
            if e.get("chunks") is not None
            else None
        ),
    )
    for e in row["manifest"]
)
```

Add `from kdive.provider_components.uploads import ChunkEntry, ManifestEntry` to the imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/db/test_upload_manifest.py -q`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/db/upload_manifest.py tests/db/test_upload_manifest.py
git commit -m "feat(upload-manifest): persist optional chunk list in JSONB (no migration)"
```

---

## Task 4: Object-store multipart primitives

**Files:**
- Modify: `src/kdive/store/objectstore.py` (add four methods to `ObjectStore`, after `presign_get`)
- Test: `tests/store/test_objectstore.py` (extend with a stub boto3 client)

- [ ] **Step 1: Write the failing test**

```python
# tests/store/test_objectstore.py  (add; follow the file's existing stub-client pattern)
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import ObjectStore


class _MpuClient:
    def __init__(self):
        self.calls = []

    def create_multipart_upload(self, **kw):
        self.calls.append(("create", kw))
        return {"UploadId": "uid-1"}

    def upload_part_copy(self, **kw):
        self.calls.append(("copy", kw))
        return {"CopyPartResult": {"ETag": '"etag-%s"' % kw["PartNumber"]}}

    def complete_multipart_upload(self, **kw):
        self.calls.append(("complete", kw))
        return {"ETag": '"final-etag"'}

    def abort_multipart_upload(self, **kw):
        self.calls.append(("abort", kw))


def test_multipart_reassembly_primitives_round_trip():
    client = _MpuClient()
    store = ObjectStore(client, "bucket")
    uid = store.create_multipart_upload(
        "local/runs/x/vmlinux", sensitivity=Sensitivity.SENSITIVE, retention_class="build"
    )
    assert uid == "uid-1"
    create_kw = client.calls[0][1]
    assert create_kw["Metadata"] == {"sensitivity": "sensitive", "retention-class": "build"}
    etag1 = store.upload_part_copy(
        "local/runs/x/vmlinux", uid, part_number=1, source_key="local/runs/x/vmlinux.part0001"
    )
    assert etag1 == "etag-1"
    assert client.calls[1][1]["CopySource"] == {"Bucket": "bucket", "Key": "local/runs/x/vmlinux.part0001"}
    final = store.complete_multipart_upload("local/runs/x/vmlinux", uid, [(1, "etag-1")])
    assert final == "final-etag"
    assert client.calls[2][1]["MultipartUpload"] == {"Parts": [{"PartNumber": 1, "ETag": "etag-1"}]}
    store.abort_multipart_upload("local/runs/x/vmlinux", uid)
    assert client.calls[3][0] == "abort"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/store/test_objectstore.py -k multipart -q`
Expected: FAIL (`ObjectStore` has no `create_multipart_upload`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/store/objectstore.py  (methods on ObjectStore, after presign_get)
def create_multipart_upload(
    self, key: str, *, sensitivity: Sensitivity, retention_class: str
) -> str:
    """Initiate a multipart upload for ``key``, setting object metadata at create time.

    Metadata cannot be attached at completion, so the sensitivity/retention-class are set
    here and ride onto the reassembled object (ADR-0104 §4). No checksum algorithm is set,
    so the final object carries an ETag but no whole-object checksum. Returns the upload id.

    Raises:
        CategorizedError: the call fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    try:
        resp = self._client.create_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            Metadata={"sensitivity": sensitivity.value, "retention-class": retention_class},
        )
    except (BotoCoreError, ClientError) as err:
        raise _infrastructure_error("create_multipart_upload", key, err) from err
    return resp["UploadId"]


def upload_part_copy(
    self, key: str, upload_id: str, *, part_number: int, source_key: str
) -> str:
    """Copy ``source_key`` into part ``part_number`` of ``key``'s multipart upload.

    A server-side copy — no bytes transit the process. Returns the part ETag.

    Raises:
        CategorizedError: the copy fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    try:
        resp = self._client.upload_part_copy(
            Bucket=self._bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            CopySource={"Bucket": self._bucket, "Key": source_key},
        )
    except (BotoCoreError, ClientError) as err:
        raise _infrastructure_error("upload_part_copy", key, err) from err
    return _normalize_etag(resp["CopyPartResult"]["ETag"])


def complete_multipart_upload(
    self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
) -> str:
    """Complete ``key``'s multipart upload with the ordered ``(part_number, etag)`` list.

    Returns the final object ETag (a multipart ``-N`` form).

    Raises:
        CategorizedError: completion fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    multipart = {"Parts": [{"PartNumber": n, "ETag": etag} for n, etag in parts]}
    try:
        resp = self._client.complete_multipart_upload(
            Bucket=self._bucket, Key=key, UploadId=upload_id, MultipartUpload=multipart
        )
    except (BotoCoreError, ClientError) as err:
        raise _infrastructure_error("complete_multipart_upload", key, err) from err
    return _normalize_etag(resp["ETag"])


def abort_multipart_upload(self, key: str, upload_id: str) -> None:
    """Abort ``key``'s multipart upload (best-effort cleanup of a failed reassembly).

    Raises:
        CategorizedError: the abort fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    try:
        self._client.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
    except (BotoCoreError, ClientError) as err:
        raise _infrastructure_error("abort_multipart_upload", key, err) from err
```

Add `from collections.abc import Sequence` to the imports if absent.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/store/test_objectstore.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/store/objectstore.py tests/store/test_objectstore.py
git commit -m "feat(store): add multipart create/copy/complete/abort primitives"
```

---

## Task 5: Raise the per-artifact cap to 50 GiB

**Files:**
- Modify: `src/kdive/config/core_settings.py:157-161` (`MAX_UPLOAD_BYTES` default)
- Test: `tests/config/test_core_settings.py` (extend) or the config-docs guard

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_core_settings.py  (add)
from kdive.config.core_settings import MAX_UPLOAD_BYTES


def test_max_upload_default_is_50_gib():
    assert MAX_UPLOAD_BYTES.default == str(50 * 1024 * 1024 * 1024)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/config/test_core_settings.py -k max_upload -q`
Expected: FAIL (default is still 5 GiB).

- [ ] **Step 3: Write minimal implementation**

Change `MAX_UPLOAD_BYTES`'s `default=str(5 * 1024 * 1024 * 1024)` to `default=str(50 * 1024 * 1024 * 1024)` and update its `suggest=`/`help=` text to say 50 GiB.

- [ ] **Step 4: Run test + regenerate config docs**

Run: `uv run python -m pytest tests/config/test_core_settings.py -q && just config-docs-check`
If `config-docs-check` reports drift, run the documented regeneration recipe (`just config-docs` or equivalent) and re-run the check.
Expected: PASS, docs in sync.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/config/core_settings.py tests/config/test_core_settings.py docs/
git commit -m "feat(config): raise KDIVE_MAX_UPLOAD_BYTES default to 50 GiB"
```

---

## Task 6: Chunked declaration validation in `create_upload`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/uploads.py` — `_validate_artifact_declarations` (~line 87) and `_materialize_uploads` (~line 112)
- Test: `tests/mcp/lifecycle/test_create_upload_tool.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
# tests/mcp/lifecycle/test_create_upload_tool.py  (add; reuse the file's run/ctx/store harness)
import pytest

# Validation-only unit tests against the pure helper:
from kdive.mcp.tools.catalog.artifacts.uploads import _validate_artifact_declarations
from kdive.mcp.responses import ToolResponse
from kdive.provider_components.uploads import ManifestEntry

_ALLOWED = frozenset({"vmlinux", "kernel"})
_CAP = 50 * 1024 * 1024 * 1024
_5GIB = 5 * 1024 * 1024 * 1024


def test_single_over_5gib_rejected_size_out_of_range():
    out = _validate_artifact_declarations(
        "rid", [{"name": "vmlinux", "sha256": "a", "size_bytes": _5GIB + 1}], _ALLOWED, _CAP
    )
    assert isinstance(out, ToolResponse)
    assert out.data["reason"] == "size_out_of_range"


def test_chunked_well_formed_accepted():
    decl = {
        "name": "vmlinux", "sha256": "whole", "size_bytes": _5GIB + 100,
        "chunks": [
            {"sha256": "c0", "size_bytes": _5GIB},
            {"sha256": "c1", "size_bytes": 100},
        ],
    }
    out = _validate_artifact_declarations("rid", [decl], _ALLOWED, _CAP)
    assert isinstance(out, list)
    assert out[0].chunks is not None and len(out[0].chunks) == 2


def test_chunked_non_final_below_min_part_rejected():
    decl = {
        "name": "vmlinux", "sha256": "w", "size_bytes": 1024 + 10,
        "chunks": [{"sha256": "c0", "size_bytes": 1024}, {"sha256": "c1", "size_bytes": 10}],
    }
    out = _validate_artifact_declarations("rid", [decl], _ALLOWED, _CAP)
    assert isinstance(out, ToolResponse) and out.data["reason"] == "chunk_too_small"


def test_chunked_sum_mismatch_rejected():
    decl = {
        "name": "vmlinux", "sha256": "w", "size_bytes": 999,
        "chunks": [{"sha256": "c0", "size_bytes": _5GIB}, {"sha256": "c1", "size_bytes": 100}],
    }
    out = _validate_artifact_declarations("rid", [decl], _ALLOWED, _CAP)
    assert isinstance(out, ToolResponse) and out.data["reason"] == "chunk_size_mismatch"


def test_chunked_effective_config_rejected():
    decl = {"name": "effective_config", "sha256": "w", "size_bytes": 100,
            "chunks": [{"sha256": "c0", "size_bytes": 100}]}
    out = _validate_artifact_declarations(
        "rid", [decl], frozenset({"effective_config"}), _CAP
    )
    assert isinstance(out, ToolResponse) and out.data["reason"] == "size_out_of_range"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_create_upload_tool.py -k "chunk or out_of_range" -q`
Expected: FAIL (chunks ignored / `ManifestEntry` built without chunks).

- [ ] **Step 3: Write minimal implementation**

Replace the per-artifact body of `_validate_artifact_declarations` with a single + chunked split. Add a helper `_validate_chunks` returning `tuple[ChunkEntry, ...] | ToolResponse`:

```python
# uploads.py — imports
from kdive.provider_components.uploads import (
    ChunkEntry,
    ManifestEntry,
    MAX_PART_BYTES,
    MAX_PARTS,
    MIN_PART_BYTES,
    SINGLE_PUT_MAX_BYTES,
)


def _validate_chunks(
    object_id: str, raw_chunks: object, declared_total: int, cap: int
) -> tuple[ChunkEntry, ...] | ToolResponse:
    if not isinstance(raw_chunks, list) or not (1 <= len(raw_chunks) <= MAX_PARTS):
        return _config_error(object_id, data={"reason": "too_many_chunks"})
    chunks: list[ChunkEntry] = []
    total = 0
    last = len(raw_chunks) - 1
    for i, c in enumerate(raw_chunks):
        if not isinstance(c, dict):
            return _config_error(object_id, data={"reason": "bad_artifact_declaration"})
        csha, csize = c.get("sha256"), c.get("size_bytes")
        if not isinstance(csha, str) or not isinstance(csize, int) or csize <= 0:
            return _config_error(object_id, data={"reason": "bad_artifact_declaration"})
        if csize > MAX_PART_BYTES:
            return _config_error(object_id, data={"reason": "size_out_of_range"})
        if i != last and csize < MIN_PART_BYTES:
            return _config_error(object_id, data={"reason": "chunk_too_small"})
        chunks.append(ChunkEntry(sha256=csha, size_bytes=csize))
        total += csize
    if total != declared_total or not (0 < declared_total <= cap):
        return _config_error(object_id, data={"reason": "chunk_size_mismatch"})
    return tuple(chunks)
```

Then in `_validate_artifact_declarations`, after the existing name/sha/size type checks, branch on `art.get("chunks")`:

```python
    raw_chunks = art.get("chunks")
    artifact_cap = _EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES if name == "effective_config" else cap
    if raw_chunks is None:
        if size <= 0 or size > min(SINGLE_PUT_MAX_BYTES, artifact_cap):
            return _config_error(object_id, data={"reason": "size_out_of_range"})
        entries.append(ManifestEntry(name=name, sha256=sha256, size_bytes=size))
    else:
        if name == "effective_config":
            return _config_error(object_id, data={"reason": "size_out_of_range"})
        validated_chunks = _validate_chunks(object_id, raw_chunks, size, artifact_cap)
        if isinstance(validated_chunks, ToolResponse):
            return validated_chunks
        entries.append(
            ManifestEntry(name=name, sha256=sha256, size_bytes=size, chunks=validated_chunks)
        )
```

Note: keep the existing `isinstance(size, int)` guard before this branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_create_upload_tool.py -q`
Expected: PASS (all, including pre-existing single-artifact tests).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/mcp/tools/catalog/artifacts/uploads.py tests/mcp/lifecycle/test_create_upload_tool.py
git commit -m "feat(create-upload): validate chunked artifact declarations"
```

---

## Task 7: Per-chunk presign in `_materialize_uploads`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/uploads.py` — `_materialize_uploads` (~line 112) and `_upload_response` (~line 256)
- Test: `tests/mcp/lifecycle/test_create_upload_tool.py` (extend, full handler path)

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/lifecycle/test_create_upload_tool.py  (add, using the file's create_run_upload harness)
@pytest.mark.asyncio
async def test_chunked_artifact_mints_one_url_per_chunk(... existing harness fixtures ...):
    # Declare a chunked vmlinux with two parts; drive create_run_upload with a recording store.
    # Assert: two upload items, ids end .part0001 and .part0002, each data carries
    #   artifact_name == "vmlinux" and part_number 1, 2; the recording store saw two
    #   presign_put calls with the two chunk sha256 values.
    ...
```

(Mirror the existing recording-store fixture in this file; assert on `_RecordingStore.requests` sha256 values and the returned collection items.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_create_upload_tool.py -k chunked_artifact_mints -q`
Expected: FAIL (one URL minted at `<name>`, not per chunk).

- [ ] **Step 3: Write minimal implementation**

```python
# uploads.py — _materialize_uploads: branch on entry.chunks
from kdive.provider_components.artifacts import chunk_key  # add import

def _materialize_uploads(entries, *, kind, owner_id, store):
    uploads: list[_MaterializedUpload] = []
    expires_in = _presign_ttl_seconds()
    prefix = owner_prefix(_TENANT, kind, str(owner_id))
    for entry in entries:
        if entry.chunks is None:
            key = artifact_key(_TENANT, kind, str(owner_id), entry.name)
            uploads.append(_materialize_one(store, key, entry.sha256, entry.size_bytes,
                                             entry, part_number=None, expires_in=expires_in))
        else:
            for i, c in enumerate(entry.chunks, start=1):
                key = chunk_key(prefix, entry.name, i)
                uploads.append(_materialize_one(store, key, c.sha256, c.size_bytes,
                                                entry, part_number=i, expires_in=expires_in))
    return uploads


def _materialize_one(store, key, sha256, size_bytes, entry, *, part_number, expires_in):
    presigned = store.presign_put(
        PresignPutRequest(
            key=key, sha256=sha256, size_bytes=size_bytes,
            sensitivity=Sensitivity.SENSITIVE, retention_class=_RETENTION_CLASS,
            expires_in=expires_in,
        )
    )
    return _MaterializedUpload(entry, key, presigned, part_number)
```

Extend `_MaterializedUpload` with a `part_number: int | None` field. In `_upload_response`, add to `data`:

```python
        data={
            "name": upload.entry.name,
            "artifact_name": upload.entry.name,
            "expires_in": str(_presign_ttl_seconds()),
            **({"part_number": str(upload.part_number)} if upload.part_number is not None else {}),
            **upload.presigned.required_headers,
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_create_upload_tool.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/mcp/tools/catalog/artifacts/uploads.py tests/mcp/lifecycle/test_create_upload_tool.py
git commit -m "feat(create-upload): mint one presigned PUT per chunk at partNNNN keys"
```

---

## Task 8: Chunked head-verify in validation (skip whole-object checksum)

**Files:**
- Modify: `src/kdive/provider_components/build_validation.py` — `_validate_one_artifact` (~line 211) and a new `verify_chunks` helper
- Test: `tests/providers/local_libvirt/test_validate_external_artifacts.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/local_libvirt/test_validate_external_artifacts.py  (add)
# A fake ValidatorStore that returns HeadResult per chunk key and final key, plus ranged magic.
def test_chunked_entry_verifies_each_chunk_and_skips_whole_object_checksum(fake_store):
    # manifest: vmlinux chunked (c0,c1) + kernel single; fake_store heads each chunk with the
    # declared sha256/size, and heads the final vmlinux/kernel keys (final has composite/None
    # checksum). Expect validate to pass: per-chunk checks succeed, final whole-object checksum
    # is NOT compared for the chunked entry.
    ...


def test_chunked_entry_chunk_checksum_mismatch_is_build_failure(fake_store):
    # one chunk head returns a different sha256 -> CategorizedError BUILD_FAILURE before reassembly.
    ...
```

(Mirror the fake-store pattern already in this test file; cover a chunk size mismatch and a missing chunk too.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_validate_external_artifacts.py -k chunk -q`
Expected: FAIL (no chunk verification path).

- [ ] **Step 3: Write minimal implementation**

Add a `verify_chunks(store, prefix, entry)` function that HEADs each `chunk_key(prefix, entry.name, i)`, asserting `head.size_bytes == c.size_bytes and head.checksum_sha256 == c.sha256`, raising `CONFIGURATION_ERROR` for a missing chunk and `BUILD_FAILURE` for a mismatch. In `_validate_one_artifact`, when `entry.chunks is not None`, verify the **final** object's size only (`head.size_bytes == entry.size_bytes`) and skip the `head.checksum_sha256 == entry.sha256` comparison (the reassembled object's checksum is composite/None). Keep magic checks unchanged (they run on the final object). The chunk HEAD-verification itself is invoked from the reassembly helper (Task 9) before reassembly; `_validate_one_artifact` for a chunked entry runs on the final reassembled object.

```python
# build_validation.py
from kdive.provider_components.artifacts import chunk_key
from kdive.provider_components.uploads import ManifestEntry


def verify_chunks(store: ValidatorStore, prefix: str, entry: ManifestEntry) -> None:
    """HEAD-verify each declared chunk's stored (size, sha256) before reassembly (ADR-0104 §4)."""
    assert entry.chunks is not None
    for i, c in enumerate(entry.chunks, start=1):
        key = chunk_key(prefix, entry.name, i)
        head = store.head(key)
        if head is None:
            raise CategorizedError(
                f"declared chunk {i} of {entry.name!r} was never uploaded",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"name": entry.name, "part_number": i},
            )
        if head.size_bytes != c.size_bytes or head.checksum_sha256 != c.sha256:
            raise _build_failure(
                "uploaded chunk disagrees with its manifest", name=entry.name, part_number=i
            )
```

```python
# build_validation.py — _validate_one_artifact
def _validate_one_artifact(store, name, entry, key):
    head = store.head(key)
    if head is None:
        raise CategorizedError(
            f"declared artifact {name!r} was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": name},
        )
    if entry.chunks is None:
        if head.size_bytes != entry.size_bytes or head.checksum_sha256 != entry.sha256:
            raise _build_failure("uploaded artifact disagrees with its manifest", name=name)
    else:
        if head.size_bytes != entry.size_bytes:
            raise _build_failure("reassembled artifact size disagrees with its manifest", name=name)
    _check_magic(store, name, key)
    return head
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_validate_external_artifacts.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/provider_components/build_validation.py tests/providers/local_libvirt/test_validate_external_artifacts.py
git commit -m "feat(build-validation): per-chunk head-verify, skip whole-object checksum for chunked"
```

---

## Task 9: Reassembly orchestration helper

**Files:**
- Create: `src/kdive/provider_components/reassembly.py`
- Test: `tests/provider_components/test_reassembly.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/provider_components/test_reassembly.py
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.reassembly import ReassemblyStore, reassemble_chunked
from kdive.provider_components.uploads import ChunkEntry, ManifestEntry


class _FakeStore:
    def __init__(self, fail_copy_at=None):
        self.events = []
        self._fail_copy_at = fail_copy_at

    def head(self, key):
        from kdive.provider_components.artifacts import HeadResult
        # chunk heads match the manifest declared below
        sizes = {".part0001": (6, "c0"), ".part0002": (4, "c1")}
        for suffix, (size, sha) in sizes.items():
            if key.endswith(suffix):
                return HeadResult(size_bytes=size, checksum_sha256=sha, etag="e")
        return None

    def create_multipart_upload(self, key, *, sensitivity, retention_class):
        self.events.append(("create", key))
        return "uid"

    def upload_part_copy(self, key, upload_id, *, part_number, source_key):
        if self._fail_copy_at == part_number:
            raise CategorizedError("boom", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        self.events.append(("copy", part_number, source_key))
        return f"etag-{part_number}"

    def complete_multipart_upload(self, key, upload_id, parts):
        self.events.append(("complete", tuple(parts)))
        return "final"

    def abort_multipart_upload(self, key, upload_id):
        self.events.append(("abort", key))


def _entry():
    return ManifestEntry("vmlinux", "whole", 10,
                         chunks=(ChunkEntry("c0", 6), ChunkEntry("c1", 4)))


def test_reassemble_verifies_copies_in_order_completes():
    store = _FakeStore()
    reassemble_chunked(store, prefix="local/runs/x/", final_key="local/runs/x/vmlinux",
                       entry=_entry())
    kinds = [e[0] for e in store.events]
    assert kinds == ["create", "copy", "copy", "complete"]
    assert store.events[1][1] == 1 and store.events[2][1] == 2
    assert store.events[3][1] == ((1, "etag-1"), (2, "etag-2"))


def test_reassemble_aborts_on_copy_failure():
    store = _FakeStore(fail_copy_at=2)
    with pytest.raises(CategorizedError):
        reassemble_chunked(store, prefix="local/runs/x/", final_key="local/runs/x/vmlinux",
                           entry=_entry())
    assert ("abort", "local/runs/x/vmlinux") in store.events
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/provider_components/test_reassembly.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/provider_components/reassembly.py
"""Server-side reassembly of a chunked artifact into one object (ADR-0104 §1, §4)."""

from __future__ import annotations

from typing import Protocol

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import chunk_key
from kdive.provider_components.build_validation import verify_chunks
from kdive.provider_components.uploads import ManifestEntry


class ReassemblyStore(Protocol):
    """The object-store ops reassembly needs (HEAD + the four multipart primitives)."""

    def head(self, key: str): ...
    def create_multipart_upload(self, key: str, *, sensitivity: Sensitivity, retention_class: str) -> str: ...
    def upload_part_copy(self, key: str, upload_id: str, *, part_number: int, source_key: str) -> str: ...
    def complete_multipart_upload(self, key: str, upload_id: str, parts) -> str: ...
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


def reassemble_chunked(
    store: ReassemblyStore, *, prefix: str, final_key: str, entry: ManifestEntry
) -> None:
    """HEAD-verify each chunk, then Create/UploadPartCopy/Complete the final object.

    Aborts the multipart upload on any failure (the caller maps that to a typed error and the
    reaper backstops the chunks). The caller runs whole-object validation on ``final_key``
    after this returns.
    """
    assert entry.chunks is not None
    verify_chunks(store, prefix, entry)
    upload_id = store.create_multipart_upload(
        final_key, sensitivity=Sensitivity.SENSITIVE, retention_class="build"
    )
    try:
        parts: list[tuple[int, str]] = []
        for i in range(1, len(entry.chunks) + 1):
            etag = store.upload_part_copy(
                final_key, upload_id, part_number=i, source_key=chunk_key(prefix, entry.name, i)
            )
            parts.append((i, etag))
        store.complete_multipart_upload(final_key, upload_id, parts)
    except BaseException:
        store.abort_multipart_upload(final_key, upload_id)
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/provider_components/test_reassembly.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/provider_components/reassembly.py tests/provider_components/test_reassembly.py
git commit -m "feat(reassembly): server-side chunk reassembly with abort-on-failure"
```

---

## Task 10a: Window guard + reassembly in `complete_build`

**Files:**
- Modify: `src/kdive/db/upload_manifest.py` — add `refresh_deadline(...)`.
- Modify: `src/kdive/mcp/tools/lifecycle/runs/build.py` — `complete_build` (~line 210): add the window guard + reassembly before validation; add imports.
- Test: `tests/mcp/lifecycle/test_complete_build_tool.py` (extend)

**Prereq context:** the existing `complete_build` body runs, in order: `require_role` → `_existing_build_result` (idempotent replay: an already-`SUCCEEDED` Run returns the recorded envelope and never reaches the guard) → `_external_build_profile` → `_created_run_guard` → `get_manifest` → `_validate_complete_build` (to_thread) → `_finalize_external_build`. The window guard + reassembly insert **between `get_manifest` and `_validate_complete_build`**, only when the manifest has a chunked entry. The single-PUT path is byte-identical to today.

- [ ] **Step 1: Write the failing tests**

```python
# tests/mcp/lifecycle/test_complete_build_tool.py  (add, using the file's harness + fake store)
@pytest.mark.asyncio
async def test_chunked_complete_build_reassembles_and_succeeds(...):
    # CREATED external Run with a chunked vmlinux manifest; fake store HEADs chunks, records
    # create/copy/complete, HEADs final keys, ranged magic ok. Assert: Run SUCCEEDED, one
    # artifacts row at the final vmlinux key (Task 10b asserts chunk deletion).
    ...


@pytest.mark.asyncio
async def test_complete_build_rejects_expired_window(...):
    # chunked manifest with deadline in the past -> upload_window_expired, no reassembly
    # calls (the fake store records zero create_multipart_upload), Run stays CREATED.
    ...


@pytest.mark.asyncio
async def test_reassembly_failure_after_concurrent_success_returns_recorded(...):
    # Run already SUCCEEDED + chunks gone -> upload_part_copy raises; complete_build re-checks
    # run state via _existing_build_result and returns the recorded success envelope, not error.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_complete_build_tool.py -k "chunked or expired or concurrent" -q`
Expected: FAIL.

- [ ] **Step 3a: Add `refresh_deadline` to `upload_manifest.py`**

```python
# src/kdive/db/upload_manifest.py
async def refresh_deadline(
    conn: AsyncConnection, owner_kind: str, owner_id: UUID, ttl: timedelta
) -> bool:
    """Set ``deadline = now() + ttl`` if a non-expired manifest exists; return whether it did.

    Returns False when no row exists OR the current deadline is already past (the caller
    treats the latter as an expired upload window, ADR-0104 §6 step A). ``timedelta`` is
    already imported by this module.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE upload_manifests SET deadline = now() + %s "
            "WHERE owner_kind = %s AND owner_id = %s AND deadline >= now()",
            (ttl, owner_kind, owner_id),
        )
        return cur.rowcount == 1
```

- [ ] **Step 3b: Add imports + the store Protocol + factory retype to `build.py`**

The current `ObjectStoreFactory = Callable[[], ValidatorStore]` (build.py:61) returns a
`ValidatorStore` exposing only `head`/`get_range`, so the reassembly + cleanup code below
(which needs `delete` and the four multipart methods) would fail `ty`. Define one Protocol
covering everything `complete_build` needs from the store and retype the factory to it. The
concrete `ObjectStore` satisfies it structurally once Task 4 lands.

```python
# src/kdive/mcp/tools/lifecycle/runs/build.py  (top-of-file imports)
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

import kdive.config as config
from kdive.config.core_settings import UPLOAD_TTL_SECONDS
from kdive.provider_components.artifacts import HeadResult, chunk_key
from kdive.provider_components.reassembly import reassemble_chunked
from kdive.provider_components.uploads import ManifestEntry

_log = logging.getLogger(__name__)


class ExternalBuildStore(Protocol):
    """Object-store surface the external-build finalize path needs (ADR-0104).

    A superset of ``ValidatorStore`` (head + get_range) plus ``delete`` and the four
    multipart primitives, so a single factory return type covers validation, reassembly, and
    chunk cleanup. The concrete :class:`~kdive.store.objectstore.ObjectStore` satisfies it.
    """

    def head(self, key: str) -> HeadResult | None: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def create_multipart_upload(self, key: str, *, sensitivity, retention_class: str) -> str: ...
    def upload_part_copy(self, key: str, upload_id: str, *, part_number: int, source_key: str) -> str: ...
    def complete_multipart_upload(self, key: str, upload_id: str, parts) -> str: ...
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...
```

Then retype the factory alias (build.py:61):

```python
type ObjectStoreFactory = Callable[[], ExternalBuildStore]
```

`StoredArtifact`/`HeadResult` are already imported from `provider_components.artifacts`
(merge the `chunk_key` addition into that line). `asyncio`, `advisory_xact_lock`,
`LockScope`, `upload_manifest`, `_existing_build_result`, `_complete_envelope`,
`object_store_from_env` are already imported. `reassemble_chunked` accepts a
`ReassemblyStore` (Task 9); `ExternalBuildStore` is a structural superset, so passing one is
type-safe.

- [ ] **Step 3c: Insert the guard + reassembly in `complete_build`, replacing the current `get_manifest` block**

The current block is:

```python
                manifest_row = await upload_manifest.get_manifest(conn, "runs", uid)
                if manifest_row is None:
                    return _config_error(run_id, data={"reason": "no_upload_manifest"})
                keys = {e.name: f"{manifest_row.prefix}{e.name}" for e in manifest_row.entries}
```

Replace it with:

```python
                manifest_row = await upload_manifest.get_manifest(conn, "runs", uid)
                if manifest_row is None:
                    return _config_error(run_id, data={"reason": "no_upload_manifest"})
                has_chunks = any(e.chunks is not None for e in manifest_row.entries)
                keys = {e.name: f"{manifest_row.prefix}{e.name}" for e in manifest_row.entries}
                if has_chunks:
                    guard = await _reassemble_chunked_artifacts(
                        conn, uid, run_id, manifest_row, self.object_store_factory()
                    )
                    if guard is not None:
                        return guard
```

Then add the helper to the module:

```python
async def _reassemble_chunked_artifacts(
    conn: AsyncConnection,
    uid: UUID,
    run_id: str,
    manifest_row: upload_manifest.UploadManifest,
    store: ExternalBuildStore,
) -> ToolResponse | None:
    """Refresh the upload window under the per-Run lock, then reassemble each chunked artifact.

    Returns a failure/recorded-success ``ToolResponse`` to short-circuit ``complete_build``, or
    ``None`` to proceed to validation+finalize on the now-single final keys (ADR-0104 §6).
    """
    ttl = timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, uid):
        refreshed = await upload_manifest.refresh_deadline(conn, "runs", uid, ttl)
    if not refreshed:
        if await upload_manifest.get_manifest(conn, "runs", uid) is None:
            return _config_error(run_id, data={"reason": "no_upload_manifest"})
        return _config_error(run_id, data={"reason": "upload_window_expired"})
    prefix = manifest_row.prefix
    try:
        for entry in manifest_row.entries:
            if entry.chunks is not None:
                await asyncio.to_thread(
                    reassemble_chunked,
                    store,
                    prefix=prefix,
                    final_key=f"{prefix}{entry.name}",
                    entry=entry,
                )
    except CategorizedError as exc:
        recorded = await _existing_build_result(conn, uid)  # a concurrent finalize won?
        if recorded is not None:
            return _complete_envelope(uid, recorded)
        return ToolResponse.failure_from_error(run_id, exc)
    return None
```

`keys` already holds final keys, so the existing `_validate_complete_build` call (Task 8 path) runs unchanged on the reassembled objects.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_complete_build_tool.py -k "chunked or expired or concurrent" -q`
Expected: PASS (chunk-deletion assertion arrives in Task 10b).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/mcp/tools/lifecycle/runs/build.py src/kdive/db/upload_manifest.py tests/mcp/lifecycle/test_complete_build_tool.py
git commit -m "feat(complete-build): window guard + reassembly for chunked uploads"
```

---

## Task 10b: Deferred manifest delete + post-commit chunk cleanup in `_finalize_external_build`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/build.py` — `_finalize_external_build` (~line 327): take `entries`/`prefix`/`chunked`, skip in-txn manifest delete when chunked, add post-commit cleanup; update the `complete_build` call site.
- Test: `tests/mcp/lifecycle/test_complete_build_tool.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
# tests/mcp/lifecycle/test_complete_build_tool.py  (add)
@pytest.mark.asyncio
async def test_chunked_finalize_deletes_chunks_and_manifest(...):
    # After a successful chunked complete_build: the fake store recorded delete() for
    # vmlinux.part0001 and .part0002, and the upload_manifests row is gone.
    ...


@pytest.mark.asyncio
async def test_chunked_finalize_chunk_delete_failure_keeps_manifest(...):
    # store.delete raises on .part0001 -> the Run still SUCCEEDED with its artifacts row, and
    # the manifest row REMAINS (so the reaper reclaims the leftover chunk later).
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_complete_build_tool.py -k "finalize_deletes or delete_failure" -q`
Expected: FAIL (chunks not deleted; manifest deleted in-txn regardless).

- [ ] **Step 3: Rewrite `_finalize_external_build` and its call site**

Current signature: `async def _finalize_external_build(conn, ctx, run, output, cmdline, keys, heads) -> ToolResponse`, and its transaction body ends with `await upload_manifest.delete_manifest(conn, "runs", run.id)`. Replace with:

```python
async def _finalize_external_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    output: BuildOutput,
    cmdline: str,
    keys: dict[str, str],
    heads: dict[str, HeadResult],
    *,
    store: ExternalBuildStore,
    entries: Sequence[ManifestEntry],
    prefix: str,
    chunked: bool,
) -> ToolResponse:
    """Write artifact rows, ledger result, and created -> succeeded under the per-Run lock.

    For a single-PUT manifest the upload manifest is deleted in the finalize transaction (as
    before). For a chunked manifest the manifest is kept until the post-commit best-effort
    chunk cleanup finishes, so a failed cleanup leaves the manifest for the reaper to reclaim
    the leftover chunks (ADR-0104 §6).
    """
    result = BuildStepResult(
        kernel_ref=output.kernel_ref,
        debuginfo_ref=output.debuginfo_ref,
        initrd_ref=keys.get("initrd"),
        build_id=output.build_id,
        cmdline=cmdline,
    )
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(run.id))
        state = RunState(row["state"])
        if state is RunState.SUCCEEDED:
            recorded = await _existing_build_result(conn, run.id)
            return _complete_envelope(run.id, recorded or result)
        if state is not RunState.CREATED:
            return _config_error(str(run.id), data={"current_status": state.value})
        for name, head in heads.items():
            stored = StoredArtifact(keys[name], head.etag, Sensitivity.SENSITIVE, "build")
            await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="runs", owner_id=run.id)
            )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result.dump())),
        )
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = %s "
            "WHERE id = %s AND state = %s",
            (
                output.kernel_ref,
                output.debuginfo_ref or None,
                RunState.SUCCEEDED.value,
                run.id,
                RunState.CREATED.value,
            ),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.complete_build",
                object_kind="runs",
                object_id=run.id,
                transition="created->succeeded",
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
        if not chunked:
            await upload_manifest.delete_manifest(conn, "runs", run.id)
    if chunked:
        await _cleanup_chunks_and_manifest(conn, store, run.id, entries, prefix)
    return _complete_envelope(run.id, result)


async def _cleanup_chunks_and_manifest(
    conn: AsyncConnection,
    store: ExternalBuildStore,
    run_id: UUID,
    entries: Sequence[ManifestEntry],
    prefix: str,
) -> None:
    """Best-effort post-commit reclamation of the reassembled chunks, then the manifest.

    A failure here never fails the already-finalized build. The manifest is deleted LAST and
    only when every chunk delete succeeded, so a failed chunk delete leaves the manifest for
    the reaper to reclaim the leftover chunks (ADR-0104 §5/§6).
    """
    for entry in entries:
        if entry.chunks is None:
            continue
        for i in range(1, len(entry.chunks) + 1):
            key = chunk_key(prefix, entry.name, i)
            try:
                await asyncio.to_thread(store.delete, key)
            except CategorizedError as exc:
                _log.warning("chunk cleanup failed for %s: %s", key, exc)
                return
    try:
        await upload_manifest.delete_manifest(conn, "runs", run_id)
    except CategorizedError as exc:
        _log.warning("manifest cleanup failed for run %s: %s", run_id, exc)
```

(Imports `Sequence`, `chunk_key`, `ManifestEntry`, `logging`/`_log`, and the
`ExternalBuildStore` Protocol were all added in Task 10a Step 3b — no new imports here.)

Update the call site in `complete_build` (the final `return`):

```python
                return await _finalize_external_build(
                    conn, ctx, run, validated.output, cmdline, keys, validated.heads,
                    store=self.object_store_factory(),
                    entries=manifest_row.entries,
                    prefix=manifest_row.prefix,
                    chunked=has_chunks,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_complete_build_tool.py -q`
Expected: PASS (all, including the pre-existing single-PUT finalize tests — they pass `chunked=False` and keep in-txn manifest delete).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/mcp/tools/lifecycle/runs/build.py tests/mcp/lifecycle/test_complete_build_tool.py
git commit -m "feat(complete-build): defer manifest delete + post-commit chunk cleanup"
```

---

## Task 11: Reaper `runs`-branch gate drop

**Files:**
- Modify: `src/kdive/reconciler/uploads.py` — `repair_abandoned_uploads` candidate query (~line 37) and `reap_one_owner` `owner_pre_finalize` recheck (~line 74)
- Test: `tests/reconciler/test_upload_reaper.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
# tests/reconciler/test_upload_reaper.py  (add)
@pytest.mark.asyncio
async def test_succeeded_run_with_lingering_manifest_reaps_chunks_not_final(...):
    # Run SUCCEEDED, manifest past deadline, objects: <name> (HAS artifacts row) and
    # <name>.part0001 (NO row). Reaper deletes the chunk, keeps <name>, deletes the manifest.
    ...


@pytest.mark.asyncio
async def test_finalized_system_with_lingering_manifest_is_not_reaped(...):
    # System advanced past DEFINED with a past-deadline manifest -> NOT swept (DEFINED gate kept).
    ...
```

(Keep/confirm the existing pre-finalize abandon test still passes unchanged.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/reconciler/test_upload_reaper.py -k "succeeded or finalized_system" -q`
Expected: FAIL (succeeded Run not selected; or system wrongly swept if over-broad).

- [ ] **Step 3: Write minimal implementation**

Change the candidate query so the `runs` arm drops its `CREATED` predicate while the `systems` arm keeps `DEFINED`:

```python
await cur.execute(
    "SELECT m.owner_kind, m.owner_id FROM upload_manifests m "
    "WHERE m.deadline < now() AND ("
    "  m.owner_kind = %s "  # runs: any state
    "  OR (m.owner_kind = %s AND EXISTS ("
    "     SELECT 1 FROM systems s WHERE s.id = m.owner_id AND s.state = %s)))",
    (
        _UPLOAD_RUN_OWNER_KIND,
        _UPLOAD_SYSTEM_OWNER_KIND,
        _UPLOAD_PRE_FINALIZE_VALUES[_UPLOAD_SYSTEM_OWNER_KIND],
    ),
)
```

In `reap_one_owner`, make the `owner_pre_finalize` recheck apply to `systems` only:

```python
        if cand_kind == _UPLOAD_SYSTEM_OWNER_KIND and not await owner_pre_finalize(
            conn, owner_kind, owner_id
        ):
            return False
```

(Use the `owner_kind` already in scope; the `runs` branch skips the recheck so a SUCCEEDED Run is swept. The per-object no-row predicate still protects the committed final object.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/reconciler/test_upload_reaper.py -q`
Expected: PASS (including the unchanged pre-finalize abandon test).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type && git add src/kdive/reconciler/uploads.py tests/reconciler/test_upload_reaper.py
git commit -m "feat(reaper): sweep finalized Run leaked chunks; keep System DEFINED gate"
```

---

## Task 12: Adversarial concurrent-finalize test

**Files:**
- Test: `tests/adversarial/test_complete_build_concurrency.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/adversarial/test_complete_build_concurrency.py  (add)
@pytest.mark.asyncio
async def test_two_overlapping_chunked_complete_builds_both_succeed(...):
    # Run two complete_build calls for one chunked CREATED Run concurrently (asyncio.gather),
    # with a store whose chunk delete races the second call's upload_part_copy. Assert: both
    # responses are success envelopes, exactly one artifacts row at the final key, Run SUCCEEDED.
    ...
```

- [ ] **Step 2: Run test to verify it fails (or is flaky) without the re-check**

Run: `uv run python -m pytest tests/adversarial/test_complete_build_concurrency.py -k overlapping_chunked -q`
Expected: With Task 10's re-check in place it PASSES; temporarily stub out the re-check to confirm it would FAIL (the loser returns an error). Restore the re-check.

- [ ] **Step 3: (No new impl — Task 10 already implements the re-check.)**

- [ ] **Step 4: Run the full adversarial file**

Run: `uv run python -m pytest tests/adversarial/test_complete_build_concurrency.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/adversarial/test_complete_build_concurrency.py
git commit -m "test(adversarial): overlapping chunked complete_build both succeed"
```

---

## Task 13: Runbook note for the bucket lifecycle rule

**Files:**
- Modify: the live-stack / object-store runbook under `docs/runbooks/` (whichever documents S3/MinIO bucket setup)

- [ ] **Step 1: Add an operator step** documenting the required `AbortIncompleteMultipartUpload` lifecycle rule (reclaims an orphaned reassembly MPU after a server crash mid-reassembly; ADR-0104 §4). Include an example `aws s3api put-bucket-lifecycle-configuration` / `mc ilm` snippet with a 1-day expiry for incomplete multipart uploads.

- [ ] **Step 2: Doc guardrails**

Run: `just docs-check && just check-mermaid`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/
git commit -m "docs(runbook): require AbortIncompleteMultipartUpload bucket lifecycle rule"
```

---

## Final verification

- [ ] Run the full local gate: `just lint && just type && just test`. Expected: all green.
- [ ] Confirm `just config-docs-check`, `just docs-check`, `just config-guard` pass (Task 5 touched config + docs).
- [ ] Grep for accidental relative imports or leftover TODOs in the touched files.

---

## Self-Review (completed by plan author)

**Spec coverage:** §2 cap → Task 5; ChunkEntry/ManifestEntry → Task 1; chunk_key → Task 2; JSONB → Task 3; store primitives → Task 4; declaration validation → Task 6; per-chunk presign → Task 7; head-verify/skip-checksum → Task 8; reassembly + abort → Task 9; window guard + idempotent re-check → Task 10a; deferred manifest delete + post-commit cleanup → Task 10b; reaper runs-only → Task 11; concurrent idempotency test → Task 12; lifecycle-rule runbook → Task 13. §9 verification bullets map to Tasks 6/8/9/10a/10b/11/12. No spec requirement is left without a task.

**Type consistency:** `ManifestEntry(name, sha256, size_bytes, chunks=None)` and `ChunkEntry(sha256, size_bytes)` are used identically in Tasks 1, 3, 6, 8, 9, 10a, 10b. `chunk_key(prefix, name, part_number)` signature is identical in Tasks 2, 7, 8, 9, 10b. `reassemble_chunked(store, *, prefix, final_key, entry)` is identical in Tasks 9 and 10a. `refresh_deadline(conn, owner_kind, owner_id, ttl) -> bool` is consistent in Task 10a. The TTL in build.py is `timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))` (Task 10a Step 3b), not a cross-module `_upload_ttl` import. The store-method names match Task 4's definitions everywhere. The `ExternalBuildStore` Protocol (Task 10a Step 3b) is the single store type for `_reassemble_chunked_artifacts`, `_finalize_external_build`, and `_cleanup_chunks_and_manifest`; it is a structural superset of Task 9's `ReassemblyStore` and Task 8's `ValidatorStore`, so the concrete `ObjectStore` (after Task 4) satisfies all three and `just type` passes.

**Open implementation note for the executor:** Task 10a's window-guard transaction (`_reassemble_chunked_artifacts`) and Task 10b's `_finalize_external_build` both take `LockScope.RUN`; keep them as separate short transactions (do not nest), and confirm the single-PUT lane skips the deadline refresh (no chunked entry ⇒ `_reassemble_chunked_artifacts` not called) and passes `chunked=False` so its in-transaction manifest delete and overall behavior are byte-identical to today.
