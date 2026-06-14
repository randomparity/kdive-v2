# KDIVE MCP Tool Coverage Campaign — Design

- Date: 2026-06-13
- Branch: `test/mcp-coverage-campaign`
- Status: Approved (brainstorming → spec)
- Report artifact: `docs/reports/mcp-coverage-campaign-2026-06-13.md`

## 1. Goal and failure bar

Prove that **every registered MCP tool** can be fully driven by an agent over the MCP
transport, across every supported provider, build mechanism, and capture method, over the
local-libvirt, remote-libvirt, and k8s environments — concretely, **two control-plane
deployments** (workstation D1, k8s D2) plus the **ub24-big target** they share (§3). Produce a
**grid-first coverage report**: a per-tool census table is the primary artifact; the
capability-arc narrative is secondary.

**Failure bar.** If a capability cannot be reached through an MCP tool, it is a filed GitHub
issue — even when the step is completed out-of-band to keep coverage moving. Deploying KDIVE
itself (helm install, host process bring-up, libvirt/TLS setup on the target) is
infrastructure and is **exempt** from the MCP-tool constraint; only KDIVE *operations*
against a running stack must go through tools. **Identity bootstrap is likewise exempt** —
obtaining a bearer token for a given role (signing a mock-OIDC JWT, or `kdivectl login`) is
authentication setup, not an MCP operation, so a missing token-mint path is never filed as a
tool gap.

This is a falsification exercise against the codebase's self-declared truth: the
`meta.maturity` tag (`implemented` / `partial`) and the reviewed `DESTRUCTIVE_TOOLS` set in
`src/kdive/mcp/tools/_docmeta.py`. An `implemented` tool that cannot be driven is a severe
issue; a `partial` tool that actually works end-to-end is a metadata bug. Per the approved
scoring, **any** shortfall on **any** applicable provider is filed.

## 2. Primary deliverable — the coverage census grid

One row per registered MCP tool. The **auto-generated** columns — `{tool, plane,
declared-maturity, annotation, destructive-membership}` — are produced by introspecting the
registered FastMCP app (`build_app` → registered tools + their `meta.maturity` +
`ToolAnnotations`) joined with `DESTRUCTIVE_TOOLS`, so that subset cannot drift from the real
surface or miss a tool that manual enumeration would. The **Required-role** column is *not*
introspectable — RBAC is enforced imperatively in the handlers, with no role meta tag — so it
is a manually-curated column sourced by reading each handler, and it carries a known drift
risk against the code (flag any mismatch found during execution as a `BUG`).

Columns:

| Column | Source |
|---|---|
| Tool | registered tool name |
| Plane | registrar grouping (catalog/lifecycle/debug/ops/accounting/…) |
| Declared maturity | `meta.maturity` |
| Annotation | `read_only` / `mutating` / `destructive` |
| Required role | RBAC role(s) the handler enforces |
| local-libvirt | cell verdict |
| remote-libvirt | cell verdict |
| fault-inject | cell verdict |
| Reached from | workstation / k8s |
| RBAC-allow | correct role admitted |
| RBAC-deny | under-privileged caller denied + audited |
| Verdict | PASS / GAP / FAIL / BLOCKED / N/A |
| Issue# | filed issue for any non-pass |

Cell legend: `✅ pass · ⚠️ confirmed-gap(#) · ❌ fail(#) · ⏭ blocked · — N/A`. Every
non-`✅`/non-`—` cell maps to exactly one issue.

## 2.5 Drive mechanism and PASS definition

**Driver.** There is no single CLI that can drive every tool. `kdivectl tool call` is a
**read-only passthrough** by construction (`src/kdive/cli/commands/mutations.py`: the
`destructive()`-annotated tools are unreachable through it; the parser registers `call` as
"call a read-only tool by name"), and kdivectl's only mutating surface is four curated
break-glass verbs plus `images`. Therefore:

- **Read-only tools** → driven via `kdivectl tool call <name> --json '<args>'`.
- **Mutating / destructive tools** → driven via a raw MCP-over-HTTP client. The campaign
  reuses/extracts `LiveStackClient` (`tests/integration/live_stack/harness.py`), the only
  existing raw-MCP-over-HTTP driver that can issue arbitrary authenticated tool calls with a
  chosen role token. Issue reproductions are recorded as **`tool-name` + JSON args + role**,
  not as `kdivectl call` (which cannot express them).

