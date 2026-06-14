# KDIVE documentation

KDIVE is an MCP platform for the Linux kernel build-boot-debug lifecycle. This
index is organized by audience: pick the tier that matches what you are doing.

## Use KDIVE — agents and users

Drive KDIVE by calling its MCP tools and reading the structured response each
tool returns.

| Page | What it covers |
|---|---|
| [Agent guide](guide/index.md) | How an agent drives the tool surface end to end |
| [Agent onboarding](guide/agents/index.md) | Connecting an MCP client and the example configs |
| [Tool reference](guide/reference/index.md) | Per-namespace parameter reference |

## Run KDIVE — operators

Deploy and operate the `server`, `worker`, `reconciler`, and `migrate`
processes against your backends.

| Page | What it covers |
|---|---|
| [Operating index](operating/index.md) | Entry point for every operating page |
| [Install](operating/install.md) | Install paths, host prerequisites, run modes |
| [Docker Compose](operating/docker-compose.md) | App tier plus dev backends in one graph |
| [Kubernetes (Helm)](operating/kubernetes.md) | The chart for the processes and migrate Job |
| [systemd](operating/systemd.md) | Running the processes as host services |
| [Providers](operating/providers/local-libvirt.md) | What each libvirt provider needs |

## Develop KDIVE — contributors

| Page | What it covers |
|---|---|
| [Contributing](../CONTRIBUTING.md) | Dev loop, branch and commit conventions, the PR gate |
| [Releasing](development/releasing.md) | Versioning policy and the release process |
| [Architecture](../ARCHITECTURE.md) | Summary of the authoritative design |

## Canonical references

| Page | What it covers |
|---|---|
| [Top-level design](design/top-level-design.md) | The authoritative architecture and rationale |
| [Architecture decision records](adr/) | Accepted ADRs; supersede, never edit in place |
