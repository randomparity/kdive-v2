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
- **No streaming / multipart upload.** `put_artifact` takes in-memory `bytes` and
  uses single-PUT `put_object` (valid up to the S3 5 GiB single-PUT limit). Large
  vmcores that need streaming/multipart are a follow-up that widens the input type;
  M0's test artifacts and first cores fit in memory.
- **No artifact authorization.** `get_artifact(key, etag)` returns any object whose
  key+etag match — it is not a tenant boundary (M0 is one bucket, one credential).
  The `artifacts.get` handler (#24) MUST resolve the artifact's owning tenant — from
  the owner row, or the key's leading `tenant` component — and authorize it against
  the request **before** calling `get_artifact`. The `artifacts` row carries no
  `project` column, so this binding is the handler's to make; naming it here so the
  handler issue does not miss it.

## Components

### `objectstore.py` — the S3 client

```python
class StoredArtifact(NamedTuple):
    key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str

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
) -> Artifact: ...
```

`put_artifact` returns `StoredArtifact`, whose `.key` / `.etag` are the issue's
documented `(key, etag)` return. It additionally carries the `sensitivity` /
`retention_class` actually written to the object, so `register_artifact_row` derives
the row's sensitivity/retention from the same source — the `artifacts` row cannot
disagree with the object's metadata. (This is why the return is a named record
rather than a bare 2-tuple: it forecloses a class of silent
object↔row sensitivity drift that would defeat the redaction guarantee.) The row's
`sensitivity` is **authoritative** for the handler's response gate; the object
metadata `get_artifact` returns is defense-in-depth.

`get_artifact` returns `FetchedArtifact` so the handler gets `sensitivity` without a
second `HeadObject`.

The key's `object_id` (a path component) and the row's `owner_id` are **distinct by
design**: an object's kind/object_id organize the prefix (`vmcore/{system_id}`),
while the row's `owner_kind`/`owner_id` name the durable object that owns the
artifact (`system`/`system_id`). `register_artifact_row` therefore takes `owner_id`
explicitly — it is not derivable from the storage result.

**Config.** `object_store_from_env()` reads `KDIVE_S3_ENDPOINT_URL` and
`KDIVE_S3_BUCKET`, failing fast with `CategorizedError(CONFIGURATION_ERROR)` if
either is unset (mirroring `db.pool.database_url`). It builds
`boto3.client("s3", endpoint_url=..., region_name=...)` with an explicit region
(`KDIVE_S3_REGION`, default `us-east-1`) — boto3 signs with SigV4 and raises
`NoRegionError` if no region is resolvable, which a clean CI environment would hit —
and lets boto3's default chain resolve credentials (M0/MinIO via the standard
`AWS_*` env vars); no credential handling is reinvented.

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
retention_class})`. Returns
`StoredArtifact(key, _normalize_etag(resp["ETag"]), sensitivity, retention_class)`.
S3 returns the ETag wrapped in double quotes; `_normalize_etag` strips them so the
value persisted on the `artifacts` row is the bare hex (stable to compare and log).

**etag form: stored vs `If-Match`.** The *stored* etag is the normalized bare hex.
The *`If-Match` header* is a different concern: HTTP entity-tags are quoted, and a
server may compare `If-Match` literally. `get_artifact` therefore re-quotes when
issuing the conditional GET (`IfMatch=f'"{etag}"'`) rather than assuming the bare
form is accepted. This separates "what we store" from "what the wire expects" so a
healthy object is never mis-read as stale. Two assumptions ride on the pinned MinIO
and are the etag tests' job to verify (they fail loudly if either is false): that
MinIO honors `If-Match` on `GetObject`, and that it accepts the quoted form. If a
future S3 backend ignores `If-Match` on GET, the fallback is a `HeadObject` etag
compare before returning, so `stale_handle` is never silently lost — called out here
so the implementer treats the conditional-GET contract as verified, not assumed.

**`get_artifact`.** Issues `get_object(Bucket=bucket, Key=key, IfMatch=f'"{etag}"')`:

- Success → reads `sensitivity` / `retention-class` from `resp["Metadata"]` (boto3
  lowercases metadata keys and strips the `x-amz-meta-` prefix) and returns
  `FetchedArtifact(data=body.read(), sensitivity=Sensitivity(...),
  retention_class=...)`. If either metadata key is **absent**, or `sensitivity`
  holds a value outside the `Sensitivity` enum, the object is one this layer cannot
  interpret → `CategorizedError(INFRASTRUCTURE_FAILURE)` (the `KeyError`/`ValueError`
  is caught and re-raised typed, never escaping as a bare exception).
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
object_key=stored.key, etag=stored.etag, sensitivity=stored.sensitivity,
retention_class=stored.retention_class)`. Sensitivity/retention come from `stored`,
not fresh parameters, so the row matches the object by construction. The timestamps
are advisory (the DB overwrites them on insert per
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
  row = register_artifact_row(stored, owner_kind="system", owner_id=system_id)
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
| `get` on an object with absent/invalid sensitivity metadata | `CategorizedError(INFRASTRUCTURE_FAILURE)` | operational |
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
  `KDIVE_REQUIRE_DOCKER=1`). Build the `ObjectStore` with an explicit
  `region_name="us-east-1"` and the mapped host/port endpoint
  (`get_container_host_ip()` + `get_exposed_port(9000)`), create the bucket, and
  yield it. The official MinIO image is archived; the tag is pinned and the fallback
  (localstack / a Chainguard rebuild) is noted in
  [ADR-0017](../../adr/0017-object-store-client-interface.md).
