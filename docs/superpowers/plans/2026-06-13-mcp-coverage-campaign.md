# MCP Tool Coverage Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive every registered MCP tool over the MCP transport across local-libvirt, remote-libvirt, and fault-inject providers on the workstation and k8s control-plane deployments, produce a grid-first coverage report, and file a GitHub issue for every capability that cannot be reached through an MCP tool.

**Architecture:** A small committed census generator introspects the live FastMCP app to emit the auto-generated grid columns; a results recorder merges per-cell verdicts into the final grid markdown. Execution arcs reuse existing machinery — `kdivectl tool call` for read-only tools, the `tests/integration/live_stack` harness (`LiveStackClient`, `mint_token`, `spine.py`) for mutating/destructive tools — and write per-cell result records the recorder consumes. Spec: `docs/superpowers/specs/2026-06-13-mcp-coverage-campaign-design.md`.

**Tech Stack:** Python 3.13, `uv`, `pytest` (`live_stack` marker), FastMCP, `kdivectl`, libvirt/qemu, microk8s/helm, `gh` CLI.

**Reading order for the implementer:** the spec (above) is authoritative for *what each cell means* (§2.5 PASS table, §5 arc ordering, §6 issue taxonomy). This plan is *how to build and run it*. Phase 1 is real TDD code. Phases 2–11 are operational: each task gives the exact tool call, the PASS assertion, and the on-failure issue action — they are runbook steps, not code to author.

---

## File structure

| Path | Responsibility | Phase |
|---|---|---|
| `scripts/coverage_campaign/__init__.py` | package marker | 1 |
| `scripts/coverage_campaign/gridgen.py` | introspect `build_app().list_tools()` → census rows (tool, plane, maturity, annotation, destructive-membership) | 1 |
| `scripts/coverage_campaign/results.py` | `CellResult` dataclass + `merge_and_render(rows, results) -> markdown` | 1 |
| `tests/scripts/test_gridgen.py` | unit tests for the generator | 1 |
| `tests/scripts/test_results.py` | unit tests for the recorder/renderer | 1 |
| `tests/scripts/__init__.py` | package marker | 1 |
| `docs/reports/mcp-coverage-campaign-2026-06-13.md` | the deliverable report (grid + narrative + issue index + appendix) | 11 |
| `artifacts/coverage-campaign/results.jsonl` (gitignored, run-local) | per-cell verdict records written during arcs | 2–10 |

The campaign keeps committed code minimal (one generator + one recorder); everything else reuses existing tools and the `live_stack` harness, written as run-local result records, not new production modules.

---

## Phase 1 — Census tooling (TDD)

### Task 1: Package skeleton

**Files:**
- Create: `scripts/coverage_campaign/__init__.py`
- Create: `tests/scripts/__init__.py`

- [ ] **Step 1: Create the package markers**

```python
# scripts/coverage_campaign/__init__.py
"""Coverage-campaign tooling: census generation and result rendering."""
```

```python
# tests/scripts/__init__.py
```

- [ ] **Step 2: Commit**

```bash
git add scripts/coverage_campaign/__init__.py tests/scripts/__init__.py
git commit -m "test(coverage-campaign): add tooling package skeleton"
```

### Task 2: Census generator — enumerate tools

**Files:**
- Create: `scripts/coverage_campaign/gridgen.py`
- Test: `tests/scripts/test_gridgen.py`

The generator builds the app exactly as the ADR-0047 doc guard does (null pool + local-keypair verifier — no DB, no OIDC), pulls `list[FunctionTool]` via `asyncio.run(app.list_tools())`, and reads `.name`, `.meta["maturity"]`, `.annotations` (`readOnlyHint`/`destructiveHint`), joined against `_docmeta.DESTRUCTIVE_TOOLS`. Plane is derived from the tool-name prefix.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_gridgen.py
from __future__ import annotations

from scripts.coverage_campaign.gridgen import CensusRow, generate_rows