**The shipped-CLI gap is itself a finding.** That the only shipped MCP client cannot drive
mutating tools is recorded as a `GAP-TOOL` against `kdivectl` (one issue, not per-tool),
because it fails the campaign's own bar: an agent restricted to the shipped client cannot
reach those capabilities.

**PASS definition (what makes a cell `✅`).** Per annotation class:

| Class | PASS requires |
|---|---|
| read-only | `status: ok` envelope with the expected shape |
| mutating | `status: ok`/`running`; the resulting state transition observed via the matching read tool (e.g. `runs.get`, `systems.get`) or terminal success via `jobs.wait`; expected artifact `ref`s present |
| destructive | the 3-factor gate enforced *and* the effect observed via a read tool |
| RBAC-deny | the correct `error_category` denial envelope; **plus** the denial class's expected audit outcome (below) |

**RBAC-deny is class-aware** — the audit design records denials selectively
(`mcp/middleware.py` `DenialAuditMiddleware` + ADR-0043 §4 / ADR-0062 §5), so the PASS rule
must match it or it mis-flags correct behaviour:

| Denial class | Expected audit outcome |
|---|---|
| member with insufficient *project* role | `transition='denied'` row visible via `audit.query` |
| platform-role denial (`require_platform_role`) | denial row recorded (audited elsewhere) and visible via `audit.query` |
| destructive-gate `{op}:denied` | `transition='denied'` row carrying the gated object, via `audit.query` |
| **non-member** (token not a member of the target project) | envelope only — **no** audit row is written by design; absence is the expected pass, *not* a gap |

Denials land in `audit_log` with the reserved `transition='denied'` literal, so `audit.query`
with a `'denied'` transition filter is the reader. Arc 7 constructs an **in-project member
with an insufficient role** when it intends to assert the audit row; it uses a non-member
token only to verify the envelope-only path.

Anything short of the row's class definition is `⚠️`/`❌` and filed. Where a confirming
signal genuinely cannot be read through any MCP tool, that verification gap is itself filed as
`GAP-OOB`.

## 3. Deployment topology (Phase 0 brings these up)

- **D1 — workstation** (`just stack-up`): server + worker + reconciler as host processes
  with compose backends (Postgres / MinIO / mock-OIDC). Drives **local-libvirt**,
  **fault-inject**, **remote-libvirt → ub24-big**, and the **ssh** and **ephemeral-libvirt**
  build hosts.
- **D2 — k8s `kdive-dev`** (helm release): control plane in-cluster. No local KVM, so it
  drives **remote-libvirt → ub24-big** and **fault-inject** only. Also validates the M2.1
  container/helm deployment surface end-to-end.
- **ub24-big** (`dave@ub24-big.prod.pdx.drc.nz`) — a *target*, not a control plane: the
  remote-libvirt host (libvirt + qemu + TLS) and an ssh build-host target. Reached from both
  D1 and D2.

**Deployment × provider is not a full cross-product.** local-libvirt cells under k8s are
`—` (N/A); k8s exercises only remote-libvirt and fault-inject.

## 4. Matrix dimensions

| Dimension | Values |
|---|---|
| Provider / runtime | local-libvirt, remote-libvirt, fault-inject |
| Build mechanism | worker-local, ssh build-host, ephemeral-libvirt VM, local-libvirt-over-transport, remote-worker |
| Capture method — **core** (vmcore-producing) | KDUMP (remote-libvirt only), HOST_DUMP |
| Capture method — **live** (not vmcore-producing) | GDBSTUB (debug attach), CONSOLE (console artifact) |
| Debug transport | gdb-MI (gdbstub), drgn-live, drgn-from-vmcore |
| Deployment | workstation (D1), k8s kdive-dev (D2) |
| RBAC | project roles viewer/operator/admin × platform roles platform_admin/platform_operator/platform_auditor + 3-factor destructive gate (capability scope + role + profile opt-in) |

## 5. Execution arcs (how the grid is filled)

