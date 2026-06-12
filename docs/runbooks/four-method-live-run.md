# Runbook: four-method live run on the from-source System

Operator guide for validating all four capture methods — `kdump`, `gdbstub`, `console`, and
`host_dump` — on a System whose kernel is built from source using the seeded kdump
config-fragment catalog. This is the **acceptance gate for the kernel-build-config provisioning
milestone** (ADR-0096). Like prior milestones' real-hardware runs, it is **operator-run, not CI**:
the `live_stack` suite skips cleanly on hosts without the prerequisites.

For the stack bring-up steps shared by every live run (backends, env, VM fixtures, host processes),
follow the [local live-stack runbook](live-stack.md) §1–4 first. The remote `qemu+tls://` variant
additionally needs the steps in [remote-live-stack.md](remote-live-stack.md) §1–4. Run this runbook
**after** the stack is up.

## Prerequisites

All prerequisites from the [local live-stack runbook](live-stack.md), plus:

- The `build_config_catalog` migration applied (migration `0025_build_config_catalog`) and the
  kdump fragment seeded. Both are handled by `python -m kdive migrate` (the migrate step calls the
  build-config seed after applying migrations) or by `just stack-up`. Verify below in Step 1.
- A kernel source tree at `KDIVE_KERNEL_SRC` (same fixture as the live-stack suite; see
  [live-stack.md §3](live-stack.md#3-build-the-vm-fixtures)).
- The remote provider configured with a base-OS qcow2 and TLS, if running against a remote
  `qemu+tls://` host (see [remote-live-stack.md §1–4](remote-live-stack.md)).

## 1. Verify the seed

Confirm the `build_config_catalog` row is present and the object-store artifact is reachable.

**Via the database:**

```bash
uv run psql "$KDIVE_DATABASE_URL" -c \
  "SELECT name, object_key, sha256 FROM build_config_catalog WHERE name = 'kdump';"
```

Expected: one row with `name=kdump`, an `object_key` of the form
`system/build-configs/kdump/kdump.config`, and a non-empty `sha256`.

**Via the MCP tool** (with the stack up and a valid operator token):

```bash
uv run python -c "
import httpx, json
token = open('/tmp/operator-token').read().strip()   # or export KDIVE_TOKEN
resp = httpx.post(
    'http://127.0.0.1:8000/mcp',
    json={'method': 'tools/call', 'params': {'name': 'buildconfig.get', 'arguments': {'name': 'kdump'}}},
    headers={'Authorization': f'Bearer {token}'},
)
print(json.dumps(resp.json(), indent=2))
"
```

Expected: a `ToolResponse` with `status: available` whose `data` carries `content` (the
`CONFIG_*` fragment), `sha256`, and `merge_recipe`. The `data.sha256` must match the database
row's `sha256`.

If the row is absent, re-run the seed step:

```bash
python -m kdive migrate   # applies migrations and calls seed_build_configs
```

## 2. Build a from-source kernel (no explicit config)

Allocate a System (call `allocations.request` then `systems.provision`), then create a Run
**without** supplying a `config` field in the build profile. The omitted field causes the build
to default to the `kdump` catalog entry (ADR-0096). The build profile goes to `runs.create`;
`runs.build` then takes only the resulting `run_id`.

```python
# Illustrative profile passed to runs.create — omit "config" entirely:
build_profile = {
    "schema_version": 1,
    "kernel_source_ref": os.environ["KDIVE_KERNEL_SRC"],   # e.g. "file:///src/linux"
}
# 1. runs.create(investigation_id=<inv>, system_id=<B>, build_profile=build_profile)
#    No "config" key in build_profile → kdump catalog default fires at build time.
# 2. runs.build(run_id=<the run id returned by runs.create>)
```

Poll `jobs.wait` until the build job reaches `completed`. Success is silent — the build emits no
per-symbol log line on a clean merge. The fragment-survival check fires only on a problem: if a
fragment symbol is dropped by `make olddefconfig`, the build fails with `configuration_error` and
the error `details.dropped` names the dropped symbol(s). A dropped symbol means the kernel tree
does not satisfy a dependency the kdump fragment requires; either update the fragment or the
kernel version.

## 3. Install and boot the built kernel

Call `runs.install` then `runs.boot`, both keyed on the build `run_id`. Poll `jobs.wait` until
the System reaches `ready`.

```
runs.install(run_id=<...>)  → job: queued → wait → completed
runs.boot(run_id=<...>)     → job: queued → wait → System state: ready (or boot_timeout)
```

If the System reaches `boot_timeout`, check the console artifact for boot messages. Since a clean
build does not log the merged config, confirm the fragment applied by calling
`buildconfig.get name=kdump` (the symbols it lists are what the build merged) and comparing
against the booted kernel.

## 4. Drive the four capture methods

The four methods require **two** Systems because `host_dump` and `kdump` are both vmcore methods
and `ensure_method_match` (ADR-0050) binds the first captured method per System. Drive them as
in the [M2.5 capstone](remote-live-stack.md#6-four-method-capture-capstone-m25):

| method | System | how |
|--------|--------|-----|
| `gdbstub` | **B** (booted, from-source kernel) | `debug.start_session transport=gdbstub` → gdb-MI ops (`debug.set_breakpoint`, `debug.continue`, `debug.read_registers`) → `debug.end_session` |
| `kdump` | **B** (after gdbstub, or a fresh boot) | `control.force_crash` → `vmcore.fetch method=kdump` → `introspect.from_vmcore run_id=<B's run>` |
| `console` | **B** (over the same boot lifetime) | `artifacts.list` for the console artifact after teardown/finalize |
| `host_dump` | **A** (separate System, provisioned and crashed) | `control.force_crash` → `vmcore.fetch method=host_dump` via host-side `virDomainCoreDumpWithFormat` |

`control.force_crash` is a destructive op: it requires the `admin` role plus the three-factor
destructive-op gate (capability scope + RBAC role + the provisioning profile's `force_crash`
opt-in). Provision Systems A and B from a profile that opts into `force_crash` and drive the
crash with an admin token.

### 4a. gdbstub

With the Run booted and System B in the `ready` state, open a single-attach gdbstub session,
keyed on the **run_id** (not the System):

```
debug.start_session(run_id=<B's run>, transport=gdbstub)
```

Confirm the session reaches the `live` state. Then drive the gdb-MI ops the gdbstub transport
exposes — for example set a breakpoint on a kernel symbol, continue, and read registers when it
hits:

```
debug.set_breakpoint(session_id=<...>, location=<kernel symbol>)
debug.continue(session_id=<...>)
debug.read_registers(session_id=<...>, registers=["rip", "rsp"])
debug.end_session(session_id=<...>)
```

The DWARF debuginfo from `CONFIG_DEBUG_INFO_DWARF5` (in the kdump fragment) is what makes the
symbol-name breakpoint resolve. (Live drgn introspection — `introspect.run` — runs over a
`drgn-live` session, not a gdbstub one, so it is a separate transport; the gdbstub leg here proves
the gdb-MI attach + symbolization.)

### 4b. kdump

Force a crash on System B:

```
control.force_crash system_id=<B>
```

Poll until the System reaches `crashed`. Then fetch the vmcore:

```
vmcore.fetch system_id=<B> method=kdump
```

The worker queues a capture job that waits out the guest's crash→reboot→upload window
(see the [capture budget note](remote-live-stack.md#5-run-the-suite)). Poll `jobs.wait` until
`completed`. Confirm the artifact appears in `artifacts.list system_id=<B>`.

Then run the postmortem, keyed on the build **run_id** (it resolves the Run's `debuginfo_ref` and
its System's captured core):

```
introspect.from_vmcore(run_id=<B's run>)
```

Confirm a non-empty `tasks` dict in the response's `report`. A missing `VMCOREINFO` is a
`configuration_error` and is **not** a 4/4 pass — do not accept a missing-build-id skip as success.

### 4c. console

The console artifact assembles on System teardown. After System B's lifecycle ends:

```
artifacts.list system_id=<B>
```

Confirm a `console` artifact is present. If the artifact is absent, check whether the reconciler
collected the console stream — the reconciler-hosted `virDomainOpenConsole` collector (ADR-0095)
assembles the artifact on teardown-finalize, so assert it **after** the System is `torn_down`.

### 4d. host_dump

On a **separate** System A (provisioned and in the `ready` state):

```
control.force_crash system_id=<A>
vmcore.fetch system_id=<A> method=host_dump
```

The worker uses `virDomainCoreDumpWithFormat` on the host side and streams the resulting
storage-pool volume through the object store (ADR-0094). Poll `jobs.wait` until `completed`,
then confirm the artifact in `artifacts.list system_id=<A>`.

## 5. Record evidence

A successful four-method run on the from-source System is the acceptance gate for the
kernel-build-config provisioning milestone. Attach the following as the recorded evidence:

- The `build_config_catalog` query output from Step 1 (row present, sha256 non-empty).
- The `buildconfig.get name=kdump` response (fragment content + sha256 matches the row).
- The completed build job from Step 2 (a clean build is the survival-check pass — it raises
  `configuration_error` with `details.dropped` only on a dropped symbol).
- The `artifacts.list` output for each System showing the vmcore and console artifacts.

The passing run confirms that a from-source kernel built with the kdump catalog default is
kdump-capable, symbolizable (gdbstub/DWARF), and console-observable — the three previously
blocked methods now work alongside `host_dump`.