def test_generate_rows_covers_known_tools_with_correct_metadata() -> None:
    rows = {r.tool: r for r in generate_rows()}

    # A read-only catalog tool.
    assert rows["resources.list"].annotation == "read_only"
    assert rows["resources.list"].plane == "resources"

    # A partial mutating lifecycle tool.
    assert rows["runs.build"].maturity == "partial"
    assert rows["runs.build"].annotation == "mutating"

    # A reviewed-destructive tool carries the membership flag and destructive annotation.
    assert rows["control.force_crash"].annotation == "destructive"
    assert rows["control.force_crash"].destructive_member is True

    # Every row has a non-empty plane and a valid maturity.
    assert all(r.plane for r in rows.values())
    assert all(r.maturity in {"implemented", "partial", "planned"} for r in rows.values())


def test_generate_rows_is_nonempty_and_unique() -> None:
    rows = generate_rows()
    names = [r.tool for r in rows]
    assert len(names) > 50
    assert len(names) == len(set(names))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_gridgen.py -q`
Expected: FAIL with `ModuleNotFoundError: scripts.coverage_campaign.gridgen`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/coverage_campaign/gridgen.py
"""Generate the coverage-census rows by introspecting the live FastMCP app.

Mirrors the ADR-0047 doc guard's app-build path (null pool + local-keypair verifier;
no DB, no OIDC) so the static grid columns cannot drift from the real tool surface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.tools import _docmeta
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


@dataclass(frozen=True)
class CensusRow:
    tool: str
    plane: str
    maturity: str
    annotation: str  # "read_only" | "mutating" | "destructive"
    destructive_member: bool


def _annotation(tool: FunctionTool) -> str:
    ann = tool.annotations
    if ann and ann.destructiveHint:
        return "destructive"
    if ann and ann.readOnlyHint:
        return "read_only"
    return "mutating"


def _build_tools() -> list[FunctionTool]:
    _, public_key = make_keypair()
    verifier = JWTVerifier(public_key=public_key, issuer=ISSUER, audience=AUDIENCE)
    pool = cast(AsyncConnectionPool, None)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    return cast(list[FunctionTool], asyncio.run(app.list_tools()))


def generate_rows() -> list[CensusRow]:
    rows: list[CensusRow] = []
    for tool in _build_tools():
        meta = tool.meta or {}
        rows.append(
            CensusRow(
                tool=tool.name,
                plane=tool.name.split(".", 1)[0],
                maturity=str(meta.get("maturity", "")),
                annotation=_annotation(tool),
                destructive_member=tool.name in _docmeta.DESTRUCTIVE_TOOLS,
            )
        )
    rows.sort(key=lambda r: r.tool)
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/scripts/test_gridgen.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Type-check and lint the new module**

Run: `just type && just lint`
Expected: no errors on the new files. (If `make_keypair`/`ISSUER`/`AUDIENCE` import paths differ, fix to match `tests/mcp/conftest.py`.)

- [ ] **Step 6: Commit**

```bash
git add scripts/coverage_campaign/gridgen.py tests/scripts/test_gridgen.py
git commit -m "feat(coverage-campaign): census generator over the live tool surface"
```

### Task 3: Result record + grid renderer

**Files:**
- Create: `scripts/coverage_campaign/results.py`
- Test: `tests/scripts/test_results.py`

A `CellResult` is one (tool, provider, deployment) verdict. `merge_and_render` joins the census rows with the collected results and emits the grid markdown using the spec's legend (`✅ ⚠️ ❌ ⏭ —`).

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_results.py
from __future__ import annotations

from scripts.coverage_campaign.gridgen import CensusRow
from scripts.coverage_campaign.results import CellResult, merge_and_render


def _row(tool: str) -> CensusRow:
    return CensusRow(tool=tool, plane=tool.split(".")[0], maturity="implemented",
                     annotation="read_only", destructive_member=False)


def test_render_marks_pass_gap_and_na() -> None:
    rows = [_row("resources.list")]
    results = [
        CellResult(tool="resources.list", provider="local-libvirt", verdict="pass", issue=None),
        CellResult(tool="resources.list", provider="remote-libvirt", verdict="gap", issue=42),
        # fault-inject deliberately omitted -> renders as N/A
    ]
    md = merge_and_render(rows, results)
    assert "resources.list" in md
    assert "✅" in md
    assert "⚠️(#42)" in md
    assert "—" in md  # fault-inject cell with no result


def test_render_is_deterministic() -> None:
    rows = [_row("a.x"), _row("b.y")]
    assert merge_and_render(rows, []) == merge_and_render(rows, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_results.py -q`