**Arcs 2–5 are one ordered System session, not independent passes.** Arcs 3/4/5 all act on
the *same* System that Arc 2 boots, and the order is forced by the lifecycle state machine:
`debug.*` (live) and `control.power` need a `ready` System; `control.force_crash` is a
one-way `ready → crashed` once-per-System transition; and `vmcore.fetch` admits a core capture
only on a **`crashed`** System (core-producing methods are `{HOST_DUMP, KDUMP}`;
`GDBSTUB`/`CONSOLE` are live, not core). So per provider the session runs:

> Arc 2 build→boot (→ `ready`) → Arc 3-live (gdb-MI, drgn-live) → Arc 4-live (GDBSTUB/CONSOLE)
> → Arc 5 `control.power` then `control.force_crash` (→ `crashed`) → Arc 4-core (HOST_DUMP/KDUMP)
> `vmcore.fetch` → Arc 3-offline (drgn-from-vmcore) + `introspect.from_vmcore` + postmortem
> → **session-end teardown** (§5.1).

The offline debug transport (`drgn-from-vmcore`) and `introspect.from_vmcore`/`postmortem.*`
all consume the captured core, so they run **after** Arc 4-core, never in the live slot.

Teardown happens once at session end, never per-arc (a crashed System can't be re-readied).

- **Arc 0 — Preflight / deploy.** Stand up D1 and D2. **The server process must be launched
  with both opt-in gates flipped on**, because `providers/composition.py` gates fault-inject
  behind `_fault_inject_enabled` (env, default *off*) and remote-libvirt behind
  `is_remote_libvirt_configured()`; without that config those tools resolve to
  `not_implemented` and would be mis-filed as tool gaps. Concretely: set the
  fault-inject enable env/flag and supply remote-libvirt operator config in **both** D1 and
  D2 at start. Then register remote-libvirt and the build hosts via MCP tools where they
  exist (`build_hosts.register`, systems/profile config). **Preflight assertions before any
  other arc runs:** each expected provider appears in `resources.list` / `ops.diagnostics`;
  and a **bidirectional reachability check** — control-plane → ub24-big libvirt TLS port, and
  ub24-big-guest → the object store the control plane will hand out in presigned URLs (the
  in-cluster MinIO for D2). **Identity gate (hard prerequisite):** assert each deployment's
  JWT verifier trusts the campaign's token issuer by minting a token for each of the six roles
  and making one authenticated read. If a deployment cannot be configured to trust the
  campaign issuer (e.g. D2 wired to a real OIDC the campaign can't sign for), that
  deployment's entire mutating + RBAC surface is declared `⏭ blocked` up front — not filed
  per-tool. Out-of-band infra (helm, TLS, libvirt, server env, token minting) is allowed here
  and is not a tool failure.
- **Arc 1 — Reads sweep.** Every read-only tool on each deployment first (cheap, establishes
  baseline reachability and auth).
- **Arc 2 — Build → boot.** `(provider × build-mechanism)` is **not** a full cross-product
  (same caveat as deployment×provider in §3). The valid combos are: local-libvirt ×
  {worker-local, ssh, ephemeral-libvirt, local-over-transport}; remote-libvirt ×
  {remote-worker, ssh}. Invalid combos are `—`, not failures. Each valid cell drives
  `allocations.request → systems.define/provision → runs.create/build/complete_build/install/boot`,
  leaving the System `ready` for the rest of the session (Arcs 3–5), torn down at session end (§5.1).
- **Arc 3 — Debug.** Per `(provider × transport)`. **Arc 3-live** (gdb-MI, drgn-live, on the
  `ready` System) drives `debug.start_session` … `debug.{set,clear,list}_breakpoint`,
  `read_memory`, `read_registers`, `continue`, `interrupt`, `end_session`. **Arc 3-offline**
  (drgn-from-vmcore) needs the captured core and therefore runs in the post-Arc-4-core phase,
  alongside `introspect.from_vmcore`, not in the live slot.
- **Arc 4 — Capture → introspect.** Per `(provider × method)`; the advertised method set is
  verified via `resources.describe` first (authoritative — the "KDUMP remote-only" note in §4
  is the current expectation, confirmed here). The four `CaptureMethod`s are two families with
  different tools and PASS signals (`vmcore.fetch` admits only the core methods; it excludes
  console/gdbstub):
  - **Arc 4-core** (HOST_DUMP, KDUMP) — vmcore-producing. Runs *after* Arc 5's `force_crash`
    produced the `crashed` System: `vmcore.fetch` → `vmcore.list` → `introspect.from_vmcore` +
    `postmortem.crash` / `postmortem.triage`. PASS = core `ref` produced + introspect returns.
  - **Arc 4-live** — *not* vmcore-producing, runs against the `ready` System. **CONSOLE** is
    exercised via console-artifact retrieval (`artifacts.list` / `artifacts.get`); PASS =
    console artifact present + a redacted snippet. **GDBSTUB** is the same single-client
    gdbstub channel as Arc 3-live, so it is *not* a separate flow: it is scored as the
    capability advertised (via `resources.describe`) + a successful `debug.start_session`
    attach (cross-referenced to Arc 3, and requiring Arc 3's `debug.end_session` to have
    released the single client first).
- **Arc 5 — Control (destructive, gated).** `control.power` (System stays `ready`), then
  `control.force_crash` with the destructive gate satisfied — this `ready → crashed`
  transition is the **input** to Arc 4-core, so it runs before core capture, after live work.
- **Arc 6 — Platform operations.** `resources.{set_status,cordon,uncordon,drain}`,
  `ops.{force_release,force_teardown,reconcile_now,queue_pause,queue_resume,jobs_list,set_cost_class_coeff,set_host_capacity}`,
  `ops.diagnostics(.egress)`, `accounting.*`, `shapes.{set,delete}`, `build_hosts.*`,
  `images.*`, `secrets.list`, `audit.query`, `inventory.list`, `fixtures.list`,
  `investigations.*`, `artifacts.create_*_upload`.
- **Arc 7 — RBAC cross-cut.** For each mutating/destructive tool, issue an under-privileged
  call (reusing `spine.py` `mint_role_token`) immediately after the positive call. Assert the
  denial envelope **always**, and the audit row **per the §2.5 denial-class table**: a
  `transition='denied'` row (via `audit.query`) for member-over-reach / platform-role /
  destructive-gate denials, and envelope-only (no row) for the non-member probe. Verify the
  3-factor destructive gate denies when any one factor is missing.
- **Arc 8 — fault-inject.** fault-inject does **not** run the Arc 2–5 build→boot session — its
  build/boot/lifecycle grid cells are `—`. It is exercised only here: drive control/capture
  **error paths** through the fault-inject provider to confirm error envelopes and
  `error_category` values.

**Arc 6 isolation.** Arc 6's queue/drain/teardown/reconcile verbs (`ops.queue_pause` stalls
*every* job-based tool — build/install/boot/capture; `ops.reconcile_now` tears down orphans;
`resources.drain`/`ops.force_teardown` remove live infra) must run **last**, or against a
dedicated throwaway allocation/resource, never against the live state of Arcs 2–5/8.
`ops.queue_pause` in particular is immediately followed by `ops.queue_resume` and is never
left paused while another arc has work queued.

### 5.1 Cleanup, isolation, and capacity contract

The campaign creates allocations, Systems, VMs, ephemeral build domains, images, uploads, and
build-host leases across many provider×mechanism combos on a *single shared* ub24-big host
(which triples as remote-libvirt target + ssh build-host + ephemeral-libvirt host).

- **Session-end teardown.** Each provider's System session (Arcs 2–5, per the ordering above)
  ends — once, after Arc 4-core — by releasing its allocation and tearing down its System
  (`allocations.release` → `systems.teardown`); ephemeral build domains are reaped by the
  reconciler. The session's residue is verified gone (via `systems.list` /
  `resources.describe`) before the next session that shares its resource starts. Teardown is
  never per-arc: Arcs 3/4/5 share Arc 2's System, and a `crashed` System cannot be re-readied.
