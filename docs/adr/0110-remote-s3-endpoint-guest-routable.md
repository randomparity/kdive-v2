# ADR 0110 — Remote install/capture preflight the S3 endpoint as guest-routable

- **Status:** Proposed
- **Date:** 2026-06-13
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0082](0082-remote-install-in-guest-kernel.md)
  (the remote install plane that mints a presigned GET and runs the in-guest helper),
  [ADR-0084](0084-remote-control-two-phase-vmcore-retrieve.md) (the two-phase KDUMP capture
  that mints a presigned PUT the guest uploads to),
  [ADR-0078](0078-object-store-in-target-install-seam.md) (the registered-URL artifact
  channel), and [ADR-0087](0087-config-registry.md) (the single `KDIVE_*` registry source —
  the preflight reads the endpoint through it, never a raw env read).
- **Spec:** [`../superpowers/specs/2026-06-13-remote-s3-endpoint-guest-routable.md`](../archive/superpowers/specs/2026-06-13-remote-s3-endpoint-guest-routable.md)
- **Issue:** [#375](https://github.com/randomparity/kdive/issues/375)

## Context

The remote-libvirt install and KDUMP-capture planes have the *guest* move the bytes. `install()`
mints a presigned GET against `KDIVE_S3_ENDPOINT_URL` and the in-guest helper `curl`s the bundle;
KDUMP `capture()` mints a presigned PUT against the same endpoint and the guest uploads the vmcore.
The presigned URL embeds whatever host the boto3 client was built with.

The dev default is `http://localhost:9000` (MinIO co-located with the control plane). For a *remote*
guest that host is the guest's own loopback — there is no object store there — so the in-guest
transfer fails opaquely. The operator sees only a non-zero helper exit (`install_failure` /
`infrastructure_failure`) with no signal that the root cause is a misconfigured endpoint. The correct
value is a control-plane address routable from the remote guest network (in-cluster service endpoint
in the design deployment; a LAN address when driving remote from a workstation, which is off-design).
This was finding F8 of the MCP coverage campaign.

A doc note alone is weak: it is read once at setup and the failure mode is silent and downstream. An
actionable, fast preflight error at the exact moment of misconfiguration is strictly better.

## Decision

Add a shared preflight, `validate_guest_routable_endpoint()`
(`providers/remote_libvirt/endpoint_preflight.py`), and call it from both guest-transfer seams
immediately before minting the presigned URL: `RemoteLibvirtInstall.install()` before `presign_get`,
`KdumpCapturer.capture()` before `presign_put`.

**What it rejects.** A *loopback/localhost* endpoint host — the `localhost` name (case-insensitive)
or a literal loopback IP (`127.0.0.0/8`, `::1`, via `ipaddress.ip_address(...).is_loopback`). It does
**no DNS resolution**: only the literal `localhost` name and literal loopback addresses are rejected,
so the check is deterministic and side-effect-free, and a real hostname is never resolved on the
worker (whose resolver may differ from the guest's anyway).

**The error.** A `CategorizedError(category=CONFIGURATION_ERROR)` whose message names the env var and
whose `details` carries a machine-readable remediation: `env_var="KDIVE_S3_ENDPOINT_URL"`,
`next_action="set KDIVE_S3_ENDPOINT_URL to a control-plane address routable from the remote guest
network"`, and the offending `configured_endpoint` (a non-secret URL). This follows the established
`configuration_error`-names-the-env-var pattern (`object_store_from_env`,
`remote_config_from_env`), and adds a structured `details.next_action` so an agent or operator gets
the literal env-var identifier, not prose.

**Boundary with "unset".** `object_store_from_env()` already raises a `configuration_error` naming
`KDIVE_S3_ENDPOINT_URL` when the endpoint is unset. The preflight no-ops on a blank value, so the
two checks don't double-report: the store builder owns *unset*, the preflight owns *set-to-loopback*.

**Scope.** Only the two guest-transfer seams. host-dump capture streams from the worker (host-side)
and never hands the guest a presigned URL, so it is unaffected. local-libvirt is unaffected — its
seams don't expose a presigned URL to a *remote* guest network; the loopback constraint is specific
to remote guests.

## Consequences

- A loopback endpoint fails *before* any guest round-trip, with an error that names the exact env var
  and the value class to set — replacing an opaque downstream `install_failure`/`infrastructure_failure`.
- The preflight is statically conservative: it catches the dev-default footgun (`localhost`/loopback)
  but cannot catch a routable-looking endpoint that the guest still can't reach (a missed guest→store
  ACL). That residual surfaces as the existing in-guest transfer failure and stays documented in the
  runbook's bidirectional-reachability note.
- No new config knob, no construction-time dependency: the preflight reads the already-loaded
  registry value at the per-op point config is already read.

## Considered & rejected

- **Active reachability probe from the worker.** Rejected: the worker's routing/DNS is not the
  guest's, so a worker-side probe proves nothing about guest reachability and adds a network
  round-trip and a new failure mode. The static loopback check catches the actual reported footgun.
- **Resolve the hostname and reject any name resolving to loopback.** Rejected: DNS resolution on the
  worker is non-deterministic, environment-dependent, and a side effect in a preflight; `localhost`
  plus literal loopback IPs cover the dev default and any hand-set loopback.
- **Doc-only.** Rejected: the failure is silent and downstream; an actionable runtime error is the
  stronger guardrail (the issue explicitly prefers it). Docs are added *in addition*.
- **Validate in `object_store_from_env()` for every caller.** Rejected: a loopback endpoint is
  correct for *co-located* control-plane traffic (server/worker reaching MinIO on the same host); it
  is only wrong when the URL is handed to a *remote guest*. The constraint belongs at the remote
  guest-transfer seam, not in the generic store builder.