Expected: FAIL with `ModuleNotFoundError: scripts.coverage_campaign.results`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/coverage_campaign/results.py
"""Per-cell verdict record and the grid-markdown renderer (spec §2)."""

from __future__ import annotations

from dataclasses import dataclass

from scripts.coverage_campaign.gridgen import CensusRow

_PROVIDERS = ("local-libvirt", "remote-libvirt", "fault-inject")
_GLYPH = {"pass": "✅", "gap": "⚠️", "fail": "❌", "blocked": "⏭"}


@dataclass(frozen=True)
class CellResult:
    tool: str
    provider: str
    verdict: str  # "pass" | "gap" | "fail" | "blocked"
    issue: int | None


def _cell(result: CellResult | None) -> str:
    if result is None:
        return "—"
    glyph = _GLYPH[result.verdict]
    return f"{glyph}(#{result.issue})" if result.issue is not None else glyph


def merge_and_render(rows: list[CensusRow], results: list[CellResult]) -> str:
    by_key = {(r.tool, r.provider): r for r in results}
    header = "| Tool | Plane | Maturity | Annotation | " + " | ".join(_PROVIDERS) + " |"
    sep = "|" + "---|" * (4 + len(_PROVIDERS))
    lines = [header, sep]
    for row in rows:
        cells = [_cell(by_key.get((row.tool, p))) for p in _PROVIDERS]
        marker = "★" if row.destructive_member else ""
        lines.append(
            f"| `{row.tool}`{marker} | {row.plane} | {row.maturity} | "
            f"{row.annotation} | " + " | ".join(cells) + " |"
        )
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/scripts/test_results.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Type-check, lint**

Run: `just type && just lint`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add scripts/coverage_campaign/results.py tests/scripts/test_results.py
git commit -m "feat(coverage-campaign): cell-result record and grid renderer"
```

### Task 4: Emit the census skeleton + gitignore run artifacts

**Files:**
- Modify: `.gitignore` (append the run-local artifact dir)
- Create: `docs/reports/.gitkeep` only if `docs/reports/` does not exist

- [ ] **Step 1: Generate the skeleton grid and the cell count**

Run:
```bash
uv run python -c "from scripts.coverage_campaign.gridgen import generate_rows; rows=generate_rows(); print(len(rows), 'tools'); [print(r.tool, r.maturity, r.annotation) for r in rows]"
```
Expected: prints the tool count (≈90) and one line per tool. Record the exact count — it is the §8 "Size" figure for the report.

- [ ] **Step 2: Ignore run-local results**

Append to `.gitignore`:
```
/artifacts/coverage-campaign/
```

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore(coverage-campaign): ignore run-local result artifacts"
```

---

## Phase 2 — Arc 0: deploy + preflight (operational)

Each task here is a runbook step. The "assertion" is the stated observable. Infra setup (helm, TLS, libvirt, server env, token minting) is MCP-exempt (spec §1).

### Task 5: Bring up D1 (workstation control plane)

**Files:** none (operational).

- [ ] **Step 1: Start backends + host processes**

Run: `just stack-up` then `just stack-start-daemon`
Expected: Postgres, MinIO, mock-OIDC healthy; server/worker/reconciler running. Note the printed env (base URL, OIDC issuer) — export `KDIVE_BASE_URL`, `KDIVE_OIDC_ISSUER`.

- [ ] **Step 2: Launch the server with both provider opt-ins (spec §5 Arc 0)**

The server process MUST start with fault-inject enabled and remote-libvirt configured, else those tools resolve `not_implemented`. Set the fault-inject enable env and the remote-libvirt operator config (point it at `dave@ub24-big.prod.pdx.drc.nz`) before `stack-start`. Confirm the exact env names from `src/kdive/providers/composition.py` (`_fault_inject_enabled`) and `src/kdive/providers/remote_libvirt/config.py` (`is_remote_libvirt_configured`).
Expected: server restarts cleanly with the opt-ins set.

