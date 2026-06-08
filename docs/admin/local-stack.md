# Local KDIVE Stack Administration

This guide assumes KDIVE is installed as a Python package on the libvirt host. It does not
use `just` or require running from a source checkout.

## Backing Services

Install the local backing-service compose file:

```bash
python -m kdive install-compose --dest /etc/kdive/docker-compose.local.yml
```

Start the backing services:

```bash
docker compose -f /etc/kdive/docker-compose.local.yml up -d --wait postgres minio oidc
docker compose -f /etc/kdive/docker-compose.local.yml run --rm minio-init
```

Production-like deployments may replace these containers with managed Postgres, managed
S3-compatible object storage, and a real OIDC issuer. The KDIVE processes only require the
environment variables below.

## Environment

Install the default local-libvirt fixture catalog:

```bash
python -m kdive install-fixtures --dest /etc/kdive/fixtures/local-libvirt
```

Print local defaults:

```bash
python -m kdive print-local-env > /etc/kdive/local.env
```

Review `/etc/kdive/local.env`, especially:

- `KDIVE_DATABASE_URL`
- `KDIVE_OIDC_*`
- `KDIVE_S3_*`
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
- `KDIVE_KERNEL_SRC`
- `KDIVE_FIXTURE_CATALOG_PATH`

## Schema

```bash
set -a
. /etc/kdive/local.env
set +a
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

Demo supervisor:

```bash
python -m kdive stack
```

Production-style process split:

```bash
python -m kdive server
python -m kdive worker
python -m kdive reconciler
```

Use a process manager such as systemd for the split mode.
