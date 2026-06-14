# Spec — Remote install/capture preflight: S3 endpoint must be guest-routable

- Date: 2026-06-13
- Issue: [#375](https://github.com/randomparity/kdive/issues/375)
- ADR: [`../../adr/0110-remote-s3-endpoint-guest-routable.md`](../../adr/0110-remote-s3-endpoint-guest-routable.md)
- Status: accepted

## Problem

The remote-libvirt install and kdump-capture planes have the *guest* transfer the bytes:

- `install()` mints a presigned **GET** against `KDIVE_S3_ENDPOINT_URL` and the in-guest helper
  `curl`s the bundle from it.
- kdump `capture()` mints a presigned **PUT** against the same endpoint and the in-guest helper
  uploads the vmcore to it.

The presigned URL embeds the endpoint host the boto3 client was built with. The dev default is
`http://localhost:9000` (MinIO on the control-plane host). For a **remote** guest that host is the
guest's *own* loopback — there is no MinIO there — so the in-guest `curl` fails opaquely (connection
refused / 404 against nothing), and the operator sees only a non-zero helper exit
(`install_failure` / `infrastructure_failure`) with no hint that the real cause is a misconfigured
endpoint. This was finding F8 of the MCP coverage campaign
(`docs/reports/mcp-coverage-campaign-2026-06-13.md`).

The correct value is a control-plane address **routable from the remote guest network** — not a
loopback/localhost address. In the design deployment (worker in-cluster, near the store) this is the
in-cluster service endpoint; when driving remote from a workstation it is off-design and the operator
must point the endpoint at a LAN-reachable address.

## Goals

1. Fail **fast** with an actionable, structured error when a remote install/capture is about to mint
   a presigned URL against a loopback/localhost endpoint, instead of letting the in-guest transfer
   fail opaquely.
2. The error names the exact env var to set (`KDIVE_S3_ENDPOINT_URL`) as a literal identifier in a
   machine-readable `details.next_action`, not buried in prose.
3. Document the requirement in the remote-libvirt host-setup runbook and the
   provider-configuration-requirements report.

## Non-goals

- Validating that a non-loopback endpoint is *actually* reachable from the guest (a network probe
  from the worker cannot observe the guest's routing table; the guest→store ACL is the operator's,
  documented in the runbook). The preflight rejects only the statically-detectable misconfiguration:
  loopback/localhost. A routable-looking-but-unreachable endpoint still surfaces as the existing
  in-guest transfer failure.
- Touching local-libvirt. Its capture/install seams don't hand a presigned URL to a *remote* guest;
  the loopback constraint is specific to the remote guest network.
- The host-dump capture path: it streams from the *worker* (host-side), never minting a presigned URL
  for the guest, so it is correctly unaffected.

## Design

A single shared preflight, `validate_guest_routable_endpoint()`, in a new module
`src/kdive/providers/remote_libvirt/endpoint_preflight.py`:

- Reads `KDIVE_S3_ENDPOINT_URL` via the config registry (`config.get(S3_ENDPOINT_URL)`), keeping the
  `config-guard` (ADR-0087) invariant — no raw env read outside `kdive.config`.
- Parses the host with `urllib.parse.urlsplit`. The host is **loopback** when:
  - it is `localhost` (case-insensitive), or
  - it is an IPv4/IPv6 literal in a loopback range (`127.0.0.0/8`, `::1`), detected with
    `ipaddress.ip_address(...).is_loopback` (no DNS resolution — a hostname is not resolved, only
    literal loopback addresses and the `localhost` name are rejected).
- On a loopback host, raises `CategorizedError(category=CONFIGURATION_ERROR)` whose message names the
  env var and whose `details` carries:
  - `next_action`: the literal `"set KDIVE_S3_ENDPOINT_URL to a control-plane address routable from
    the remote guest network"`,
  - `env_var`: `"KDIVE_S3_ENDPOINT_URL"`,
  - `configured_endpoint`: the offending value (it is a non-secret endpoint URL).
- An unset/blank endpoint is **not** this preflight's concern — `object_store_from_env()` already
  fails with a `configuration_error` naming `KDIVE_S3_ENDPOINT_URL` when it is unset. The preflight
  returns early (no-op) on a blank value so the two checks don't double-report; the store builder owns
  "unset", the preflight owns "set to loopback".

Both guest-transfer seams call the preflight immediately before minting the presigned URL:

- `RemoteLibvirtInstall.install()` — before `presign_get`.
- `KdumpCapturer.capture()` — before `presign_put`.

The preflight runs at the same point the config is already read per-op (`self._config_factory()`),
adding no new construction-time dependency.

## Failure contract

| Condition | Before | After |
|---|---|---|
| `KDIVE_S3_ENDPOINT_URL` = `http://localhost:9000` (or `127.0.0.1`, `::1`) | in-guest `curl` fails → `install_failure` / `infrastructure_failure`, opaque | **fast** `configuration_error` naming the env var, before any guest round-trip |
| `KDIVE_S3_ENDPOINT_URL` = routable LAN/in-cluster address | passes (existing behavior) | passes (unchanged) |
| `KDIVE_S3_ENDPOINT_URL` unset | `object_store_from_env` → `configuration_error` | unchanged (preflight is a no-op on blank) |

## Tests

`tests/providers/remote_libvirt/test_endpoint_preflight.py`:
- loopback variants (`localhost`, `127.0.0.1`, `127.0.0.2`, `::1`, with/without scheme/port) →
  `configuration_error`; assert `details["env_var"] == "KDIVE_S3_ENDPOINT_URL"` and `next_action`
  names the env var.
- routable endpoints (`http://10.0.0.5:9000`, `https://minio.svc.cluster.local`, a public FQDN) →
  no raise.
- blank/unset → no raise (the store builder owns it).

`tests/providers/remote_libvirt/test_install.py` and the kdump-capture tests gain one case each:
a loopback endpoint makes `install()` / `capture()` raise `configuration_error` **before** the
guest agent is touched (the scripted agent records zero argvs).
