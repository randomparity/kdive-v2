# kdive Helm chart

Deploys the three kdive processes — server, worker, reconciler — plus a migrate
one-shot Job, against operator-provided Postgres/MinIO/OIDC backends. Implements
ADR-0088 (deployment & packaging).

## Install (external backends, production)

```sh
helm dependency build deploy/helm/kdive   # populate charts/ from Chart.lock
helm install kdive deploy/helm/kdive \
  --set config.KDIVE_DATABASE_URL='postgresql://<user>:<password>@<host>:5432/kdive' \
  --set config.KDIVE_OIDC_ISSUER=https://idp.example/realms/kdive \
  --set config.KDIVE_OIDC_JWKS_URI=https://idp.example/realms/kdive/protocol/openid-connect/certs \
  --set config.KDIVE_S3_ENDPOINT_URL=https://s3.example
```

The migrate Job runs as a `pre-install`/`pre-upgrade` hook — the external backend
already exists, so migrations apply before the app rollout. Migrations must be
backward-compatible (expand-contract); the runner is forward-only (ADR-0015), so
rollback is image-only and the prior image must tolerate the newer schema.

## Bundled backends (demo only)

`bundledBackends=true` pulls the Postgres and MinIO subcharts and runs them on
`emptyDir`: **a pod restart drops all state by design**. It is ephemeral and not a
production path. The toggle must be co-set with `demoAcknowledged=true` or the
chart refuses to render. On this path the chart derives `KDIVE_DATABASE_URL`,
`KDIVE_S3_ENDPOINT_URL`, and the demo MinIO `AWS_*` credentials from the in-release
service names and `demoCredentials`, and the migrate Job runs `post-install` (after
the bundled DB exists) behind a DB-readiness init container.

```sh
helm dependency build deploy/helm/kdive
helm install kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true
```

## Secrets

`config.*` renders into a plain ConfigMap, so it is for **non-secret** configuration
(endpoints, bucket, region, OIDC issuer). Do not put a database DSN with an embedded
password or S3 secret keys into `config.*` in production. Supply secret-bearing
values out of band — secret files mounted under `KDIVE_SECRETS_ROOT` (ADR-0088
decision 3), IRSA/workload identity for S3, or a managed Secret you `envFrom` onto
the pods. The fixed `demoCredentials` are non-secret by design: the demo data they
guard is throwaway `emptyDir` state.

## Subchart distribution

The `postgresql`/`minio` dependencies pin to `oci://registry-1.docker.io/bitnamicharts`.
Bitnami retired the `charts.bitnami.com` HTTP index in 2025 and now publishes only
OCI artifacts. `charts/` is gitignored and rebuilt from the committed `Chart.lock`
with `helm dependency build`.
