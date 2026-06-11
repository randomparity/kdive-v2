# Runbook: M2.3 `doctor` exit-criterion / band-gate proof

The operator-run proof that `kdivectl doctor` is a correct deployment preflight: on a fresh
two-host setup, an operator who is **not** the author runs `doctor` against each of the four
M2 contract faults and records that `doctor` names the **exact fix** for each. This is the
band-gate (M3-entry) evidence: `doctor` is built in this same band, so it cannot be its own
sole oracle — a human-run record on real infrastructure closes that loop.

See [ADR-0091](../adr/0091-doctor-diagnostics-model.md) (diagnostics model, exit codes) and
[ADR-0090](../adr/0090-opentelemetry-adoption-service-health.md) (health endpoints). The
CI-tier proof of the same four faults (seeded through the real checks with fakes) lives in
`tests/integration/test_doctor_exit_criterion.py` and runs in normal CI; this runbook is the
**live** complement that exercises the one hop CI cannot — a real guest's egress to
object-store. Running this runbook is band-gate evidence, not a CI check.

## What CI already proves (and what it cannot)

`tests/integration/test_doctor_exit_criterion.py` drives all four checks through the real
`DiagnosticsService` → `ops.diagnostics` → `doctor` exit-code path and asserts, for each
seeded fault, the **exact** fix string and the gate-safe exit code:

| fault | check | exact fix asserted | doctor exit |
|-------|-------|--------------------|-------------|
| broken TLS chain | `provider_tls` | `provider cert not signed by configured CA <ca>; reissue or set KDIVE_PROVIDER_CA` | `1` |
| closed gdb ACL | `gdbstub_acl` | `gdbstub port range <range> on <host> blocked; open the host firewall / ACL for it` | `1` |
| missing secret ref | `secret_ref` | `secret ref does not resolve under KDIVE_SECRETS_ROOT; create the file-ref or fix the path` | `1` |
| blocked guest→object-store egress | `guest_egress` | `guest bridge -> object-store blocked (likely host FORWARD DROP); allow the guest subnet -> MinIO` | `1` |

CI also proves the error-vs-fail distinction (an unreachable provider reads as `error`, exit
`6` — distinct from a `fail`'s exit `1`, so a gate never goes green on a check that could not
run), that a `fail` dominates a co-occurring `error` (exit `1`), and that `/readyz` flips
not-ready with a backend down on **all three** processes (server on PG+MinIO+OIDC; worker and
reconciler on PG+MinIO, no OIDC).

What CI **cannot** prove: the `guest_egress` check in CI execs against a *fake* probe guest.
The headline M2 egress fault — a guest→object-store path silently dropped by an unrelated host
`FORWARD` policy — can only be observed from inside a **real** guest on the provider bridge.
That is what this live run adds.

## Precondition: an operator-staged remote probe image (NAMED, not assumed)