- [ ] **Step 3: Identity gate — mint a token per role and do one read**

Using the mock-OIDC issuer, mint a token for each of: viewer, operator, admin, platform_admin, platform_operator, platform_auditor (`mint_token` in `tests/integration/live_stack/harness.py`). For each, `kdivectl tool call resources.list` (or the in-memory client) and confirm an authenticated `status: ok`.
Expected: all six tokens authenticate. If any role can't be minted/trusted, record D1 as identity-blocked (it won't be — D1 uses mock-OIDC).

- [ ] **Step 4: Provider preflight**

Run: `kdivectl tool call resources.list` and `kdivectl tool call ops.diagnostics`
Expected: local-libvirt, remote-libvirt, and fault-inject all present. Any missing provider → fix the Step-2 opt-in before proceeding (not a tool gap).

### Task 6: Bring up D2 (k8s kdive-dev) + ub24-big target

**Files:** none (operational).

- [ ] **Step 1: Verify/refresh the ub24-big target**

Confirm libvirt+qemu+TLS reachable and certs current (spec §10). From the control-plane host: `virsh -c qemu+tls://ub24-big.prod.pdx.drc.nz/system list` succeeds.
Expected: connection ok. Stale certs → refresh (out-of-band infra).

- [ ] **Step 2: Deploy the chart to kdive-dev with the SHA-tagged image matching this tree**

Build/tag the image for this commit, helm-install/upgrade `kdive-dev` with fault-inject enabled and remote-libvirt config pointing at ub24-big. (Per `k8s-deploy-secrets` memory: non-root UID needs `fsGroup`+`0440` secret mounts.)
Expected: pods Ready; migrate hook completed.

- [ ] **Step 3: D2 identity gate**

Determine D2's verifier issuer. If it trusts the mock-OIDC keypair (mint six role tokens, one authenticated read each) → proceed. If D2 is wired to a real OIDC the campaign cannot sign for → mark **D2's entire mutating + RBAC surface `⏭ blocked`** in the results, with reason, and restrict D2 to read-only arcs (spec §5 Arc 0).
Expected: documented identity status for D2.

- [ ] **Step 4: D2 bidirectional reachability preflight (spec §10)**

Confirm: cluster → ub24-big libvirt TLS port; and ub24-big guest → the cluster object store endpoint that presigned URLs will use. If the guest cannot reach the cluster MinIO, mark D2 Arc-4-core cells `⏭` (not a tool gap).
Expected: documented reachability status; capture feasibility on D2 known before arcs run.

- [ ] **Step 5: Record the run manifest**

Write `artifacts/coverage-campaign/manifest.json`: deployment base URLs, issuer trust status per deployment, provider availability, ub24-big reachability, and the tool count from Task 4. This is the source for the report's topology appendix and the `⏭ blocked` declarations.

---

## Phase 3 — Arc 1: read-only sweep

### Task 7: Drive every read-only tool on each reachable deployment

**Files:** appends to `artifacts/coverage-campaign/results.jsonl`.

- [ ] **Step 1: Enumerate read-only tools**

From the census: `annotation == "read_only"`. Run:
```bash
uv run python -c "from scripts.coverage_campaign.gridgen import generate_rows; [print(r.tool) for r in generate_rows() if r.annotation=='read_only']"
```

- [ ] **Step 2: Call each read-only tool via kdivectl on D1, then D2**

For each tool, `kdivectl tool call <tool> --json '<minimal-valid-args>'` with an operator token. PASS (spec §2.5 read-only) = `status: ok` with the expected shape.
Record one `CellResult` per (tool, deployment) into `results.jsonl`. Provider dimension for pure-catalog reads is the deployment's default; mark provider-specific reads under each provider.

- [ ] **Step 3: File issues for read failures**

Any non-`ok` read of an `implemented` tool → `BUG`/`GAP-TOOL` issue (spec §6 taxonomy), recorded with the exact `kdivectl tool call` repro. Add the issue number to the `CellResult`.

