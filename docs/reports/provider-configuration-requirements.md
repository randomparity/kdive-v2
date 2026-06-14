# KDIVE Provider Configuration Requirements (test / evaluation setup)

- Date: 2026-06-13
- Audience: someone standing up their own KDIVE control plane to test or evaluate a provider.
- Scope: the **configuration** each provider needs to come online — env vars, secrets, host
  prerequisites, and network rules — plus the non-obvious requirements surfaced while bringing
  up all three providers for the MCP coverage campaign. For deep host-build steps it links the
  existing runbooks rather than repeating them.
- Companion deliverable to `docs/reports/mcp-coverage-campaign-2026-06-13.md`.

This document records configuration only. The control-plane processes themselves
(`python -m kdive {server,worker,reconciler}`), the chart, and the host PKI/build steps are
covered by `docs/runbooks/` — each section links the relevant one.

## 0. Common prerequisites (all providers)

A KDIVE control plane is three processes over shared backing services. Every provider needs
these regardless of which provider you enable:

| Concern | Requirement |
|---|---|
| Processes | `server` (MCP HTTP), `worker` (job queue), `reconciler` (drift + **resource registration**) — all three reading the same config. |
| State DB | Postgres, `KDIVE_DATABASE_URL`. Schema applied via `kdive migrate` (or `scripts/live-stack/apply-migrations.sh`). |
| Object store | S3-compatible (MinIO works): `KDIVE_S3_ENDPOINT_URL`, `KDIVE_S3_BUCKET`, `KDIVE_S3_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. |
| Identity | OIDC: `KDIVE_OIDC_ISSUER`, `KDIVE_OIDC_JWKS_URI`, `KDIVE_OIDC_AUDIENCE`. **Must emit nested `roles` + `platform_roles` claims (ADR-0044)** or RBAC cannot be exercised — see §4. |
| Secrets | File-ref backend (ADR-0027): `KDIVE_SECRETS_ROOT` (default `/var/lib/kdive/secrets`). Secret *refs* are file paths confined under this root; the file's content is the secret value. |
| Config source | All knobs are `KDIVE_*` env (ADR-0087). The dev seam `scripts/live-stack/env.sh` sets workable localhost defaults. |

Runbook for the host-process / compose bring-up: `docs/runbooks/live-stack.md`.

**Resource registration is the reconciler's job.** A provider does not appear in
`resources.list` until the **reconciler** (not the server) has seen its config and registered
the resource. After enabling or reconfiguring a provider, restart the reconciler.

**Seed the build-config catalog (any provider that builds a kernel).** Applying schema
migrations alone does **not** populate the `build_config_catalog`. The default build profile
resolves the `kdump` catalog fragment, so a kernel build (`runs.build`) fails with
`configuration_error: unknown build-config catalog entry` until the seed step runs. The deploy
path (`kdive seed-demo` / the chart's migrate→seed) seeds it; a bare
`scripts/live-stack/apply-migrations.sh` does not. Run the seed (it is idempotent, sha256-gated,
and needs `KDIVE_S3_*` configured) before building.

**Build host prerequisites (kernel builds).** `rsync` must be installed (the warm kernel tree
is mirrored into the workspace with `rsync -a --delete`). `KDIVE_KERNEL_SRC` must be an
**absolute** path to an existing kernel tree. `KDIVE_BUILD_WORKSPACE` must be a writable
directory. Gotcha: `scripts/live-stack/env.sh` derives these paths from bash's
`${BASH_SOURCE[0]}` — **source it under bash, not zsh**, or the repo root resolves empty and the
paths collapse to `//.live-build`, failing with `build workspace mkdir failed` (Permission
denied at filesystem root). Set `KDIVE_BUILD_WORKSPACE` / `KDIVE_BUILD_COMPONENT_ROOTS`
explicitly to absolute paths when driving from a non-bash shell.

## 1. local-libvirt (default provider)

The default, always-registered provider. No opt-in env — if libvirt is present it is offered.

| Requirement | Detail |
|---|---|
| Host | KVM / nested-virt, a running libvirt daemon (`virtqemud` or `libvirtd`), `qemu-system-x86`. |
| Build deps | `libvirt-dev` headers (`libvirt-python` compiles against them), plus the kernel build toolchain if building images locally. |
| Build env | `KDIVE_BUILD_WORKSPACE`, `KDIVE_BUILD_COMPONENT_ROOTS`, `KDIVE_KERNEL_SRC`, `KDIVE_INSTALL_STAGING` (see `env.sh` for shapes). |
| Opt-in | none. |
| Capture methods advertised | `CONSOLE`, `HOST_DUMP`, `GDBSTUB` — **3 of 4; no KDUMP** (`providers/local_libvirt/composition.py`). |
| Build mechanisms | worker-local (default), ssh build-host, ephemeral-libvirt build VM, local-libvirt-over-transport. |

