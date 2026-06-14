# Reference compose — app tier (ADR-0088)

The repo-root [`docker-compose.yml`](../../docker-compose.yml) brings up the three kdive
processes (`server` / `worker` / `reconciler`) plus a `migrate` one-shot, on top of the
existing dev backends (Postgres, MinIO, mock OIDC). Everything is wired purely through
`KDIVE_*` (see the [config reference](../../docs/guide/reference/config.md)); the shared
backend env is declared once as the `x-backends` anchor and merged into each service.

This is a dev/demo reference, not a production deployment. It runs the app tier from the
image built by the repo [`Dockerfile`](../../Dockerfile) (`image: kdive:dev`).

## Bring-up

The dependency graph is self-contained, so a single `up` brings the whole stack:

```bash
docker compose up -d server worker reconciler   # builds the image, runs the backends + migrate first
```

`docker compose up` resolves the graph rather than relying on the operator to order it:
the app services pull in a healthy Postgres, the `minio-init` bucket-creation one-shot
(which itself waits for a healthy MinIO), the OIDC issuer, and the `migrate` one-shot. They
declare `depends_on: migrate` with `condition: service_completed_successfully`, so they
never reach the database before the schema is rolled forward (ADR-0088 decision 4); a
non-zero `migrate` exit blocks app start. The bucket-creation one-shot completes before any
app process starts, so the worker's first artifact write never races a missing bucket.

The image is built once from the repo `Dockerfile` via the `migrate` service's `build: .`
and reused by the others. Pre-build it explicitly if you prefer:

```bash
docker build -t kdive:dev .
```

## Verify

```bash
docker inspect "$(docker compose ps -q migrate)" --format '{{.State.ExitCode}}'  # 0
docker compose logs migrate                                                       # "applied N migration(s)"
docker compose ps server worker reconciler                                        # all running
curl -i http://localhost:8000/mcp                                                 # server accepts (HTTP 401 unauthenticated)
```

`migrate` exits 0, the three processes stay up, and the server accepts connections. An
unauthenticated probe returns `401` — that is the server's auth layer responding.

### Health probes (ADR-0090 §5)

Each app process runs the aux health/metrics listener and compose health-checks it on its
own `/readyz`. The listener binds `0.0.0.0:<port>` *inside* the container (`server` 9464,
`worker` 9465, `reconciler` 9466) via `KDIVE_HEALTH_BIND_ADDR`, set per service. The port
is **never published to the host** — the container network namespace is its only access
boundary, so the unauthenticated `/readyz`/`/metrics` stay non-public. A backend going down
flips the container to `unhealthy`:

```bash
docker compose ps                          # STATUS shows (healthy)/(unhealthy) per process
# Inspect the aux endpoints from inside a container (the port is not on the host):
docker compose exec server python -c \
  'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9464/readyz").read())'
```

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
and the [live-stack runbook](../../docs/operating/runbooks/live-stack.md), which runs the server *on
the host* (where `iss=http://localhost:8090/default` matches host-minted tokens).

## Teardown

```bash
docker compose down -v   # stop everything and drop the named volumes
```

## Image provenance — verify before you run a published image

This reference builds the image locally (`image: kdive:dev`). When you instead pull a
**published** image from `ghcr.io/randomparity/kdive`, verify its signature first. The
[`release-image`](../../.github/workflows/release-image.yml) workflow signs each released
digest keyless/OIDC on a SemVer tag and attaches an SBOM (ADR-0088 decision 8), so a
consumer can confirm the image was built by this repo's release workflow before trusting it.

Install [cosign](https://docs.sigstore.dev/cosign/system_config/installation/), then verify
the tag you intend to run:

```bash
cosign verify ghcr.io/randomparity/kdive:vX.Y.Z \
  --certificate-identity-regexp '^https://github.com/randomparity/kdive/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The identity regexp pins the signer to a workflow in this repository and the issuer pins it
to GitHub's OIDC provider; a signature from any other identity or issuer fails the check.

The SBOM and provenance are attached by BuildKit (`docker/build-push-action` `sbom: true`,
`provenance: mode=max`) as in-toto **attestations** referring to the image index — distinct
from the image signature `cosign verify` checks. Inspect them with `buildx imagetools`:

```bash
docker buildx imagetools inspect ghcr.io/randomparity/kdive:vX.Y.Z \
  --format '{{ json .SBOM }}'        # or '{{ json .Provenance }}'
```