- [ ] **Step 4: Checkpoint commit (results are gitignored; commit only the running report draft if started)**

No code commit here; results.jsonl is run-local. Proceed to Arc 2.

---

## Phase 4 — Arc 2: build → boot sessions

> Spec §5: Arcs 2–5 are ONE ordered System session per provider. Do not tear down between arcs. Valid build combos: local-libvirt × {worker-local, ssh, ephemeral-libvirt, local-over-transport}; remote-libvirt × {remote-worker, ssh}. Run D1 fully before D2 on ub24-big (spec §5.1).

### Task 8: local-libvirt build→boot per build mechanism (D1)

**Files:** appends to `results.jsonl`; drive via the `live_stack` harness (`LiveStackClient`, `spine.py` helpers).

- [ ] **Step 1: For each local-libvirt build mechanism, drive the lifecycle**

Reuse `tests/integration/live_stack/spine.py` helpers (`mint_role_token`, `drain_job`, `await_system_state`). Sequence per mechanism:
`allocations.request → systems.define → systems.provision → runs.create → runs.build → runs.complete_build → runs.install → runs.boot`.
Register the ssh/ephemeral build host first via `build_hosts.register` where the mechanism needs it.

- [ ] **Step 2: Assert PASS per spec §2.5 (mutating)**

For each tool: `status: ok`/`running`, the state transition observed via `runs.get`/`systems.get` or terminal `jobs.wait` success, and expected artifact `ref`s present. Leave the System `ready` (do NOT tear down — Arcs 3–5 reuse it).

- [ ] **Step 3: Record cells + file issues**

One `CellResult` per (tool, local-libvirt) with verdict. Any shortfall on this provider → file `GAP-PARTIAL`/`BUG` with the tool+args+role repro (spec §6). A `partial`-maturity tool that works end-to-end → file a metadata-bug (`BUG`) per spec §1.

### Task 9: remote-libvirt build→boot per build mechanism (D1 → ub24-big)

- [ ] **Step 1: Drive `{remote-worker, ssh}` build mechanisms** through the same lifecycle sequence against the remote-libvirt provider, honoring the §5.1 capacity ceiling (≤1 remote boot VM + ≤1 ephemeral build VM concurrently; arcs serial).
- [ ] **Step 2: Assert PASS** (same §2.5 mutating rule). Leave System `ready`.
- [ ] **Step 3: Record cells + file issues.** A collision despite serialization → `BUG`, not a tool gap (spec §5.1).

---

## Phase 5 — Arc 3-live: live debug

### Task 10: gdb-MI and drgn-live on the ready System (per provider)

- [ ] **Step 1: Drive the debug session** on each provider's `ready` System (from Arc 2): `debug.start_session` → `debug.set_breakpoint` → `debug.list_breakpoints` → `debug.read_memory` → `debug.read_registers` → `debug.continue` → `debug.interrupt` → `debug.clear_breakpoint` → `debug.end_session`. Run gdb-MI then drgn-live.
- [ ] **Step 2: Assert PASS** (§2.5 mutating: ok envelope + observable effect, e.g. breakpoint appears in `debug.list_breakpoints`, registers return values). **`debug.end_session` MUST run** so the single-client gdbstub is freed for Arc 4-live GDBSTUB.
- [ ] **Step 3: Record cells + file issues** per (debug tool, provider).

---

## Phase 6 — Arc 4-live: live capture

### Task 11: CONSOLE and GDBSTUB capture (per provider, ready System)

- [ ] **Step 1: CONSOLE** — drive console-artifact retrieval via `artifacts.list` then `artifacts.get` for the System's console artifact. PASS = artifact present + a redacted snippet (spec §4/Arc 4-live).
- [ ] **Step 2: GDBSTUB** — score as capability advertised (`resources.describe` shows GDBSTUB) + a successful `debug.start_session` attach (cross-reference Arc 3; requires the Arc-3 `end_session` already ran). Do NOT double-count as a separate debug flow.
- [ ] **Step 3: Record cells + file issues** per (method, provider).

---

