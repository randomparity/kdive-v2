# ADR 0017 — Object-store client interface & failure contract

- **Status:** Proposed
- **Date:** 2026-06-03
- **Deciders:** core-platform
- **Implements:** issue #8; builds on [0005](0005-postgres-object-store-state.md)
  (object store for bulk artifacts, write-before-commit ordering) and
  [0013](0013-object-store-layout-retention.md) (key scheme, sensitivity/retention).

## Context

The schema (#6) and the repository layer (#7) persist `artifacts` rows that
reference S3 objects by `key + etag`; nothing yet writes or reads the objects
themselves. M0 needs the S3-compatible client that the worker (`capture_vmcore`,
build outputs) and the `artifacts.get` handler call. The key scheme, sensitivity
and retention classes, and the write-before-commit ordering are already decided in
[0013](0013-object-store-layout-retention.md) and
[0005](0005-postgres-object-store-state.md); this ADR pins the *client interface*
and its *failure contract* — the seams the worker/handler issues build on. The
layer has no callers yet, so the contracts are internal and refactorable under the
strict `ty` config, but they shape those later issues. Six decisions had viable
alternatives.

## Decision

**1. A synchronous `ObjectStore` wrapping a boto3 S3 client; async callers offload.**
`ObjectStore(client, bucket)` holds a boto3 client and a bucket; an
`object_store_from_env()` factory reads `KDIVE_S3_ENDPOINT_URL`, `KDIVE_S3_BUCKET`,
and `KDIVE_S3_REGION` (default `us-east-1` — boto3 signs with SigV4 and raises
`NoRegionError` without one), and lets boto3's default chain resolve credentials.
`put_artifact` and `get_artifact` are synchronous (boto3 is synchronous). Callers
in the async worker/handler offload with `asyncio.to_thread` — the storage module
does not depend on the event loop.

**2. The client is policy-neutral; redaction-eligibility is the caller's.**
`get_artifact` fetches `sensitive` objects too (the worker must read raw to derive
a redacted copy) and returns the object's `sensitivity` alongside the bytes so the
handler can enforce "only `redacted` is response-eligible"
([0013](0013-object-store-layout-retention.md)). The storage layer never decides
what may reach a response.

**3. etag consistency via conditional GET; both miss and mismatch are
`stale_handle`.** `get_artifact(key, etag)` issues a conditional `GetObject`
(`IfMatch=etag`). A missing object (404) and an etag mismatch (412) both raise
`CategorizedError(STALE_HANDLE)` — the single failure
[0005](0005-postgres-object-store-state.md)/[0013](0013-object-store-layout-retention.md)
name for "the row's object is gone or rotated". Other S3 errors — any other
`ClientError`, any `BotoCoreError` (connection/timeout, a sibling of `ClientError`,
not a subclass), and an object the layer cannot interpret (absent or out-of-enum
`sensitivity` metadata) — raise `CategorizedError(INFRASTRUCTURE_FAILURE)`; no bare
`BotoCoreError`/`ClientError`/`KeyError`/`ValueError` escapes, so the layer's only
failure type is `CategorizedError`.

**4. Sensitivity and retention are stored as S3 user metadata.** `put_artifact`
writes `sensitivity` and `retention_class` as object user metadata
(`x-amz-meta-*`), read back on `get`. Tag-based bucket lifecycle wiring is deferred
to the ops issue that configures lifecycle rules; M0 records both on the object so
the values round-trip and the redaction rule is enforceable at fetch.

**5. `register_artifact_row` constructs the `Artifact` row from the storage result;
the caller commits.** It is a pure function — given the `StoredArtifact` `put`
returned and the owner (`owner_kind`, `owner_id`), it mints an `Artifact`. The
row's `sensitivity` / `retention_class` are read off `StoredArtifact` (which carries
the values actually written to the object), not re-supplied, so the row cannot
disagree with the object — closing a silent-drift path that would defeat redaction.
The row's `sensitivity` is authoritative for the handler's response gate; the
metadata `get_artifact` returns is defense-in-depth. It does **not** touch the
database, so the storage module has no dependency on `kdive.db`. The caller (#24)
writes the object first, then inserts and commits the row in its own transaction —
the write-before-commit ordering of
[0005](0005-postgres-object-store-state.md) stays in the caller's hands.

**6. Keys are built and validated by one helper.** `put_artifact` derives
`{tenant}/{kind}/{object_id}/{name}` through a single validator that rejects an
empty component or one containing `/` or a control character, raising
`CategorizedError(CONFIGURATION_ERROR)`. This keeps the prefix unambiguous and
forecloses key injection; tenant isolation itself remains an access-control concern
([0013](0013-object-store-layout-retention.md)), not the prefix.

## Consequences

- A synchronous client keeps the dependency surface to the already-present `boto3`;
  the cost is that async callers must remember `asyncio.to_thread` (documented on
  the methods). A future move to `aioboto3` stays additive.
- Returning `sensitivity` from `get_artifact` gives the handler what it needs to
  refuse raw objects without a second `HeadObject` call, and keeps the storage layer
  free of response policy.
- Collapsing "missing" and "mismatch" onto `stale_handle` matches the row-reference
  contract: a caller that holds a stale `(key, etag)` gets one typed failure to map,
  not two.
- Storing class on the object (not a tag) means lifecycle-by-tag needs a later
  migration to tags; the acceptance only requires the value to persist as metadata,
  and metadata round-trips on `get` with no extra call.
- A pure `register_artifact_row` keeps storage and `db` decoupled and the
  two-store write ordering explicit at the call site; the cost is the caller writes
  the `ARTIFACTS.insert` + commit itself (it owns the transaction anyway).
- Centralized key validation makes a malformed key a loud, typed config error before
  any S3 call, not a silent odd object.

## Alternatives considered

- **`aioboto3` async client.** Rejected for M0: a new dependency to avoid a
  `to_thread` hop the worker already needs for other blocking work; the sync client
  is honest about boto3 and keeps offloading the caller's choice.
- **`get_artifact` refuses `sensitive` objects.** Rejected: the worker must read raw
  to produce the redacted derivative; enforcing response-policy in storage would
  break that path. Policy lives in the handler.
- **Separate `not_found` vs `stale_handle` for missing vs mismatch.** Rejected:
  [0005](0005-postgres-object-store-state.md) already names both "row's object is
  gone" as `stale_handle`; splitting them adds a category callers must re-merge.
- **Sensitivity as an S3 object tag now.** Rejected for M0: tags exist to drive
  lifecycle rules that M0 has not configured; user metadata satisfies the acceptance
  (persist + round-trip) without the extra `PutObjectTagging` call. Revisit with the
  lifecycle-config issue.
- **`register_artifact_row` inserts the row itself.** Rejected: it would couple the
  storage module to `kdive.db` and bury the commit point, obscuring the
  write-before-commit ordering the caller must own.
- **`put_artifact` returns a bare `(key, etag)` 2-tuple; `register_artifact_row`
  re-takes `sensitivity`/`retention_class`.** Rejected: re-supplying the class on the
  row lets it silently diverge from the object's metadata — a raw object recorded as
  redacted is exactly the redaction failure [0013](0013-object-store-layout-retention.md)
  forbids. The return is a `StoredArtifact` named record carrying the written class so
  the row derives it from one source; `.key`/`.etag` still expose the issue's
  documented pair.
- **Enforce write-once with a conditional `PutObject` (`IfNoneMatch=*`).** Rejected
  for M0 (out of issue scope): write-once is a property of the key-allocation
  discipline and the retention rule, not yet a client guard; revisit if clobbering
  becomes a real risk.
- **Add the `minio` client / `testcontainers[minio]` for the test fixture.**
  Rejected: a generic `DockerContainer` plus a boto3 readiness poll reuses the
  `boto3` we already ship; MinIO's official images are archived, so the fixture pins
  the last tag actually published to Docker Hub (`RELEASE.2025-09-07T16-13-09Z` — the
  source-only 2025-10-15 patch was never pushed as an image) for the disposable test
  container and notes localstack / a Chainguard rebuild as the fallback if the tag
  stops resolving.