- **Final sweep.** After all arcs, one `ops.reconcile_now` + `resources.list` confirms no
  leaked Systems/leases/domains remain; any leak is itself a filed `BUG`.
- **Capacity ceiling.** A per-host concurrency cap for ub24-big (no more than one
  remote-libvirt boot VM + one ephemeral build VM concurrently) prevents capacity-exhaustion
  failures from being mis-filed as tool gaps. Arcs sharing ub24-big run **serially**.
- **Two control planes, one host.** D1 and D2 both drive remote-libvirt on ub24-big from
  *separate* control-plane DBs, so they can collide on shared host resources (networks,
  pool/overlay names, and the gdbstub port range encoded in domain XML) with no shared
  registry. Remote-libvirt sessions run **D1 fully, then D2** — never concurrently on
  ub24-big. A collision despite serialization is a filed `BUG`, not a tool gap.
- **Partial-failure rule.** If an arc aborts mid-way, its cleanup still runs (release +
  teardown + sweep) before continuing, so leaked state never poisons a later arc.

## 6. Failure taxonomy → issues

| Category | Meaning |
|---|---|
| `GAP-TOOL` | No MCP path to the capability at all (most severe) |
| `GAP-PARTIAL` | Tool works on ≥1 provider but falls short on another applicable provider |
| `GAP-OOB` | Capability requires an out-of-band action; no MCP tool exposes it |
| `GAP-RBAC` | Wrong authz behaviour (over/under-permissive) or missing audit row |
| `BUG` | Tool errors, wrong response envelope, or wrong `error_category` |

