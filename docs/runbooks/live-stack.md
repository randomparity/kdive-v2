# Runbook: live-stack end-to-end bring-up

Operator guide for standing up the M1.2 live stack and running the `live_stack` suite.
The suite drives the full kdive spine over the real MCP HTTP transport against a running
`server`/`worker`/`reconciler` and the containerized backing services. See
[ADR-0042](../adr/0042-live-stack-e2e-mcp-http.md) for the decision and
[`docs/plans/m1.2-implementation.md`](../plans/m1.2-implementation.md) for the epic.

The `server`, `worker`, and `reconciler` run **on the host** (not in containers) against
the `docker-compose.yml` backends, so qemu disk-image and kernel-tree paths resolve where
`libvirtd` runs. Containerizing them is a deferred follow-on (ADR-0042 §2).

For the **remote** `qemu+tls://` variant — driving the spine against a host the worker tier does
not share a filesystem with — see [remote-live-stack.md](remote-live-stack.md); it reuses this
bring-up and adds worker→host TLS, the gdbstub ACL, and object-store reachability for the
two-phase vmcore upload.

The `just` recipes below are source-tree conveniences. Installed-package deployments use
`python -m kdive migrate` and `python -m kdive seed-demo`, then run the app tier from the
compose reference (`docker compose up -d migrate server worker reconciler`); see
[`docs/admin/local-stack.md`](../admin/local-stack.md) and
[`deploy/compose/README.md`](../../deploy/compose/README.md).

## Prerequisites

- A KVM / nested-virt host with `libvirt` and a running `libvirtd`.
- Docker with a reachable daemon and **pullable** compose images. The compose file pins
  `ghcr.io/navikt/mock-oauth2-server:3.0.3`; if that tag no longer resolves on ghcr.io,
  re-pin it to a current tag before `just stack-up`.
