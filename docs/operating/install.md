# Installing KDIVE

KDIVE runs as three processes — `server`, `worker`, `reconciler` — plus a `migrate`
one-shot, on top of operator-provided backends (Postgres, an S3-compatible object store,
and an OIDC issuer). This page covers where the code comes from, what the host needs, and
the three ways to run it.

## Install paths

### From source

Clone the repository and install the locked dependency set with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/randomparity/kdive
cd kdive
uv sync
```

This gives you a `.venv` with the `kdive` package and the `just` recipes used throughout
the docs. Run a process directly with `uv run python -m kdive server`.

### Container image

Released images are published to the GitHub Container Registry:

```bash
docker pull ghcr.io/randomparity/kdive:latest
```

The image runs any of the four entrypoints (`server` / `worker` / `reconciler` / `migrate`)
via `python -m kdive <command>`. How releases are cut and tagged is described in
[the release process](../development/releasing.md).

### PyPI

A PyPI distribution is planned but not yet published. Use the source or container install
until it lands.

## Host prerequisites

KDIVE is configured entirely through `KDIVE_*` environment variables. Every setting,
its default, and whether it is required is listed in
[the config reference](../guide/reference/config.md). At minimum the processes need a
Postgres DSN, S3 endpoint and credentials, and the three OIDC values.

Before the first start, run the provider preflight for the libvirt backend you intend to
use. The preflight reports what is missing without changing the host:

- Local provider: run `just check-local-libvirt`.
- Remote provider: run `just check-remote-libvirt HOST USER URI`.

See [local-libvirt](providers/local-libvirt.md) and
[remote-libvirt](providers/remote-libvirt.md) for what each provider needs.

## Run modes

Pick one of the three deployment shapes:

- [Docker Compose](docker-compose.md) — the app tier plus dev backends in one graph;
  the quickest way to a working endpoint for demos and evaluation.
- [Kubernetes (Helm)](kubernetes.md) — the chart deploys the three processes and the
  migrate Job against external backends.
- [systemd](systemd.md) — run the processes as host services against external backends.
