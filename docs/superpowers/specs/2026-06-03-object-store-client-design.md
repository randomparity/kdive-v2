# Object-store Client — Design

**Issue:** #8 (M0) · **Depends on:** #6 (schema + migration runner, merged), #7
(repository layer, merged) · **Decisions:**
[ADR-0017](../../adr/0017-object-store-client-interface.md), building on
[ADR-0005](../../adr/0005-postgres-object-store-state.md) and
[ADR-0013](../../adr/0013-object-store-layout-retention.md) · **Parent spec:**
[`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)

## Goal

The S3-compatible artifact-storage client for the M0 walking skeleton: write a
bulk artifact to the object store under the spec's key scheme with its
sensitivity/retention recorded on the object, and read it back with an
etag-consistency check. One new module, `src/kdive/store/objectstore.py`, and its
test, `tests/store/test_objectstore.py`.

This layer sits between the S3-compatible store below it and the worker
(`capture_vmcore`, build outputs) and `artifacts.get` handler above it (later
issues). It owns *how an object is written and fetched*; it does not own *when*, nor
the `artifacts` row's commit (that is the caller's transaction, #24), nor whether a
fetched object may reach a response (that is the handler's redaction policy).

## Non-goals

- **No `artifacts` row insert/commit.** `register_artifact_row` constructs the row;
  the caller (#24) inserts and commits it after the object write
  ([ADR-0005](../../adr/0005-postgres-object-store-state.md) write-before-commit).
- **No bucket lifecycle / retention-rule configuration.** The client records the
  retention class on the object; lifecycle rules are an ops issue
  ([ADR-0013](../../adr/0013-object-store-layout-retention.md)).
- **No redaction enforcement.** The client is policy-neutral; the handler refuses
  `sensitive` objects (it has `sensitivity` from `get_artifact`).
- **No write-once guard.** Not in issue scope; see ADR-0017 rejected alternatives.
- **No reconciler GC of orphan objects.** That consumes this layer later
  ([m0 spec](../../specs/m0-walking-skeleton.md) "Reconciler").
- **No async S3 client.** Synchronous boto3; async callers use `asyncio.to_thread`.

## Components

### `objectstore.py` — the S3 client

```python
class StoredArtifact(NamedTuple):
    key: str
    etag: str

class FetchedArtifact(NamedTuple):
    data: bytes
    sensitivity: Sensitivity
    retention_class: str

class ObjectStore:
    def __init__(self, client: S3Client, bucket: str) -> None: ...

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
    ) -> StoredArtifact: ...

    def get_artifact(self, key: str, etag: str) -> FetchedArtifact: ...

def object_store_from_env() -> ObjectStore: ...

def register_artifact_row(
    stored: StoredArtifact,
    *,
    owner_kind: str,
    owner_id: UUID,
    sensitivity: Sensitivity,
    retention_class: str,
) -> Artifact: ...
```

`put_artifact` returns `StoredArtifact`, a 2-field `NamedTuple` that unpacks as
`(key, etag)` (the issue's documented return) while giving attribute access.
`get_artifact` returns `FetchedArtifact` so the handler gets `sensitivity` without a
second `HeadObject`.

**Config.** `object_store_from_env()` reads `KDIVE_S3_ENDPOINT_URL` and
`KDIVE_S3_BUCKET`, failing fast with `CategorizedError(CONFIGURATION_ERROR)` if
either is unset (mirroring `db.pool.database_url`). It builds
`boto3.client("s3", endpoint_url=...)` and lets boto3's default chain resolve
credentials and region (M0/MinIO via the standard `AWS_*` env vars); no credential
handling is reinvented.

**Key construction & validation.** A private `_artifact_key(tenant, kind,
object_id, name)` joins the four components with `/`. Each component is validated
first: non-empty, no `/`, no control character (`ord(c) < 0x20`). A violation
raises `CategorizedError(CONFIGURATION_ERROR)` naming the offending component,
before any S3 call. This keeps the prefix unambiguous and forecloses key injection;
it is **not** the tenant-isolation boundary
([ADR-0013](../../adr/0013-object-store-layout-retention.md): isolation is
access-control).

**`put_artifact`.** Builds the key, then `put_object(Bucket=bucket, Key=key,
Body=data, Metadata={"sensitivity": sensitivity.value, "retention-class":
retention_class})`. Returns `StoredArtifact(key, _normalize_etag(resp["ETag"]))`.
S3 returns the ETag wrapped in double quotes; `_normalize_etag` strips them so the
stored etag matches what a later conditional GET compares against.

**`get_artifact`.** Issues `get_object(Bucket=bucket, Key=key, IfMatch=etag)`:

- Success → `FetchedArtifact(data=body.read(), sensitivity=Sensitivity(meta),
  retention_class=meta)`, reading `sensitivity` / `retention-class` from
  `resp["Metadata"]` (boto3 lowercases metadata keys and strips the `x-amz-meta-`
  prefix).
- `ClientError` whose HTTP status is **404** (`NoSuchKey`) or **412**
  (`PreconditionFailed`) → `CategorizedError(STALE_HANDLE)` — the object is gone or
  the etag no longer matches ([ADR-0005](../../adr/0005-postgres-object-store-state.md),
  [ADR-0013](../../adr/0013-object-store-layout-retention.md)).
- Any other `ClientError` → `CategorizedError(INFRASTRUCTURE_FAILURE)`, carrying the
  S3 error code in `details`.

The status is read from `err.response["ResponseMetadata"]["HTTPStatusCode"]` (the
status code is consistent across S3 implementations; MinIO and AWS return 412 for
an `IfMatch` miss and 404 for a missing key).

**`register_artifact_row`.** Pure construction — mints
`Artifact(id=uuid4(), created_at=now, updated_at=now, owner_kind=…, owner_id=…,
object_key=stored.key, etag=stored.etag, sensitivity=…, retention_class=…)`. The
timestamps are advisory (the DB overwrites them on insert per
[ADR-0016](../../adr/0016-repository-layer-locks-idempotency.md)); `now` uses
`datetime.now(UTC)`. No database access — the caller inserts via `ARTIFACTS.insert`
and commits.

### Typing the boto3 client

boto3 ships no inline types and `boto3-stubs` is not a dependency. The module aliases
`S3Client = Any` at a single annotated site with a short justification, rather than
adding a stubs package for one client type. This is the only `Any` in the module;
everything the module itself returns is fully typed (`StoredArtifact`,
`FetchedArtifact`, `Artifact`).

## Data flow (illustrative, a future worker step)

```
worker capture_vmcore step (later issue), one pooled connection:
  store = object_store_from_env()
  stored = await asyncio.to_thread(
      store.put_artifact, tenant, "vmcore", str(system_id), "vmcore.zst",
      data=redacted_bytes, sensitivity=Sensitivity.REDACTED,
      retention_class="vmcore",
  )                                                   # object written first
  row = register_artifact_row(
      stored, owner_kind="system", owner_id=system_id,
      sensitivity=Sensitivity.REDACTED, retention_class="vmcore",
  )
  async with conn.transaction():
      await ARTIFACTS.insert(conn, row)               # row committed after
