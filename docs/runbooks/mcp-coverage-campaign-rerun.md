# MCP Tool Coverage Campaign — Rerun Runbook

Repeatable procedure to re-run the MCP tool coverage campaign
(`docs/reports/mcp-coverage-campaign-2026-06-13.md`). Drives every reachable MCP tool over the
live transport across local-libvirt, remote-libvirt, and fault-inject, records per-cell
verdicts, and renders the coverage grid. Companion setup reference:
`docs/reports/provider-configuration-requirements.md`.

Committed tooling used here:
- `systems.toml.example` — root-level scaffold for the **systems descriptor** (see below).
- `scripts/coverage_campaign/systems.py` — load the descriptor; `render-env` emits `d1.env`,
  `setup-commands` emits the remote iptables ACL + k8s port-forwards.
- `scripts/coverage_campaign/gridgen.py` — enumerate the 91-tool census from the live app.
- `scripts/coverage_campaign/results.py` — `CellResult` + `merge_and_render` (grid markdown).
- `scripts/coverage_campaign/drive.py` — mint a role token + call one tool over MCP.

**Systems descriptor (do this first):** copy `systems.toml.example` to `systems.toml` at the
repo root (GITIGNORED — it holds host FQDNs/IPs + TLS secret refs) and fill in your environment.
Everything below derives from it via `scripts/coverage_campaign/systems.py`.

Run-local state (gitignored): `artifacts/coverage-campaign/` (results.jsonl, grid.md, manifest,
fetched TLS certs, guest helpers).

## Prerequisites

- Workstation: KVM/libvirt, Docker, `uv`, `gh` authed, this repo.
- Remote host (e.g. ub24-big): libvirt+qemu+`qemu+tls` listener on 16514, a staged base image
  **carrying the in-guest helpers** (`deploy/remote-libvirt-guest-helpers/`), and a gdbstub
  port range. See `docs/runbooks/remote-libvirt-host-setup.md`.
- k8s cluster (D2): kdive deployed via the chart. See `docs/runbooks/kubernetes-deploy.md`.

## Phase 0 — bring-up

### D1 (workstation)
1. `just stack-up`. If it fails with `migration … checksum changed`, the persisted dev volume
   is stale: `docker compose down -v` then `just stack-up` (resets disposable dev data).
