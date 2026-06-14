# KDIVE MCP Tool Coverage Campaign — Report (in progress)

- Date: 2026-06-13
- Branch: `test/mcp-coverage-campaign`
- Spec: `docs/superpowers/specs/2026-06-13-mcp-coverage-campaign-design.md`
- Plan: `docs/superpowers/plans/2026-06-13-mcp-coverage-campaign.md`
- Companion: `docs/reports/provider-configuration-requirements.md`
- Status: **execution in progress** — Arc 0 complete (all deployments), arc execution underway.

This report is updated as the campaign runs. The tool census is 91 registered MCP tools
(65 `implemented`, 26 `partial`).

## Deployment topology (as built)

| | D1 workstation | D2 k8s `kdive-demo` | ub24-big |
|---|---|---|---|
| Control plane | host processes | helm rev 3 (`:edge`) | — (target) |
| Reach | `http://127.0.0.1:8000/mcp` | port-forward `:18000` | qemu+tls |
| local-libvirt | ✅ present | — (no KVM) | — |
| fault-inject | ✅ | ✅ (enabled this session) | — |
| remote-libvirt | ✅ (TLS; worker-subnet ACL added) | ✅ (nodes pre-allowed) | ✅ |
| Identity gate (6 roles) | ✅ | ✅ (OIDC reconfigured) | — |

Per `provider-configuration-requirements.md`, bringing these up surfaced several requirements
(reconciler-restart to register a provider, the 16514+gdbstub firewall ACL, the `pkipath`
layout, the demo-OIDC role-claim gap).

## Confirmed working over the real MCP transport (PASS)

- **Reads (D1)**: `resources.list`, `resources.availability`, `systems.list`, `jobs.list`,
  `images.list`, `inventory.list`, `fixtures.list`, `shapes.list`, `build_hosts.list`,
  `vmcore.list`, `accounting.report_all_projects`, `accounting.report_granted_set`,
  `accounting.usage_project`. Plus 11 get-tools reachable (correct `configuration_error` on a
  bogus id; PASS deferred to real objects).
- **Identity / RBAC plumbing**: all six roles authenticate on D1 *and* D2.
- **Platform ops**: `ops.reconcile_now` (clean accounting envelope), `ops.force_release`
  (break-glass; correctly released leaked allocations — used 3×).
- **Remote lifecycle (partial)**: `allocations.request` and `systems.provision` on
  remote-libvirt both PASS — a disk-image domain provisions on ub24-big. The arc then fails at
  `runs.build` (finding F6).

## Findings (to file as issues)

| # | Category | Finding |
|---|---|---|
| F1 | GAP-TOOL | The shipped `kdivectl tool call` is **read-only** by construction; it cannot drive any mutating/destructive tool. An agent restricted to the shipped client cannot reach most of the surface. |
| F2 | GAP / product | The Helm **demo OIDC cannot mint role tokens** — `interactiveLogin:false` + a fixed no-role claim set is **hardcoded in the chart template** (`templates/demo/oidc.yaml`), not a value. A stock demo deployment cannot exercise the RBAC/authz surface at all. |
| F3 | GAP (fixture) | **local-libvirt lifecycle is blocked**: `KDIVE_GUEST_IMAGE` is unset and the guest rootfs is built by `python -m kdive build-rootfs`, historically a stub. Reads/structure work; build→boot→debug→crash→capture cannot run locally. |
| F4 | BUG / reaper | A **leaked `active` allocation is never reaped**. A failed/interrupted lifecycle run leaves an `active` allocation; `ops.reconcile_now` does not collect it (`expired_allocations:0` — it is active, not expired), so it permanently holds the remote `cap=1` slot and blocks **all** future remote allocations. Only `ops.force_release` recovers it. Observed 3× this session. |
| F5 | GAP / reaper | An **orphaned remote domain is invisible to the reaper**. A libvirt domain with no DB record on any control plane (orphaned beyond the System lifecycle) is not collected by the `leaked_domains` reaper (`ops.reconcile_now` reported `leaked_domains:0` with a live orphan present). No MCP tool reaps it; it had to be removed out-of-band (`virsh undefine`). |
| F6 | BUG / config | **Remote `runs.build` fails on an unknown build-config catalog entry.** The standard kdump-enabled remote profile references build-config `{kind: catalog, provider: system, name: "kdump"}`, which the default catalog (`build_configs/defaults.py`) rejects as `unknown build-config catalog entry` (`configuration_error`). A stock kdump remote build cannot complete without provisioning that fragment. |

F4 and F5 together mean a single failed remote run wedges the provider until an operator
manually breaks glass and reaps the host — directly the §5.1 partial-failure / two-control-plane
hazards the spec predicted, now reproduced.

## Not yet executed

Full lifecycle on remote-libvirt past `runs.build` (blocked on F6), the debug / capture / control
arcs, the RBAC denial cross-cut, fault-inject error paths, and the targeted D2 cells. The
coverage grid (per `scripts/coverage_campaign/`) is assembled once those arcs run.

## Cleanup tracked for campaign end

- ub24-big: remove the iptables ACCEPTs for `192.168.2.8` (`16514` + `47000:47099`).
- workstation: `rm -rf ~/.kdive-secrets ~/.kdive-pkipath`; stop the host stack + port-forwards.
- D2: the demo OIDC `JSON_CONFIG` removal + `KDIVE_FAULT_INJECT` were applied to evaluate RBAC;
  revert if restoring the pristine demo.