```

## Error handling summary

| Condition | Raised | Kind |
|-----------|--------|------|
| `KDIVE_S3_ENDPOINT_URL` / `KDIVE_S3_BUCKET` unset | `CategorizedError(CONFIGURATION_ERROR)` | operational |
| empty / `/`- / control-char key component | `CategorizedError(CONFIGURATION_ERROR)` | operational |
| `get` on a missing object (404) | `CategorizedError(STALE_HANDLE)` | operational |
| `get` with a non-matching etag (412) | `CategorizedError(STALE_HANDLE)` | operational |
| any other S3 `ClientError` | `CategorizedError(INFRASTRUCTURE_FAILURE)` | operational |

The storage layer raises only `CategorizedError` — every failure here is
operational (a handler turns it into a client response). There is no consistency
or programming error specific to this module.

## Testing strategy

A disposable MinIO container, mirroring the db layer's `testcontainers` discipline
(`tests/db/conftest.py`). Because `testcontainers.minio` requires the `minio` pip
package (not a dependency) and we already ship `boto3`, a new
`tests/store/conftest.py` starts a generic `DockerContainer` and polls boto3
`list_buckets` for readiness:

- `minio_store` (session-scoped): start `minio/minio:RELEASE.2025-10-15T17-29-55Z`
  with `command="server /data"` and root user/password env, expose 9000, poll
  `list_buckets` until ready (bounded retries with a timeout, hard-skip rules
  matching `conftest.py`: skip when Docker is unreachable unless
  `KDIVE_REQUIRE_DOCKER=1`). Yield a configured `ObjectStore` against a freshly
  created bucket. The official MinIO image is archived; the tag is pinned and the
  fallback (localstack / a Chainguard rebuild) is noted in
  [ADR-0017](../../adr/0017-object-store-client-interface.md).

Tests (behavior and edges, not implementation):

- **round-trip** — `put_artifact` then `get_artifact(key, etag)` returns the same
  bytes (the acceptance).
- **sensitivity persisted** — after `put`, the fetched `FetchedArtifact.sensitivity`
  is the value written, and a direct `head_object` shows it in object metadata (the
  acceptance: "sensitivity tag is persisted as object metadata"); `retention_class`
  round-trips too.
- **stale etag → `stale_handle`** — `get_artifact(key, "wrong-etag")` raises
  `CategorizedError` with `STALE_HANDLE` (the acceptance).
- **missing object → `stale_handle`** — `get_artifact("absent/key", etag)` raises
  `STALE_HANDLE`.
- **key scheme** — `put_artifact("t", "vmcore", "oid", "core")` stores under
  `t/vmcore/oid/core` (assert the returned `key`).
- **key validation** — empty, `/`-bearing, and control-char components each raise
  `CONFIGURATION_ERROR`; the offending component is named.
- **etag normalization** — the returned `etag` has no surrounding quotes, and it is
  exactly the value a successful `IfMatch` GET accepts.
- **`register_artifact_row`** — maps a `StoredArtifact` + owner to an `Artifact`
  with matching `object_key` / `etag` / `sensitivity` / `retention_class`, a minted
  `id`, and no database access (pure; unit test, no container).
- **`object_store_from_env`** — missing env var raises `CONFIGURATION_ERROR`
  (monkeypatched env; no container).

The env-gated libvirt/gdb/drgn integration tests are untouched and stay gated. The
MinIO tests gate on Docker exactly as the db tests do.

## Files

- Create `src/kdive/store/objectstore.py`.
- Create `tests/store/__init__.py`, `tests/store/conftest.py`,
  `tests/store/test_objectstore.py`.
- Create `docs/adr/0017-object-store-client-interface.md`; add it to
  `docs/adr/README.md`.