The `guest_egress` check provisions a tiny short-lived guest on the target provider and execs
a presigned `HEAD`/`PUT` to object-store from inside it. On **local-libvirt** the probe reuses
the existing fixture image, so that tier is self-contained. The **remote** provider has **no
managed probe image until M2.4** (image/rootfs lifecycle is out of this band's scope, see the
epic's "Out of scope"). Therefore the remote `--with-egress` run requires the operator to
**stage a bootable probe image on the remote provider first**. This is an explicit gate
precondition — not an assumed capability — and `doctor --with-egress` against a remote
deployment with no probe-guest seam wired **fails fast** with a `CONFIGURATION_ERROR` (it does
not silently drop the opt-in check; see `default_service_factory` in
`src/kdive/diagnostics/service.py`).

Stage the image and wire the probe-guest seam before running the egress step:

1. Build/obtain a minimal Linux image with a presign-capable HTTP client (`curl`) and the
   in-guest exec channel the remote provider uses (the guest-agent path from M2 issue #202).
2. Publish it to the remote provider host so a `kdive-egress-probe-*` domain can boot from it.
3. Wire a probe-guest-backed `ServiceFactory` in the remote deployment (replacing the
   fail-fast default) so `with_egress=True` assembles a real `GuestEgressCheck`.

If the image is not staged, run only the three read checks (`doctor` without `--with-egress`)
and record the egress probe as **not exercised — image not staged**, rather than as a pass.

## The live run (operator-not-the-author, fresh two-host setup)

Bring up a fresh two-host remote-libvirt deployment per
[remote-live-stack.md](remote-live-stack.md): the kdive app tier (server/worker/reconciler +
Postgres/MinIO/OIDC) on the control host, and the remote-libvirt provider on a second host.
Authenticate as a `platform_operator` (`doctor` is operator-gated):

```bash
export KDIVE_SERVER_URL="https://kdive.example.com/mcp"
kdivectl login --platform-role platform_operator     # or export KDIVE_TOKEN=...
```

Run `doctor` once per fault below. Capture the **full JSON verdict** (`--json`) and the
process **exit code** for each — both are independently-checkable evidence. The four faults
are seeded on the deployment (not in kdive); after recording each, restore the healthy
configuration before seeding the next so the runs are independent.

### Read-only baseline + three read faults

```bash
kdivectl doctor --provider remote-libvirt --json ; echo "exit=$?"
```

1. **Broken TLS chain.** Reissue the provider cert under a CA the deployment does not trust
   (or point `KDIVE_PROVIDER_CA` at the wrong CA). Expect `provider_tls` → `fail` and the
   exact `...reissue or set KDIVE_PROVIDER_CA` fix; `doctor` exits `1`.
2. **Closed gdb ACL.** Add a host firewall rule dropping the configured gdbstub port range on
   `config.gdb_addr`. Expect `gdbstub_acl` → `fail` and the exact `...open the host firewall /
   ACL for it` fix; `doctor` exits `1`.
3. **Missing secret ref.** Remove (or misname) a configured secret file under
   `KDIVE_SECRETS_ROOT`. Expect `secret_ref` → `fail` and the exact `...create the file-ref or
   fix the path` fix; `doctor` exits `1`. Confirm a **per-tenant** ref that is missing is
   counted in the aggregate but its identifier is **never** printed in the verdict.

### The egress fault (requires the staged probe image)

```bash
kdivectl doctor --provider remote-libvirt --with-egress --json ; echo "exit=$?"
```

4. **Blocked guest→object-store egress.** With the probe image staged, add a host `FORWARD
   DROP` for the guest subnet → MinIO path. Expect `guest_egress` → `fail` and the exact
   `guest bridge -> object-store blocked (likely host FORWARD DROP); allow the guest subnet ->
   MinIO` fix; `doctor` exits `1`. Then open the `FORWARD` path and re-run: `guest_egress` →
   `pass`, `doctor` exits `0`.

### The error-vs-fail distinction (live)

Power down the remote provider host (or block its management port) and run
`doctor --provider remote-libvirt`. The worker-vantage checks must read as **`error`** (the
host is simply down — the contract may be fine), `doctor` exits **`6`** (distinct from a
`fail`'s `1`), and **no** check emits a fix. This proves `doctor` does not emit a confident
wrong remediation when a dependency is merely unreachable.

## Recording the evidence (each probe independently checkable)

The band gate requires each probe's **individual** result, not just an overall verdict. For
each of the four faults plus the error-vs-fail and the egress-pass-after-fix runs, record:

- the seeded condition (what was broken, on which host);
- the `doctor --json` verdict row for the relevant `check` (`status`, `detail`, `fix`,
  `provider`) — copy it verbatim;
- the process **exit code** (`echo "exit=$?"`);
- the operator (must not be the author) and the date.

A row reads as **passing band-gate evidence** only when the seeded fault produced the exact
fix string from the table above (or the documented `error`/exit-`6` for the unreachable run).
A run that could not be performed (e.g. the remote probe image was not staged) is recorded as
**not exercised**, with the reason — never silently as a pass.

Suggested evidence stub (one block per run):

```text
fault:        <broken TLS chain | closed gdb ACL | missing secret ref | blocked egress | provider unreachable>
seeded on:    <host / config change>
check:        <provider_tls | gdbstub_acl | secret_ref | guest_egress>
status:       <fail | error | pass>
fix:          <verbatim fix string, or "(none — error)">
doctor exit:  <0 | 1 | 6>
operator:     <name, not the author>          date: <YYYY-MM-DD>
```

## Relation to the CI proof

| | CI tier (`tests/integration/test_doctor_exit_criterion.py`) | Live band-gate run (this runbook) |
|---|---|---|
| four faults flagged with exact fix | yes (seeded through the real checks, fakes at the leaf) | yes (seeded on real infra) |
| error-vs-fail / exit 6 vs 1 | yes (unreachable provider) | yes (real provider powered down) |
| `/readyz`-down on all three processes | yes (the real per-process dependency-set builders) | yes (drop a backend, scrape `/readyz`) |
| guest→object-store egress | fake probe guest (no real `FORWARD` hop) | **real guest** on the provider bridge |
| who runs it | CI | operator, not the author |

CI is the always-on regression guard; this runbook is the one-time-per-band human proof on
real infrastructure that closes the "`doctor` cannot be its own oracle" gap.