- `key_ns` (function-scoped): a per-test `uuid4` string used as the `tenant`
  component so tests sharing the session bucket cannot collide on keys (the db
  layer's per-test clean schema has no object-store analog; unique prefixes give the
  same isolation cheaply).

Tests (behavior and edges, not implementation):

- **round-trip** — `put_artifact` then `get_artifact(key, etag)` returns the same
  bytes (the acceptance).
- **sensitivity persisted** — after `put`, the fetched `FetchedArtifact.sensitivity`
  is the value written, and a direct `head_object` shows it in object metadata (the
  acceptance: "sensitivity tag is persisted as object metadata"); `retention_class`
  round-trips too.
- **stale etag → `stale_handle`** — `get_artifact(key, "wrong-etag")` raises
  `CategorizedError` with `STALE_HANDLE` (the acceptance).
- **missing object → `stale_handle`** — `get_artifact("<ns>/absent/key", etag)`
  raises `STALE_HANDLE`.
- **metadata-less object → `infrastructure_failure`** — write an object with the raw
  boto3 client and no user metadata, then `get_artifact` on it raises
  `CategorizedError` with `INFRASTRUCTURE_FAILURE` (never a bare `KeyError`).
- **key scheme** — `put_artifact("t", "vmcore", "oid", "core")` stores under
  `t/vmcore/oid/core` (assert the returned `key`).
- **key validation** — empty, `/`-bearing, and control-char components each raise
  `CONFIGURATION_ERROR`; the offending component is named.
- **etag normalization & `If-Match` form** — the returned `etag` has no surrounding
  quotes; the round-trip and stale-etag tests are the verification of record that a
  conditional GET built from that stored etag (re-quoted by `get_artifact`) both
  succeeds for the current object and raises `STALE_HANDLE` for a non-matching one —
  i.e. MinIO honors `If-Match` on GET and accepts the quoted form.
- **`register_artifact_row`** — maps a `StoredArtifact` + owner to an `Artifact`
  with `object_key` / `etag` / `sensitivity` / `retention_class` taken from `stored`,
  a minted `id`, and no database access (pure; unit test, no container).
- **`object_store_from_env`** — a missing env var raises `CONFIGURATION_ERROR`, and
  an unset `KDIVE_S3_REGION` defaults to `us-east-1` (monkeypatched env; no
  container).

The env-gated libvirt/gdb/drgn integration tests are untouched and stay gated. The
MinIO tests gate on Docker exactly as the db tests do.

## Files

- Create `src/kdive/store/objectstore.py`.
- Create `tests/store/__init__.py`, `tests/store/conftest.py`,
  `tests/store/test_objectstore.py`.
- Create `docs/adr/0017-object-store-client-interface.md`; add it to
  `docs/adr/README.md`.
