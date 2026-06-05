# Runbook: live-stack end-to-end bring-up

Operator guide for standing up the M1.2 live stack and running the `live_stack` suite.
The suite drives the full kdive spine over the real MCP HTTP transport against a running
`server`/`worker`/`reconciler` and the containerized backing services. See
[ADR-0042](../adr/0042-live-stack-e2e-mcp-http.md) for the decision and
[`docs/plans/m1.2-implementation.md`](../plans/m1.2-implementation.md) for the epic.

The `server`, `worker`, and `reconciler` run **on the host** (not in containers) against
the `docker-compose.yml` backends, so qemu disk-image and kernel-tree paths resolve where
`libvirtd` runs. Containerizing them is a deferred follow-on (ADR-0042 §2).

## Prerequisites

- A KVM / nested-virt host with `libvirt` and a running `libvirtd`.
- Docker with a reachable daemon and **pullable** compose images. The compose file pins
  `ghcr.io/navikt/mock-oauth2-server:3.0.3`; if that tag no longer resolves on ghcr.io,
  re-pin it to a current tag before `just stack-up`.
- The repo set up: `just setup` (or `uv sync --locked`).
- The VM fixtures built (below).

## 1. Bring up the backends

```bash
just stack-up
```

This waits for the three long-running backends — Postgres, MinIO, and the mock OIDC issuer
— to be **healthy**, then runs the one-shot `minio-init` to completion (creating the
`kdive-artifacts` bucket) and prints the host-process env block and next steps.

> The recipe scopes `docker compose up --wait` to the long-running backends and runs
> `minio-init` separately, because `--wait` treats a run-to-completion service's exit as a
> wait failure. `minio-init`'s exit code still propagates, so a genuine bucket-creation
> failure fails `just stack-up`.

## 2. Export the host-process env

The three host processes read `KDIVE_*` env pointed at the compose host ports. `stack-up`
prints this block; export it in the shell that will start the processes:

```bash
export KDIVE_DATABASE_URL=postgresql://kdive:kdive@localhost:5432/kdive # pragma: allowlist secret — local dev only
export KDIVE_OIDC_ISSUER=http://localhost:8090/default
export KDIVE_OIDC_JWKS_URI=http://localhost:8090/default/jwks
export KDIVE_OIDC_AUDIENCE=kdive
export KDIVE_S3_ENDPOINT_URL=http://localhost:9000
export KDIVE_S3_BUCKET=kdive-artifacts
export KDIVE_S3_REGION=us-east-1
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
```

**The most error-prone step:** the object store reads S3 **credentials from boto3's
default chain** (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`), **not** from `KDIVE_S3_*`.
MinIO's root user/password are `minioadmin`/`minioadmin`, so those must be exported as the
`AWS_*` vars or every artifact `put`/`get` fails with an access error that looks like a
code bug. The `KDIVE_S3_*` vars carry only the endpoint, bucket, and region.

| var | value | consumed by |
|-----|-------|-------------|
| `KDIVE_DATABASE_URL` | `postgresql://kdive:kdive@localhost:5432/kdive` | `db/pool.py` | <!-- pragma: allowlist secret — local dev only -->
| `KDIVE_OIDC_ISSUER` | `http://localhost:8090/default` | `mcp/auth.py` |
| `KDIVE_OIDC_JWKS_URI` | `http://localhost:8090/default/jwks` | `mcp/auth.py` |
| `KDIVE_OIDC_AUDIENCE` | `kdive` | `mcp/auth.py` |
| `KDIVE_S3_ENDPOINT_URL` | `http://localhost:9000` | `store/objectstore.py` |
| `KDIVE_S3_BUCKET` | `kdive-artifacts` | `store/objectstore.py` |
| `KDIVE_S3_REGION` | `us-east-1` | `store/objectstore.py` |
| `AWS_ACCESS_KEY_ID` | `minioadmin` | boto3 default chain |
| `AWS_SECRET_ACCESS_KEY` | `minioadmin` | boto3 default chain |

## 3. Build the VM fixtures

The spine boots a real guest and builds a real kernel, so the suite needs an
operator-provided guest image and kernel tree (the ADR-0035 fixtures, reused unchanged):

```bash
scripts/live-vm/build-guest-image.sh    # writes the kdump-enabled guest image
scripts/live-vm/fetch-kernel-tree.sh     # checks out the pinned kernel source tree
export KDIVE_GUEST_IMAGE=/path/to/guest-image
export KDIVE_KERNEL_SRC=/path/to/kernel-tree
```

Point `KDIVE_GUEST_IMAGE` and `KDIVE_KERNEL_SRC` at the scripts' output. The `live_stack`
preflight skips with the exact script to run when either is missing.

## 4. Start the three host processes

In **three separate terminals** (each with the env from step 2 exported), foreground:

```bash
python -m kdive server       # MCP streamable-HTTP server (default 127.0.0.1:8000)
python -m kdive worker       # job-queue worker — drains provision/build/install/boot/capture
python -m kdive reconciler   # drift-repair loop
```

Then point the test driver at the running server:

```bash
export KDIVE_STACK_BASE_URL=http://127.0.0.1:8000/mcp/
```

Override the bind address with `KDIVE_HTTP_HOST` / `KDIVE_HTTP_PORT` if `127.0.0.1:8000`
is taken; keep `KDIVE_STACK_BASE_URL` in sync.

## 5. Run the suite

```bash
just test-live-stack
```

This runs `pytest -m live_stack`. The `live_stack` preflight skips cleanly with an
actionable reason when the fixtures or the stack are absent — so the recipe is safe to run
on any host. When **no** `live_stack` test is collected yet (the marked spine driver lands
in a later sub-issue), the recipe reports `no live_stack tests collected — skipping
cleanly` and exits 0.

## 6. Teardown

Stop the three host processes (Ctrl-C each), then remove the backends and their volumes:

```bash
docker compose down -v
```

`down -v` drops the Postgres and MinIO volumes, so the next `just stack-up` starts from a
clean schema and an empty bucket.
