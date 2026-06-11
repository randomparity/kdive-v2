# kdive Helm chart

Deploys the three kdive processes — server, worker, reconciler — plus a migrate
one-shot Job, against operator-provided Postgres/MinIO/OIDC backends. Implements
ADR-0088 (deployment & packaging).

This README is the value/flag reference. For an end-to-end bring-up — building and
pushing the image, standing up backends, reaching the MCP endpoint, and verifying —
follow [`docs/runbooks/kubernetes-deploy.md`](../../../docs/runbooks/kubernetes-deploy.md).

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

## Health probes & scrape (ADR-0090 §5)

Every Deployment wires `livenessProbe` → `/livez` and `readinessProbe` → `/readyz` on
the process's aux port (`server` 9464, `worker` 9465, `reconciler` 9466), and carries
`prometheus.io/scrape` pod annotations pointing a pull-based collector at `/metrics` on
that port. Liveness tracks the loop being alive, readiness tracks the process's own
backend set — a failing `/readyz` (a backend down) withdraws/gates the pod but does
**not** trip liveness, so a live-but-not-ready pod is never killed.

The aux listener binds `0.0.0.0:<port>` *inside* the pod (set per Deployment via
`KDIVE_HEALTH_BIND_ADDR`, overriding the loopback registry default) so the node kubelet
and the scrape can reach it. **No Service fronts the aux port** — only the server's MCP
`8000` is exposed — so the unauthenticated `/readyz`/`/metrics` stay pod-local. The
network boundary is their access control; scope it with a NetworkPolicy if your scrape
source is not pod-local.

## Secrets

`config.*` renders into a plain ConfigMap, so it is for **non-secret** configuration
(endpoints, bucket, region, OIDC issuer). Do not put a database DSN with an embedded
password or S3 secret keys into `config.*` in production.

### File-ref secrets (`secrets.secretName`)

The file-ref secret backend (ADR-0027/ADR-0088 decision 3) resolves credentials from
files under `KDIVE_SECRETS_ROOT` — the remote-libvirt TLS client cert/key/CA
(`KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF` etc.) and debug-session secrets. Create a
Secret whose keys are the credential filenames, then point `secrets.secretName` at it:

```sh
kubectl create secret generic kdive-remote-tls \
  --from-file=clientcert.pem=client.pem \
  --from-file=clientkey.pem=clientkey.pem \
  --from-file=cacert.pem=ca.pem

helm install kdive deploy/helm/kdive \
  --set secrets.secretName=kdive-remote-tls \
  --set config.KDIVE_REMOTE_LIBVIRT_URI='qemu+tls://host.example/system' \
  --set config.KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF=clientcert.pem \
  --set config.KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF=clientkey.pem \
  --set config.KDIVE_REMOTE_LIBVIRT_CA_CERT_REF=cacert.pem \
  --set config.KDIVE_DATABASE_URL=... --set config.KDIVE_OIDC_ISSUER=...
```

The chart mounts the Secret **read-only** (`defaultMode 0400`) at `secrets.mountPath`
(default `/etc/kdive/secrets`) on the server, worker, and reconciler, and sets
`KDIVE_SECRETS_ROOT` to that path. Refs are resolved **relative to the root**, so a
bare key name like `clientcert.pem` is enough — the Kubernetes Secret volume's `..data`
symlink indirection resolves correctly, and a ref escaping the root is rejected. Leaving
`secrets.secretName` empty mounts nothing.

For S3, prefer IRSA/workload identity, or a managed Secret you `envFrom` onto the pods.
The fixed `demoCredentials` are non-secret by design: the demo data they guard is
throwaway `emptyDir` state.

## Subchart distribution

The `postgresql`/`minio` dependencies pin to `oci://registry-1.docker.io/bitnamicharts`.
Bitnami retired the `charts.bitnami.com` HTTP index in 2025 and now publishes only
OCI artifacts. `charts/` is gitignored and rebuilt from the committed `Chart.lock`
with `helm dependency build`.