Verify: with the stack up, an authenticated `resources.list` shows a `local-libvirt` resource.

## 2. fault-inject (failure-path provider)

A synthetic provider for exercising error envelopes / `error_category` values and control/capture
failure paths. No host prerequisites.

| Requirement | Detail |
|---|---|
| Opt-in | `KDIVE_FAULT_INJECT=1` (accepts `1`/`true`/`yes`; **default off**). |
| Host | none — synthetic. |
| Gotcha | The **reconciler** must have `KDIVE_FAULT_INJECT=1` to register the resource. Setting it only on the server leaves fault-inject absent from `resources.list`; restart the reconciler. |
| Behavior | Faults are seeded and decision-keyed (process-independent hash), so they are reproducible. |

## 3. remote-libvirt (operator-configured opt-in)

Drives a remote libvirt host over `qemu+tls`. This is the most involved provider to configure;
the host-build half is in `docs/runbooks/remote-libvirt-host-setup.md`. The control-plane
config and the easy-to-miss network rule are below.

### 3.1 Control-plane env

The opt-in gate is a **declared `[[remote_libvirt]]` instance** in `systems.toml` (ADR-0112,
`KDIVE_SYSTEMS_TOML`). The connection identity is instance fields, not env:

| `[[remote_libvirt]]` field | Value |
|---|---|
| `uri` | `qemu+tls://<host-fqdn>/system` — the FQDN must match the server cert CN/SAN. |
| `client_cert_ref` | secret ref → `clientcert.pem` under `KDIVE_SECRETS_ROOT`. |
| `client_key_ref` | secret ref → `clientkey.pem`. |
| `ca_cert_ref` | secret ref → `cacert.pem`. |
| `gdb_addr` | gdbstub listen address — required field, fails closed if unset. |
| `gdbstub_range` | per-System port range, e.g. `47000:47099`. |
| `base_image` | an `[[image]]` name (the staged base volume). |
| `concurrent_allocation_cap` | per-host cap (default `1`). |

The libvirt host knobs the inventory model does not carry stay env (defaults shown):
`KDIVE_REMOTE_LIBVIRT_STORAGE_POOL=default`, `KDIVE_REMOTE_LIBVIRT_NETWORK=default`,
`KDIVE_REMOTE_LIBVIRT_MACHINE=pc`.

Capture methods advertised: **all four** — `KDUMP`, `HOST_DUMP`, `GDBSTUB`, `CONSOLE`
(`providers/remote_libvirt/composition.py`).

### 3.2 TLS secrets

Mutual TLS. Place three files under `KDIVE_SECRETS_ROOT` and point the `*_REF` settings at
their root-relative names:

- `cacert.pem` — the CA that signed both server and client certs.
- `clientcert.pem` — the worker's client cert (the runbook uses `CN=kdive-worker`).
- `clientkey.pem` — the matching private key (mode `0600`).

On Kubernetes, supply these as a `Secret` and set the chart's `secrets.secretName`; the chart
mounts it read-only at `secrets.mountPath` and points `KDIVE_SECRETS_ROOT` there.

**`pkipath` layout gotcha (manual verification only):** libvirt's `?pkipath=DIR` override
expects `clientkey.pem` **directly in `DIR`**, not in `DIR/private/`. KDIVE handles cert
placement internally, but when sanity-checking by hand with `virsh`, put the key directly in the
pki dir or you get `Unable to read TLS confirmation`/`No certificate was found`:
```
virsh -c "qemu+tls://<host-fqdn>/system?pkipath=<dir>" hostname
```

### 3.3 Network — the easy-to-miss requirement

The worker must reach the host on **two** port sets, and a hardened host restricts both to the
worker pool's source subnet (ADR-0079):

- **`16514`** — the libvirt TLS control channel.
- **the gdbstub range** (default **`47000–47099`**) — the live-debug transport.

If the host enforces a source-IP ACL, the worker's subnet must be allowed for **both**. A common
trap: open only `16514`, and provisioning/boot work while `debug.start_session` over gdbstub
hangs. Example host ACL (iptables), allowing a worker subnet:
```
iptables -A INPUT -s <worker-subnet> -p tcp --dport 16514 -j ACCEPT
iptables -A INPUT -s <worker-subnet> -p tcp --dport 47000:47099 -j ACCEPT
# ... with a DROP for these dports from anywhere else.
```
Symptom of a missed ACL: the TLS connect **times out** (silent DROP) rather than refusing.

