# MCP Tool Coverage Campaign — Rerun 2026-06-14

Rerun of the MCP tool coverage campaign per `docs/runbooks/mcp-coverage-campaign-rerun.md`.
Drives every reachable MCP tool over the live transport across the three providers
(`local-libvirt`, `remote-libvirt` @ ub24-big, `fault-inject`) on two deployments
(D1 workstation, D2 k8s). Supersedes `mcp-coverage-campaign-2026-06-13.md`.

## Deployments

| Deployment | Providers registered | Identity gate | Notes |
|---|---|---|---|
| D1 workstation | local-libvirt, fault-inject, remote-libvirt | PASS (6 roles) | source-checkout host path; MCP at `127.0.0.1:8000/mcp` |
| D2 k8s (`kdive-demo`) | remote-libvirt, fault-inject | PASS (6 roles) | helm `kdive-0.2.0`/app `0.3.0`; **87 tools vs 91 HEAD** (image predates build-host tools) |

Setup is now captured in a single root-level gitignored descriptor, `systems.toml` (scaffold:
the committed `systems.toml.example`). The loader renders the workstation env and the
per-deployment setup commands:

```
uv run python -m scripts.coverage_campaign.systems render-env > artifacts/coverage-campaign/d1.env
uv run python -m scripts.coverage_campaign.systems setup-commands
```

## Result — pass/fail per provider (deployment-collapsed cells)

| Provider | pass | fail | blocked | cells driven |
|---|---|---|---|---|
| local-libvirt | 16 | 0 | 0 | 16 (reads only) |
| remote-libvirt | 24 | 1 | 4 | 29 |
| fault-inject | 16 | 1 | 13 | 30 |

75 distinct `(tool, provider)` cells driven of the 91-tool census. Full grid:
`artifacts/coverage-campaign/grid.md` (gitignored), reproduced below (non-empty rows).

### Arc status

- **Read sweep (Arc 1):** D1 16/16 read-only tools pass across all three providers; D2 15/16
  (the 16th, `build_hosts.list`, is absent on the older deployed image — version skew, not a bug).
