# Running KDIVE on Kubernetes

The Helm chart under `deploy/helm/kdive` deploys the three KDIVE processes
(`server` / `worker` / `reconciler`) plus a `migrate` one-shot Job, against
operator-provided Postgres, S3-compatible object storage, and an OIDC issuer.

The chart's value and flag reference is
[`deploy/helm/kdive/README.md`](../../deploy/helm/kdive/README.md). For an end-to-end
bring-up — building and pushing the image, standing up backends, reaching the MCP
endpoint, and verifying — follow [the Kubernetes deploy runbook](runbooks/kubernetes-deploy.md).

## Install

```bash
helm install kdive deploy/helm/kdive \
  --set image.tag=edge \
  --values my-values.yaml
```

The default image tag is the chart `appVersion`, which tracks the next unreleased version
and has no published image until that version is cut. Installing from a source checkout
needs `--set image.tag=edge` to pin the rolling image; a bare `appVersion` default is
correct only when installing a cut release.

## Secrets and values

Backends are external. Supply their connection details and credentials through
`KDIVE_*` settings and the chart's secret mounts rather than baking them into the image.
Every setting is listed in [the config reference](../guide/reference/config.md); the
chart's README documents which values map to which keys and how the secret is mounted
(non-root containers read the env file at mode 0440 under an `fsGroup`).

The `migrate` Job runs the schema forward before the app workloads start, so the processes
never reach the database ahead of the migration.