**Bidirectional reachability for install + capture — `KDIVE_S3_ENDPOINT_URL` must be
guest-routable.** Remote **install** (the in-guest helper `curl`s the kernel bundle from a
presigned GET) and two-phase kdump **capture** (the guest uploads the vmcore to a presigned PUT)
both mint the URL against `KDIVE_S3_ENDPOINT_URL` and have the *guest* do the transfer. So that
endpoint must be a **control-plane address routable from the remote guest network — not
`localhost`/loopback**. The dev default `http://localhost:9000` is the *guest's* own loopback,
where no object store runs. If the control plane is in Kubernetes with an in-cluster MinIO, that
endpoint must be reachable from the remote guest, or remote install/boot and core-capture
(KDUMP) are blocked even though TLS provisioning works.

The worker **preflights** this (ADR-0110): a remote install/kdump-capture against a
`localhost`/loopback `KDIVE_S3_ENDPOINT_URL` fails fast with a `configuration_error` whose
`details.next_action` names `KDIVE_S3_ENDPOINT_URL`, before any in-guest round-trip — instead of
an opaque in-guest curl failure. The preflight catches only the statically-detectable loopback
case; a routable-looking endpoint the guest still cannot reach (a missed guest→store ACL) surfaces
as the in-guest transfer failure. host-dump capture streams from the *worker*, not the guest, so it
is unaffected. (Originally MCP-campaign finding F8, #375.)

## 4. Kubernetes control plane (deploy/helm/kdive)

The chart deploys server/worker/reconciler with optional bundled backends. Provider config and
the RBAC caveat:

| Concern | How |
|---|---|
| Provider env | Put `KDIVE_*` provider knobs (incl. `KDIVE_FAULT_INJECT`, the `KDIVE_REMOTE_LIBVIRT_{STORAGE_POOL,NETWORK,MACHINE}` host knobs) in chart `config.*` values → rendered to a ConfigMap consumed via `envFrom`. The remote-libvirt connection identity is a `[[remote_libvirt]]` instance in the mounted `systems.toml` ConfigMap (`KDIVE_SYSTEMS_TOML`), not env. |
| TLS secret | `secrets.secretName` → mounted read-only at `secrets.mountPath`; `KDIVE_SECRETS_ROOT` is set to that path automatically. |
| remote-libvirt from k8s | The **cluster node subnet** must be in the host's `16514` + gdbstub ACL allowlist. This is the design-intended remote-driver path (the worker pool runs in-cluster). |
| Reconciler restart | Same rule as §0 — after changing provider config, the reconciler pod must roll to register the resource. |

Runbook: `docs/runbooks/kubernetes-deploy.md`.

### 4.1 OIDC must be able to mint role tokens (campaign finding)

The bundled demo OIDC (`mock-oauth2-server` under `templates/demo/oidc.yaml`) ships a
**hardcoded** `JSON_CONFIG` with `interactiveLogin:false` and a fixed claim set carrying **no
`roles` / `platform_roles`**. Tokens it issues authenticate but carry no role, so **the entire
RBAC / authz surface cannot be exercised** against a stock demo deployment, and the config is
baked into the chart template (not a Helm value).

To test the authz surface you must provide an OIDC that emits the ADR-0044 claims. Either:
- point the deployment at a real IdP configured to emit nested `roles` + `platform_roles`; or
- for evaluation, override the demo OIDC to enable interactive login (which lets a client inject
  the `claims` it wants) instead of the fixed-claim default.

### 4.2 Driving an in-cluster issuer from outside

Minted tokens must carry an `iss` equal to the server's `KDIVE_OIDC_ISSUER` — typically an
in-cluster URL like `http://kdive-kdive-oidc:8080/default`. To drive the deployment from a
workstation (port-forwarded), make that issuer hostname resolve to your port-forward so the
`iss` the IdP stamps matches what the server validates — e.g. an `/etc/hosts` entry, or an
in-process DNS override in the test client. Otherwise every token is rejected as `invalid_token`.

## 5. Client / access notes

- **`kdivectl tool call` is read-only.** It can drive read-only tools but **not** mutating or
  destructive ones (those are reached only through the curated break-glass verbs). Driving the
  full tool surface for test requires a raw MCP-over-HTTP client that can send arbitrary
  authenticated tool calls with a chosen role token.
- A role token must carry the project `roles` map and/or the `platform_roles` array; platform
  tools (`secrets.list`, `ops.*`) require the corresponding platform role.

## 6. Quick enablement matrix

| Provider | Opt-in | Host prereq | Secrets | Network | Capture methods |
|---|---|---|---|---|---|
| local-libvirt | none (default) | KVM + libvirt + qemu | — | — | CONSOLE, HOST_DUMP, GDBSTUB |
| fault-inject | `KDIVE_FAULT_INJECT=1` (+ reconciler restart) | none | — | — | n/a (error-path provider) |
| remote-libvirt | a declared `[[remote_libvirt]]` instance | remote libvirt+qemu+mutual-TLS host | ca/client cert+key under `KDIVE_SECRETS_ROOT` | worker→host `16514` **and** `47000–47099`; guest→object store | KDUMP, HOST_DUMP, GDBSTUB, CONSOLE |