Each issue carries: labels (`area:*`, `type:gap` or `type:bug`), env + provider, the **exact
reproduction as tool-name + JSON args + role token** (the form the driver in §2.5 actually
uses; `kdivectl tool call …` only for read-only tools), expected vs actual, and a back-link
to the grid cell. Issues are filed as discovered, but the **consolidated list is shown to the
user for confirmation before any bulk filing**.

## 7. Report artifact

`docs/reports/mcp-coverage-campaign-2026-06-13.md`, committed on
`test/mcp-coverage-campaign`. Order: (1) census grid, (2) per-arc narrative, (3) issue
index, (4) topology + reproduction-command appendix.

## 8. Budget, priority, and completion criterion

The matrix is large and runs largely serially (§5.1), each System session is multi-minute VM
work, so the run is paced and prioritized rather than exhaustive-at-any-cost:

- **Size.** ~90 tools × the applicable provider/mechanism/method cells; the read sweep (Arc 1)
  is minutes, each build→boot→debug→crash→capture session is tens of minutes. Estimate the
  exact cell count in Arc 0 once the grid is auto-generated, and record it in the report.
- **Priority order** (so a truncated run still has known, useful coverage): (1) Arc 1 reads on
  all deployments; (2) `implemented` lifecycle tools, local-libvirt first (cheapest); (3)
  `partial` lifecycle/debug/capture tools; (4) remote-libvirt sessions; (5) platform-ops and
  RBAC cross-cut; (6) fault-inject error paths.
- **Completion criterion.** The campaign is done when **every non-`—` grid cell is `✅`, `⏭
  blocked`, or carries a filed issue#** — no cell left unevaluated. A partial run reports its
  covered prefix of the priority order and marks the remainder `⏭` with the reason.

## 9. Out of scope

- cloud / bare-metal / PowerVM providers (unimplemented → `N/A` rows).
- Deploying KDIVE itself (infrastructure, MCP-exempt).
- Performance / load / soak testing.

## 10. Open risks

- **ub24-big reachability and TLS PKI** must be current; stale certs block all remote arcs.
- **D2 bidirectional reachability for two-phase capture.** When k8s is the control plane, the
  ub24-big guest uploads vmcore to a presigned URL pointing at the *cluster's* object store, so
  the guest must reach that endpoint and the cluster must reach ub24-big's libvirt TLS port.
  If the cluster MinIO is not externally reachable from the ub24-big guest, Arc 4 on D2 is
  structurally blocked (mark `⏭`, not a tool gap) — preflighted in Arc 0.
- **k8s `kdive-dev` image** must be the SHA-tagged build matching this tree, or the helm
  deployment validates a stale surface.
- **KVM / nested-virt** on the workstation for local-libvirt and ephemeral-libvirt build VMs.
- **Token minting** for six RBAC roles via mock-OIDC must work in both D1 and D2 (k8s OIDC
  config may differ from compose mock-OIDC).
