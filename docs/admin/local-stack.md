# Local KDIVE Stack Administration

This guide assumes KDIVE is installed as a Python package on the libvirt host. It does not
use `just` or require running from a source checkout.

The app processes (`server` / `worker` / `reconciler`) and the `migrate` one-shot now run
from the published image via the reference compose app tier (ADR-0088); the hand-rolled
`stack` supervisor and the `install-compose`/`print-local-env` helpers were retired. See
[`deploy/compose/README.md`](../../deploy/compose/README.md) for the compose bring-up and
[the config reference](../guide/reference/config.md) for every `KDIVE_*` variable.

## Backing Services

The repo-root [`docker-compose.yml`](../../docker-compose.yml) declares the backing services
(Postgres, MinIO, mock OIDC) alongside the app tier. Bring the backends up:

```bash
docker compose up -d --wait postgres minio oidc
docker compose run --rm minio-init
```

Production-like deployments may replace these containers with managed Postgres, managed
S3-compatible object storage, and a real OIDC issuer. The KDIVE processes only require the
environment variables documented in [the config reference](../guide/reference/config.md).

## Environment

Install the default local-libvirt fixture catalog:

```bash
python -m kdive install-fixtures --dest /etc/kdive/fixtures/local-libvirt
```

Set the `KDIVE_*` environment from [the config reference](../guide/reference/config.md),
especially:

- `KDIVE_DATABASE_URL`
- `KDIVE_OIDC_*`
- `KDIVE_S3_*`
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
- `KDIVE_KERNEL_SRC`
- `KDIVE_FIXTURE_CATALOG_PATH`

## Schema

```bash
python -m kdive migrate
```

## Seed A Demo Project

```bash
python -m kdive seed-demo \
  --project demo \
  --limit-kcu 1000000 \
  --max-concurrent-allocations 4 \
  --max-concurrent-systems 4
```

This creates the budget/quota rows needed for agent allocations and registers the local
libvirt resource discovered on the host.

## Start The Stack

Run the app tier from the compose reference (builds the image, runs the backends and the
`migrate` one-shot first):

```bash
docker compose up -d migrate server worker reconciler
```

To run the processes directly under a process manager such as systemd instead of compose:

```bash
python -m kdive server
python -m kdive worker
python -m kdive reconciler
```