## Phase 7 — Arc 5: control, then Arc 4-core: core capture

> `control.force_crash` is one-way `ready → crashed`, once per System (spec §5). Run `control.power` first (System stays `ready`), then `force_crash`, then core capture.

### Task 12: control.power then control.force_crash (destructive gate)

- [ ] **Step 1: `control.power`** through the destructive gate (capability + role + profile opt-in satisfied). PASS = gate enforced + effect observed (domain restart) via a read tool; System stays `ready`.
- [ ] **Step 2: `control.force_crash`** with the gate satisfied. PASS = gate enforced + `systems.get` shows `crashed`.
- [ ] **Step 3: Record cells + file issues** per (control tool, provider).

### Task 13: Arc 4-core capture + offline introspection

- [ ] **Step 1: Core capture** on the `crashed` System for each core method the provider advertises (`resources.describe`): HOST_DUMP (both providers), KDUMP (remote-libvirt only). Drive `vmcore.fetch` → `vmcore.list`.
- [ ] **Step 2: Offline introspection** (Arc 3-offline + introspect): `introspect.from_vmcore`, `postmortem.crash`, `postmortem.triage`, and drgn-from-vmcore. PASS = core `ref` produced + introspect returns expected symbol data.
- [ ] **Step 3: Record cells + file issues.** KDUMP cell on local-libvirt = `—` (N/A). On D2, if Arc-0 marked guest→cluster-MinIO unreachable, mark core cells `⏭`.
- [ ] **Step 4: Session-end teardown (spec §5.1)** — `allocations.release` → `systems.teardown`; verify gone via `systems.list`. Repeat the whole Arc 2→5 session for the next provider/deployment.

---

## Phase 8 — Arc 6: platform operations

### Task 14: Drive admin/ops/accounting tools (against a dedicated throwaway resource)

> Arc 6 isolation (spec §5): queue/drain/teardown/reconcile run LAST or against a throwaway allocation, never against live arc state. `ops.queue_pause` is immediately followed by `ops.queue_resume`.

- [ ] **Step 1: Read/admin tools** — `accounting.*` (estimate/usage/report/set_budget/set_quota), `shapes.{set,delete}`, `build_hosts.{register,list,disable,remove}`, `images.{build,publish,upload,delete,prune_expired,extend,list}`, `secrets.list`, `audit.query`, `inventory.list`, `fixtures.list`, `investigations.{open,get,close,link,unlink}`, `artifacts.create_run_upload`/`create_system_upload`, `ops.{set_cost_class_coeff,set_host_capacity,jobs_list,diagnostics,diagnostics.egress}`. PASS per §2.5 class.
- [ ] **Step 2: Disruptive verbs on a throwaway allocation** — `resources.{set_status,cordon,uncordon,drain}`, `ops.{force_release,force_teardown,reconcile_now}`, `ops.queue_pause`→`ops.queue_resume`. Provision a dedicated allocation to target; never touch other arcs' state.
- [ ] **Step 3: Record cells + file issues** per tool.

---

## Phase 9 — Arc 7: RBAC cross-cut

### Task 15: Denial probes for every mutating/destructive tool

- [ ] **Step 1: Positive then negative** — for each mutating/destructive tool, after its positive call, issue an under-privileged call (`mint_role_token`). Assert the denial envelope ALWAYS.
- [ ] **Step 2: Class-aware audit assertion (spec §2.5 table)** — assert a `transition='denied'` row via `audit.query` for member-over-reach / platform-role / destructive-gate denials; assert envelope-only (no row) for the non-member probe. Use an in-project insufficient-role member when asserting the row.
- [ ] **Step 3: 3-factor gate** — for each destructive tool, drop one gate factor at a time (capability / role / profile opt-in) and assert denial.
- [ ] **Step 4: Record RBAC-allow + RBAC-deny cells + file issues** (`GAP-RBAC` for any wrong authz/audit behavior).

---

## Phase 10 — Arc 8: fault-inject error paths

### Task 16: Drive control/capture error paths on fault-inject