2. **Seed the build-config catalog** (else remote `runs.build` fails — F6/#373):
   ```
   uv run python -c "import asyncio,kdive.config as c; from kdive.config.core_settings import DATABASE_URL; from psycopg import AsyncConnection; from kdive.build_configs.seed import seed_build_configs; from kdive.store.objectstore import object_store_from_env; \
   asyncio.run((lambda: (lambda conn: None))())" # see seed snippet in the report; or run: kdive seed-demo
   ```
   (Simplest: open an async connection + object store and call `seed_build_configs`.)
3. Render `d1.env` from the root `systems.toml` (fill it from `systems.toml.example` first):
   `uv run python -m scripts.coverage_campaign.systems render-env > artifacts/coverage-campaign/d1.env`.
   (`setup-commands` likewise prints the remote iptables ACL + k8s port-forwards.)
4. Start the host stack **under bash** (env.sh uses `${BASH_SOURCE[0]}`, empty under zsh →
   broken `//.live-build` paths):
   ```
   set -a; source scripts/live-stack/env.sh; source artifacts/coverage-campaign/d1.env; set +a
   ./scripts/live-stack/start.sh --daemon
   ```

### Remote-libvirt → remote host
5. Place the TLS client cert/key/CA under `$KDIVE_SECRETS_ROOT` as
   `remote-clientcert.pem` / `remote-clientkey.pem` / `remote-ca.pem` (fetch from the host or
   generate per `remote-libvirt-host-setup.md`).
6. **Firewall ACL** — the worker's source subnet must reach the host's `16514` **and** the
   gdbstub range (e.g. `47000:47099`). If driving from a workstation outside the worker pool
   subnet, add it on the host (revert at the end):
   ```
   sudo iptables -I INPUT -s <workstation-ip> -p tcp --dport 16514 -j ACCEPT
   sudo iptables -I INPUT -s <workstation-ip> -p tcp --dport 47000:47099 -j ACCEPT
   ```
7. **Install the in-guest helper** into the staged base image (F7/#374), with the SELinux fix:
   ```
   virt-customize -a <base>.qcow2 \
     --copy-in deploy/remote-libvirt-guest-helpers/kdive-install-kernel:/usr/local/sbin/ \
     --run-command 'chown root:root /usr/local/sbin/kdive-install-kernel' \
     --run-command 'chmod 0755 /usr/local/sbin/kdive-install-kernel' \
     --run-command 'restorecon -v /usr/local/sbin/kdive-install-kernel'
   ```

### D2 (k8s)
8. `helm upgrade kdive deploy/helm/kdive -n <ns> --reuse-values --set config.KDIVE_FAULT_INJECT=1`.
9. **Enable role tokens** on the demo OIDC (F2/#369): `kubectl set env deploy/<oidc> JSON_CONFIG-`
   then restart server/oidc/worker/**reconciler** (the reconciler registers providers).
10. Drive D2 via port-forwards (`svc/<oidc>:8080`, `svc/<server>:8000`) and make the in-cluster
    issuer host resolve to the forward so the minted `iss` matches (an in-process
    `socket.getaddrinfo` override, or `/etc/hosts`).

### Preflight
11. **Identity gate:** mint a token for each of the six roles and make one authenticated read.
12. **Providers:** `resources.list` shows the expected providers on each deployment (restart the
    reconciler if one is missing).

## Phase 1 — drive the arcs

- **Census:** `uv run python -c "from scripts.coverage_campaign.gridgen import generate_rows; print(len(generate_rows()))"`.
- **Reads sweep / platform-ops / RBAC denials:** use `scripts/coverage_campaign/drive.py`
  (read-only: also `kdivectl tool call`). Tools with a `name` argument collide with the driver's
  positional — call via the underlying client with an args dict.
- **Lifecycle (remote build proven):** drive the `live_stack` remote spine —
  `uv run python -m pytest tests/integration/test_remote_live_stack.py -m live_stack -v -s`
  (build→boot needs Phase-0 steps 5–7 + F8; install/boot is the open item).
- Append each verdict to `artifacts/coverage-campaign/results.jsonl` as
  `{"tool","provider","verdict","issue","deployment","arc"}`.

**Leaked-state hygiene (F4/#371, F5/#372):** a failed lifecycle run leaks an `active` allocation
and possibly a host domain that the reaper does **not** collect. Between runs:
`ops.force_release` each active allocation, then `virsh undefine` any leftover `kdive-*` domain.

## Phase 2 — render + report

```
uv run python -c "
import json
from scripts.coverage_campaign.gridgen import generate_rows
from scripts.coverage_campaign.results import CellResult, merge_and_render
rows = generate_rows()
res = [CellResult(**{k:v for k,v in json.loads(l).items() if k in ('tool','provider','verdict','issue')}) for l in open('artifacts/coverage-campaign/results.jsonl')]
print(merge_and_render(rows, res))
" > /tmp/grid.md
```
Update the report grid + narrative; file any new findings (`gh issue create`).

## Phase 3 — cleanup

- `ops.force_release` all active allocations; `virsh undefine` leftover `kdive-*` domains.
- Remote host: remove the iptables ACCEPTs added in step 6.
- Workstation: `rm -rf ~/.kdive-secrets ~/.kdive-pkipath`; remove any `/etc/hosts` campaign line.
- D2: revert the OIDC `JSON_CONFIG` patch + `KDIVE_FAULT_INJECT` if restoring the pristine demo.
- Stop the host stack (`.live-stack.pid`), `docker compose down`, and any port-forwards.
