# Operating KDIVE

KDIVE runs as three processes — `server`, `worker`, `reconciler` — plus a
`migrate` one-shot, on top of operator-provided backends (Postgres, an
S3-compatible object store, and an OIDC issuer). These pages cover how to install
the code, the three deployment shapes, the libvirt providers, and the live
runbooks.

## Install and run modes

| Page | What it covers |
|---|---|
| [Install](install.md) | Install paths, host prerequisites, and the run modes |
| [Docker Compose](docker-compose.md) | App tier plus dev backends in one graph |
| [Kubernetes (Helm)](kubernetes.md) | The chart for the three processes and the migrate Job |
| [systemd](systemd.md) | Running the processes as host services |

## Providers

| Page | What it covers |
|---|---|
| [Local libvirt](providers/local-libvirt.md) | Single-host libvirt provider prerequisites |
| [Remote libvirt](providers/remote-libvirt.md) | Remote libvirt host wiring and prerequisites |

## Runbooks

Step-by-step procedures for live runs and operational tasks.

| Runbook | What it covers |
|---|---|
| [Live stack](runbooks/live-stack.md) | Bring up the HTTP live-stack against compose backends |
| [Remote live stack](runbooks/remote-live-stack.md) | Live stack driving a remote libvirt host |
| [Remote libvirt host setup](runbooks/remote-libvirt-host-setup.md) | Preparing a remote libvirt host |
| [Four-method live run](runbooks/four-method-live-run.md) | Exercising all four crash-capture methods |
| [Image lifecycle](runbooks/image-lifecycle.md) | Building, publishing, and pruning base images |
| [Kubernetes deploy](runbooks/kubernetes-deploy.md) | Deploying the Helm chart to a cluster |
| [kdivectl](runbooks/kdivectl.md) | Operating the admin CLI |
| [Doctor exit criterion](runbooks/doctor-exit-criterion.md) | The doctor readiness check |
| [MCP coverage campaign rerun](runbooks/mcp-coverage-campaign-rerun.md) | Re-running the MCP tool coverage sweep |
