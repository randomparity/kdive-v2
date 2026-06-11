# Runbook: Kubernetes / Helm deployment

Operator guide for deploying the kdive control plane — `server`, `worker`, `reconciler`, plus a
migrate one-shot — on Kubernetes with the [Helm chart](../../deploy/helm/kdive/README.md)
(ADR-0088). This is the **production-shaped** path; the
[live-stack runbook](live-stack.md) covers the source-tree (`just`) and `docker compose`
deployments. For driving the spine against a remote `qemu+tls://` libvirt host once the stack is
up, see [remote-live-stack.md](remote-live-stack.md).

It was written from a real microk8s bring-up; commands that are microk8s-specific are called out,
and the generic-cluster equivalent is given alongside.

## Prerequisites

- A Kubernetes cluster and `kubectl`/`helm` (v3) configured against it. Tested on microk8s
  v1.35; any conformant cluster works.
- A **container registry the cluster can pull from**. The chart defaults to
  `ghcr.io/randomparity/kdive`, but no image is published there yet (`docker pull` 404s) — until
  a signed release exists you build and push your own (step 1).
- **External backends** the cluster can reach: Postgres, an S3-compatible object store
  (MinIO/AWS S3), and an OIDC issuer. The bundled-backend demo path (`bundledBackends=true`) is
  `emptyDir`-only and **not** for anything you want to keep (and its Bitnami subchart images were
  retired in 2025 — see the [chart README](../../deploy/helm/kdive/README.md#subchart-distribution)).
- A `StorageClass` for the worker's build/install PVCs (microk8s: `microk8s enable
  hostpath-storage`).

## 1. Build and push the image

No image is published to `ghcr.io/randomparity/kdive` yet, so build from your checkout, tag by
git SHA (not the static `appVersion`, which is unpublished), and push to a registry the cluster
pulls from.

```bash
SHA=$(git rev-parse --short=8 HEAD)
docker build -t <registry>/kdive:$SHA -f Dockerfile .
docker push <registry>/kdive:$SHA
```

**microk8s registry addon.** Enable it (`microk8s enable registry` → `localhost:32000` on the
node) and push over an SSH tunnel from your build host — Docker treats `localhost` as an insecure
registry with no daemon config:

```bash
ssh -fN -L 32000:localhost:32000 <node>          # tunnel the node's :32000 to your host
docker tag <registry>/kdive:$SHA localhost:32000/kdive:$SHA
docker push localhost:32000/kdive:$SHA           # in-cluster ref: localhost:32000/kdive:$SHA
```

Point the chart at the image with `--set image.repository=<registry>/kdive --set image.tag=$SHA`
(below). If you instead consume a **published, signed** release image, `cosign verify` it first —
see the [compose README](../../deploy/compose/README.md#image-provenance--verify-before-you-run-a-published-image).

## 2. Stand up the external backends

Bring up Postgres, the object store, and the OIDC issuer however your environment provides them
(managed services, an in-cluster Postgres/MinIO you operate, etc.), and note the values the chart
needs (step 4). The object store needs a bucket (default `kdive-artifacts`). The migrate Job
(step 4) applies the schema against the Postgres you supply — the database must exist and be
reachable first.

> The object store reads its credentials from **boto3's default chain**
> (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`), not from `KDIVE_S3_*`. Supply them as pod env
> (a Secret you `envFrom`, IRSA/workload identity, or — for a throwaway store — `config.AWS_*`,
> which the ConfigMap `range` emits). `KDIVE_S3_*` carries only the endpoint, bucket, and region.

## 3. Create the file-ref Secret (if using remote-libvirt or debug-session secrets)

The remote-libvirt TLS materials (and debug-session secrets) are resolved **by file** under
`KDIVE_SECRETS_ROOT`. Put them in a Kubernetes Secret whose keys are the credential filenames:

```bash
kubectl create secret generic kdive-remote-tls \
  --from-file=clientcert.pem=client.pem \
  --from-file=clientkey.pem=clientkey.pem \
  --from-file=cacert.pem=ca.pem
```

The chart projects it read-only and points the refs at it in step 4. You reference each key with
a **root-relative** ref (e.g. `clientcert.pem`) — the chart sets `KDIVE_SECRETS_ROOT` to the mount
path and the backend resolves refs under it (the Kubernetes Secret's `..data` symlink indirection
resolves correctly; a ref escaping the root is rejected). Skip this step if you are not deploying
remote-libvirt.

## 4. Install the chart

```bash
helm dependency build deploy/helm/kdive    # populate charts/ from Chart.lock

helm install kdive deploy/helm/kdive \
  --set image.repository=localhost:32000/kdive --set image.tag=$SHA \
  --set config.KDIVE_DATABASE_URL='postgresql://kdive:<pw>@<pg-host>:5432/kdive' \
  --set config.KDIVE_OIDC_ISSUER='https://idp.example/realms/kdive' \
  --set config.KDIVE_OIDC_JWKS_URI='https://idp.example/realms/kdive/protocol/openid-connect/certs' \
  --set config.KDIVE_S3_ENDPOINT_URL='https://s3.example' \
  --set secrets.secretName=kdive-remote-tls \
  --set config.KDIVE_REMOTE_LIBVIRT_URI='qemu+tls://host.example/system' \
  --set config.KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF=clientcert.pem \
  --set config.KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF=clientkey.pem \
  --set config.KDIVE_REMOTE_LIBVIRT_CA_CERT_REF=cacert.pem
```

The migrate Job runs as a **`pre-install`/`pre-upgrade` hook** on the external-backend path, and
its ConfigMap is a hook-weighted pre-install resource so the migrate pod has its env before the
hook runs (this ordering was a chart bug, fixed in #312). Migrations are forward-only and must be
backward-compatible (ADR-0015), so a rollback is image-only.

Watch the rollout:

```bash
kubectl rollout status deploy/kdive-kdive-server
kubectl get pods -l app.kubernetes.io/name=kdive
```

> **Updating config after install.** `config.*` renders into a ConfigMap the pods read **once**
> via `envFrom` at start. After a `helm upgrade` that changes config, restart the pods
> (`kubectl rollout restart deploy/kdive-kdive-server deploy/kdive-kdive-worker
> deploy/kdive-kdive-reconciler`) or they keep the old values. The ConfigMap is also a pre-install
> hook, so `helm upgrade --no-hooks` **skips** it — use a hooked upgrade for config changes.

## 5. Reach the MCP endpoint

The chart's only Service fronts the server's MCP port `8000` as a **ClusterIP** (the per-process
`/livez`/`/readyz`/`/metrics` aux ports are deliberately pod-local and not exposed). To reach MCP
from outside the cluster, either port-forward:

```bash
kubectl port-forward svc/kdive-kdive-server 8000:8000
# MCP at http://127.0.0.1:8000/mcp
```

…or expose it for a longer-lived setup by patching the Service to `NodePort` (or fronting it with
an Ingress/LoadBalancer):

```bash
kubectl patch svc kdive-kdive-server -p '{"spec":{"type":"NodePort"}}'
kubectl get svc kdive-kdive-server -o jsonpath='{.spec.ports[0].nodePort}'   # e.g. 30800
# MCP at http://<node-ip>:<nodePort>/mcp
```

FastMCP serves at **`/mcp`** — a bare host returns a 307/session error, so any client base URL
must end in `/mcp`.

## 6. Verify

Each Deployment carries a readiness probe against its `/readyz` aux endpoint, so the kubelet
already evaluates health — a `Ready` pod has a passing `/readyz` (its backend set: DB, object
store, OIDC). A pod stuck `0/1 Running` is failing readiness; `kubectl describe pod` shows which
backend, which you fix via the corresponding `config.*`/Secret.

```bash
# Ready = /readyz green (the aux listener is pod-local, not fronted by a Service):
kubectl get pods -l app.kubernetes.io/name=kdive

# An authenticated MCP call (needs a token from your OIDC issuer with audience `kdive`):
curl -s -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://<mcp-host>/mcp | head
```

## 7. Endpoints every party must agree on

The OIDC issuer and S3 endpoint are bound into tokens (`iss`) and presigned URLs, so the value the
**in-cluster pods** use, the value **clients** use, and (for remote-libvirt) the value the
**guest** uses to upload a vmcore must all resolve to the same service. If you expose them on
NodePorts, set `config.KDIVE_OIDC_ISSUER` / `config.KDIVE_S3_ENDPOINT_URL` to the externally
routable URL all three can reach — not a cluster-internal name only the pods resolve.

## 8. Remote-libvirt host prerequisites

Deploying remote-libvirt also requires the operator-side setup the
[remote-live-stack runbook](remote-live-stack.md) covers: worker→host mutual TLS, the gdbstub-port
ACL, object-store reachability from the guest, and an operator-staged base-OS image on the host's
storage pool. Those are host-side obligations independent of this chart install.

## 9. Teardown

```bash
helm uninstall kdive
kubectl delete pvc -l app.kubernetes.io/name=kdive      # PVCs are not removed by uninstall
kubectl delete secret kdive-remote-tls                  # if created in step 3
```

The external backends you stood up in step 2 are uninstalled separately.
