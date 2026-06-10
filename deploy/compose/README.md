# Reference compose — app tier (ADR-0088)

The repo-root [`docker-compose.yml`](../../docker-compose.yml) brings up the three kdive
processes (`server` / `worker` / `reconciler`) plus a `migrate` one-shot, on top of the
existing dev backends (Postgres, MinIO, mock OIDC). Everything is wired purely through
`KDIVE_*` (see the [config reference](../../docs/guide/reference/config.md)); the shared
backend env is declared once as the `x-backends` anchor and merged into each service.

This is a dev/demo reference, not a production deployment. It runs the app tier from the
image built by the repo [`Dockerfile`](../../Dockerfile) (`image: kdive:dev`).

## Bring-up

```bash
docker build -t kdive:dev .                       # build the app image once
docker compose up -d --wait postgres minio oidc   # backends, wait for healthy
docker compose run --rm minio-init                # create the artifacts bucket
docker compose up -d server worker reconciler     # runs migrate to completion first
```

`docker compose up server` resolves the dependency graph: it waits for a healthy Postgres,
runs `migrate` to a **successful exit**, and only then starts the app processes. The app
services declare `depends_on: migrate` with `condition: service_completed_successfully`, so
they never reach the database before the schema is rolled forward (ADR-0088 decision 4). A
non-zero `migrate` exit blocks app start.

## Verify

```bash
docker inspect "$(docker compose ps -q migrate)" --format '{{.State.ExitCode}}'  # 0
docker compose logs migrate                                                       # "applied N migration(s)"
docker compose ps server worker reconciler                                        # all running
curl -i http://localhost:8000/mcp                                                 # server accepts (HTTP 401 unauthenticated)
```

`migrate` exits 0, the three processes stay up, and the server accepts connections. An
unauthenticated probe returns `401` — that is the server's auth layer responding, which is
the M2.1 liveness bar (ADR-0088 decision 5). A live DB round-trip and dependency-readiness
check are M2.3's readiness probe, not an M2.1 claim.

## Driving an authenticated request

The mock OIDC issuer derives a token's `iss` claim from the URL it is minted through. The
in-network server validates against `KDIVE_OIDC_ISSUER=http://oidc:8080/default` (the
issuer's address *inside* the compose network), so a token minted from the host via the
published `http://localhost:8090/default` carries `iss=http://localhost:8090/default` and is
rejected. To exercise an authenticated call against the compose server, mint the token from
*inside* the network (a one-off container joined to the compose network, hitting
`http://oidc:8080`), so its `iss` matches what the server expects.

The token flow itself (authorize → code → exchange) is the same one the live-stack harness
uses — see [`tests/integration/live_stack/harness.py`](../../tests/integration/live_stack/harness.py)
and the [live-stack runbook](../../docs/runbooks/live-stack.md), which runs the server *on
the host* (where `iss=http://localhost:8090/default` matches host-minted tokens).

## Teardown

```bash
docker compose down -v   # stop everything and drop the named volumes
```