- The repo set up: `just setup` (or `uv sync --locked`).
- The VM fixtures built (below).
- If you run a **published** kdive image from `ghcr.io/randomparity/kdive` rather than a
  locally built one, verify its signature first. The release workflow cosign-signs each
  released digest keyless/OIDC and attaches an SBOM (ADR-0088 decision 8); the consumer
  `cosign verify` check is in
  [`deploy/compose/README.md`](../../deploy/compose/README.md#image-provenance--verify-before-you-run-a-published-image).

## 1. Bring up the backends

```bash
just stack-up
```

This waits for the three long-running backends — Postgres, MinIO, and the mock OIDC issuer
— to be **healthy**, runs the one-shot `minio-init` to completion (creating the
`kdive-artifacts` bucket), and applies database migrations.

> The recipe scopes `docker compose up --wait` to the long-running backends and runs
> `minio-init` separately, because `--wait` treats a run-to-completion service's exit as a
> wait failure. `minio-init`'s exit code still propagates, so a genuine bucket-creation
> failure fails `just stack-up`.

## 2. Review the host-process env

The source-tree wrappers source `scripts/live-stack/env.sh`, which exports the local
defaults before starting KDIVE. The full set of `KDIVE_*` variables is in
[the config reference](../guide/reference/config.md); the live-run subset is below.

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

Installed-package deployments usually write these defaults to `/etc/kdive/local.env` and
source that file before running commands:

```bash
set -a
. /etc/kdive/local.env
set +a
python -m kdive migrate
python -m kdive seed-demo --project demo
```

## 3. Build the VM fixtures

The spine boots a real guest and builds a real kernel, so the suite needs an
operator-provided guest image and kernel tree (the ADR-0035 fixtures, reused unchanged):

```bash
scripts/live-vm/build-guest-image.sh    # builds the bootable kdive-ready rootfs qcow2
scripts/live-vm/fetch-kernel-tree.sh     # checks out the pinned kernel source tree
export KDIVE_GUEST_IMAGE=/path/to/guest-image
export KDIVE_KERNEL_SRC=/path/to/kernel-tree
```

The builder runs unprivileged and writes the rootfs to `KDIVE_ROOTFS` (default
`/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2`). For the default root-owned path,
an OS admin pre-prepares the output directory once and makes it writable by the build user; the
per-build write and the final `chmod 0644` are unprivileged. The image is left `0644` so
the separate `qemu` user can read it under `qemu:///system`. Under SELinux the file also
needs the `virt_image_t` label (the standard label for libvirt-managed images); this is the
host-side file label and is independent of the guest-internal SELinux the builder disables.
The build is idempotent on the destination path — delete the file to force a rebuild after
changing any build input (`KDIVE_ROOTFS_DEBUG`, `KDIVE_ROOTFS_VMLINUX`,
`KDIVE_ROOTFS_SSH_USER`, `KDIVE_ROOTFS_SIZE`, or a rotated managed SSH key).

Point `KDIVE_GUEST_IMAGE` and `KDIVE_KERNEL_SRC` at the scripts' output. The `live_stack`
preflight skips with the exact script to run when either is missing.

## 4. Start the host processes

From a source checkout, run the convenience wrapper:

```bash
just stack-start
```

Or start it in the background and stop it by pid file:

```bash
just stack-start-daemon
just stack-stop
```

Installed package — migrate and seed on the host, then run the app tier from the compose
reference ([`deploy/compose/README.md`](../../deploy/compose/README.md)):

```bash
python -m kdive migrate
python -m kdive seed-demo --project demo
docker compose up -d migrate server worker reconciler
```

The default MCP URL is `http://127.0.0.1:8000/mcp`. Override the bind address with
`KDIVE_HTTP_HOST` / `KDIVE_HTTP_PORT` if `127.0.0.1:8000` is taken; keep
`KDIVE_STACK_BASE_URL` in sync.

## 5. Run the suite

```bash
just test-live-stack
```

This runs `pytest -m live_stack`. The `live_stack` preflight skips cleanly with an
actionable reason when the fixtures or the stack are absent — so the recipe is safe to run
on any host. When **no** `live_stack` test is collected yet (the marked spine driver lands
in a later sub-issue), the recipe reports `no live_stack tests collected — skipping
cleanly` and exits 0.

## 6. Kernel debugging demo smoke check

The default installed-package flow is:

```bash
set -a
. /etc/kdive/local.env
set +a
python -m kdive migrate
python -m kdive seed-demo --project demo
docker compose up -d migrate server worker reconciler
```

Expected defaults:

- MCP URL: `http://127.0.0.1:8000/mcp`
- Kernel source: `~/src/linux` unless `KDIVE_KERNEL_SRC` is set
- Build workspace: `/var/lib/kdive/build`
- Component roots: `/var/lib/kdive/build/components:/etc/kdive/fixtures`
- Fixture catalog: `/etc/kdive/fixtures/local-libvirt`
- Fedora kdive-ready rootfs: `/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2`
- Busybox rootfs: `/var/lib/kdive/rootfs/local/busybox-bare.qcow2`

After the stack is up, use the live-stack harness to call MCP tools for:

- `accounting.set_budget`
- `accounting.set_quota`
- `resources.list`
- `allocations.request`
- `systems.provision` with
  `rootfs: {"kind": "catalog", "provider": "local-libvirt", "name": "fedora-kdive-ready-43"}`
- `runs.build` with a staged `.config`
- `runs.install`
- `runs.boot`
- `artifacts.list(system_id=...)`

Vulnerable kernels should produce a console artifact instead of an empty `boot_timeout`.
Patched kernels can boot and reach the readiness marker.

## 7. Teardown

Stop the foreground stack with Ctrl-C, or stop a daemonized source-tree stack with:

```bash
just stack-stop
```

Then remove the backends and their volumes:

```bash
docker compose down -v
```

`down -v` drops the Postgres and MinIO volumes, so the next `just stack-up` starts from a
clean schema and an empty bucket.
