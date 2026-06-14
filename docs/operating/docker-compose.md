# Running KDIVE with Docker Compose

The repo-root [`docker-compose.yml`](../../docker-compose.yml) brings up the three KDIVE
processes (`server` / `worker` / `reconciler`), the `migrate` one-shot, and a set of dev
backends (Postgres, MinIO, mock OIDC) in a single dependency graph. This is the fastest way
to a working MCP endpoint for demos and evaluation; it is not a production deployment.

The full value reference for the app tier — image selection, the `x-backends` anchor, and
pre-building the image — is in [`deploy/compose/README.md`](../../deploy/compose/README.md).

## Bring-up

`docker compose up` resolves the graph, so one command starts the whole stack:

```bash
docker compose up -d server worker reconciler
```

Configuration is read from `KDIVE_*` variables; see
[the config reference](../guide/reference/config.md) for every setting.

## Backend and migrate ordering

The app services declare `depends_on: migrate` with
`condition: service_completed_successfully`, so no process reaches the database before the
schema is rolled forward. The `migrate` one-shot itself waits on a healthy Postgres, and the
`minio-init` bucket-creation one-shot completes before any app process starts, so the
worker's first artifact write never races a missing bucket. A non-zero `migrate` exit blocks
app start. You do not order these services by hand — Compose does it from the graph.

## Pointing an agent at the endpoint

The server publishes the MCP endpoint over streamable HTTP. Point an agent at
`http://localhost:8000/mcp` (or the host/port you mapped) and supply a bearer token your
OIDC issuer accepts. The agent's MCP client config names the server and its URL; consult
your client's documentation for the exact `mcpServers` shape.