- [ ] **Step 1: Drive the fault-inject provider's control/capture error paths** (seeded decision-keyed faults). fault-inject does NOT run the Arc 2–5 build session — its build/boot/lifecycle cells are `—`.
- [ ] **Step 2: Assert error envelopes + `error_category`** match the expected taxonomy (`domain/errors.py`).
- [ ] **Step 3: Record cells + file issues** (`BUG` for wrong envelope/category).

---

## Phase 11 — Report assembly + issue consolidation

### Task 17: Render the grid and assemble the report

**Files:**
- Create: `docs/reports/mcp-coverage-campaign-2026-06-13.md`

- [ ] **Step 1: Merge results into the grid**

Run:
```bash
uv run python -c "
import json
from scripts.coverage_campaign.gridgen import generate_rows
from scripts.coverage_campaign.results import CellResult, merge_and_render
rows = generate_rows()
results = [CellResult(**json.loads(l)) for l in open('artifacts/coverage-campaign/results.jsonl')]
print(merge_and_render(rows, results))
" > /tmp/grid.md
```
Expected: a markdown grid with one row per tool.

- [ ] **Step 2: Assemble the report** in the spec §7 order: (1) census grid (from Step 1), (2) per-arc narrative, (3) issue index, (4) topology + reproduction appendix (from `manifest.json`). Include the §8 size figure and the priority-order coverage actually achieved; mark any uncovered cells `⏭` with reason (completion criterion: every non-`—` cell is `✅`/`⏭`/issue#).

- [ ] **Step 3: Doc-style guard**

Run: `rg -in 'critical|robust|comprehensive|elegant|sprint|crucial|essential|significant' docs/reports/mcp-coverage-campaign-2026-06-13.md || echo clean`
Expected: clean.

- [ ] **Step 4: Commit the report**

```bash
git add docs/reports/mcp-coverage-campaign-2026-06-13.md
git commit -m "docs(report): MCP tool coverage campaign results"
```

### Task 18: Consolidate and file issues

- [ ] **Step 1: Build the consolidated issue list** from all `CellResult`s with a non-pass verdict: title, taxonomy label (`type:gap`/`type:bug` + `area:*`), env+provider, exact tool+args+role repro, expected vs actual, grid back-link.

- [ ] **Step 2: Show the list to the user for confirmation (spec §6) BEFORE bulk filing.** Do not file in bulk until approved.

- [ ] **Step 3: File approved issues** with `gh issue create`, capture the numbers, and back-fill them into the grid/report (re-run Task 17 Step 1–2 so the grid cells carry `(#N)`).

- [ ] **Step 4: Final commit + branch handoff**

```bash
git add docs/reports/mcp-coverage-campaign-2026-06-13.md
git commit -m "docs(report): backfill filed issue numbers into coverage grid"
```
Then use `superpowers:finishing-a-development-branch` to decide merge/PR.

---

## Self-review notes (author check)

- **Spec coverage:** every spec section maps to a phase — §2 grid → Phase 1/11; §2.5 driver+PASS → Phases 1,3–10 assertions; §3 topology + §5 Arc 0 → Phase 2; §4 dimensions → Phases 4–10; §5 arcs → Phases 3–10 in the §5 order; §5.1 cleanup/capacity → Tasks 9/13; §6 taxonomy → every "file issues" step + Task 18; §7 report order → Task 17; §8 budget/priority/completion → Tasks 4/17; §9 out-of-scope → not implemented (correct); §10 risks → Task 6 preflights.
- **Placeholder scan:** Phases 2–10 are operational by design (no fictional code); each gives the exact tool sequence, the §2.5 PASS assertion, and the issue action. Phase 1 (the only new code) has complete TDD code.
- **Type consistency:** `CensusRow` / `CellResult` field names are identical across `gridgen.py`, `results.py`, and the tests; `merge_and_render(rows, results)` signature is consistent in Task 3 and Task 17.
- **Known follow-up to verify at execution:** exact env-var names for the fault-inject and remote-libvirt opt-ins (Task 5 Step 2) and the `make_keypair`/`ISSUER`/`AUDIENCE` import path (Task 2 Step 3) must be confirmed against the current tree, since they are the two places the plan names symbols it did not re-read in full.