- **remote-libvirt lifecycle (Arc 2):** allocate → provision → **build (real kernel compile)** →
  `complete_build` all **pass**. `runs.install` **fails** (#386) — the build plane is fully
  proven; install/boot/capture is the open frontier.
- **fault-inject lifecycle (Arc 2):** **blocked** — `allocations.request(kind=fault-inject)`
  fails `configuration_error` (#385); no synthetic lifecycle is reachable over MCP.
- **local-libvirt lifecycle (Arc 2):** not driven — no guest image (#370, build-rootfs stub).

## Findings

### New this run

- **#385** — the discovered fault-inject `Resource` omits `vcpus`/`memory_mb` capabilities, so
  `allocations.request(kind=fault-inject)` is denied `configuration_error (vcpus=None)`. The
  whole fault-inject lifecycle is unreachable over MCP. CI hides this by seeding the caps directly.
- **#386** — remote `runs.install` fails: the in-guest `kdive-install-kernel` helper now runs
  (so #374 is resolved) but exits non-zero, and the `install_failure` error surfaces only
  `exit_status`, not guest stderr — blocking diagnosis. Build plane fully proven upstream.

### Reconfirmed (existing)

- **#371** leaked `active` allocation never reaped — a failed lifecycle run wedges the remote
  cap=1 slot; only `ops.force_release` recovers. Hit twice this run.
- **#372** orphaned remote domain invisible to the reaper — a `kdive-*` domain survived a failed
  install (later reaped between checks; cleaned by hand).
- **#373** build-config catalog needs seeding — bare `stack-up` migrations don't seed it;
  `runs.build` fails `unknown build-config catalog entry 'kdump'` until
  `_seed_build_configs_step(KDIVE_DATABASE_URL)` runs. **This step is easy to miss — promote it.**
- **#369** demo OIDC ships hardcoded `interactiveLogin:false`; role-token minting on D2 still
  requires removing `JSON_CONFIG` and restarting the auth/provider tier.

### Setup deltas worth folding into the runbook / descriptor

- The ssh login for ub24-big is `dave@ub24-big.prod.pdx.drc.nz` (FQDN + `dave` user), not
  `ub24-big@<ip>`. Corrected in `systems.toml`.
- The remote TLS client PKI lives on the host at `/home/dave/kdive-pki/`
  (`cacert.pem`/`clientcert.pem`/`clientkey.pem`) → stage as
  `remote-ca.pem`/`remote-clientcert.pem`/`remote-clientkey.pem` under `$KDIVE_SECRETS_ROOT`.
- A `fastmcp` client logs `ForwardRef('Root') is not fully defined` / `maximum recursion depth`
  while parsing structured content; benign (the harness reads `structured_content` directly), but
  noisy — filter it from sweep output.

## Coverage grid (non-empty cells)

Legend: ✅ pass · ❌ fail · ⏭ blocked · ★ destructive-member.

| Tool | Plane | Maturity | Annotation | local-libvirt | remote-libvirt | fault-inject |
|---|---|---|---|---|---|---|
| `accounting.report_all_projects` | accounting | implemented | read_only | ✅ | ✅ | ✅ |
| `accounting.report_granted_set` | accounting | implemented | read_only | ✅ | ✅ | ✅ |
| `accounting.usage_project` | accounting | implemented | read_only | ✅ | ✅ | ✅ |
| `allocations.get` | allocations | implemented | read_only | — | ✅ | ⏭(#385) |
| `allocations.list` | allocations | implemented | read_only | ✅ | ✅ | ✅ |
| `allocations.release` | allocations | implemented | mutating | — | ✅ | ⏭(#385) |
| `allocations.request` | allocations | implemented | mutating | — | ✅ | ❌(#385) |
| `artifacts.list` | artifacts | partial | read_only | — | — | ⏭(#385) |
| `audit.query` | audit | implemented | read_only | ✅ | ✅ | ✅ |
| `build_hosts.list` | build_hosts | implemented | read_only | ✅ | ✅ | ✅ |
| `control.force_crash`★ | control | partial | destructive | — | ⏭(#386) | ⏭(#385) |
| `debug.end_session` | debug | partial | mutating | — | — | ⏭(#385) |
| `debug.list_breakpoints` | debug | partial | read_only | — | — | ⏭(#385) |
| `debug.read_registers` | debug | partial | read_only | — | — | ⏭(#385) |
| `debug.start_session` | debug | partial | mutating | — | ⏭(#386) | ⏭(#385) |
| `fixtures.list` | fixtures | implemented | read_only | ✅ | ✅ | ✅ |
| `images.list` | images | implemented | read_only | ✅ | ✅ | ✅ |
| `introspect.run` | introspect | partial | read_only | — | — | ⏭(#385) |
| `inventory.list` | inventory | implemented | read_only | ✅ | ✅ | ✅ |
| `jobs.list` | jobs | implemented | read_only | ✅ | ✅ | ✅ |
| `ops.force_release`★ | ops | implemented | destructive | — | ✅ | — |
| `ops.jobs_list` | ops | implemented | read_only | ✅ | ✅ | ✅ |
| `postmortem.triage` | postmortem | partial | read_only | — | — | ⏭(#385) |
| `resources.availability` | resources | implemented | read_only | ✅ | ✅ | ✅ |
| `resources.list` | resources | implemented | read_only | ✅ | ✅ | ✅ |
| `runs.boot` | runs | partial | mutating | — | ⏭(#386) | — |
| `runs.build` | runs | partial | mutating | — | ✅ | — |
| `runs.complete_build` | runs | implemented | mutating | — | ✅ | — |
| `runs.create` | runs | implemented | mutating | — | ✅ | — |
| `runs.install` | runs | partial | mutating | — | ❌(#386) | — |
| `secrets.list` | secrets | implemented | read_only | ✅ | ✅ | ✅ |
| `shapes.list` | shapes | implemented | read_only | ✅ | ✅ | ✅ |
| `systems.list` | systems | implemented | read_only | ✅ | ✅ | ✅ |
| `systems.provision` | systems | partial | mutating | — | ✅ | ⏭(#385) |
| `vmcore.fetch` | vmcore | partial | mutating | — | ⏭(#386) | ⏭(#385) |
| `vmcore.list` | vmcore | partial | read_only | — | — | ⏭(#385) |
