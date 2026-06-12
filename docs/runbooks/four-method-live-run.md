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

- The `build_config_catalog` migration applied (migration `0025_build_config_catalog`). This is
  handled by `python -m kdive migrate` or `just stack-up`.
- The kdump fragment seeded: `python -m kdive seed-build-configs` (or `python -m kdive migrate`
  which calls the seed step). Verify below in Step 1.
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
import asyncio, httpx, json
token = open('/tmp/operator-token').read().strip()   # or export KDIVE_TOKEN
resp = httpx.post(
    'http://127.0.0.1:8000/mcp',
    json={'method': 'tools/call', 'params': {'name': 'buildconfig.get', 'arguments': {'name': 'kdump'}}},
    headers={'Authorization': f'Bearer {token}'},
)
print(json.dumps(resp.json(), indent=2))
"
```

Expected: a response with `content` (the `CONFIG_*` fragment), `sha256`, and `merge_recipe`.
The `sha256` must match the database row's `sha256`.

If the row is absent, re-run the seed step:

```bash
python -m kdive migrate   # applies migrations and calls seed_build_configs
```

## 2. Build a from-source kernel (no explicit config)

Allocate a System (call `allocations.request` then `systems.provision`) and queue a build run
**without** supplying a `config` field in the build profile. The omitted field causes the build
to default to the `kdump` catalog entry (ADR-0096).

```python
# Illustrative profile — omit "config" entirely:
build_profile = {
    "schema_version": 1,
    "kernel_source_ref": os.environ["KDIVE_KERNEL_SRC"],   # e.g. "file:///src/linux"
}
# Call runs.build with this profile. No "config" key → kdump catalog default fires.
```

Poll `jobs.wait` until the build job reaches `completed`. The worker log should show the
fragment-survival check:

```
[build] fragment-survival check: 10/10 symbols present in .config
```

If the build fails with `configuration_error` and `details.dropped`, a fragment symbol was
dropped by `make olddefconfig` — this means the kernel tree does not satisfy a dependency the
kdump fragment requires. Check the worker log for which symbols were dropped, then either
update the fragment or the kernel version.

## 3. Install and boot the built kernel

Call `runs.install` on the completed build run, then `runs.boot`. Poll `jobs.wait` until the
System reaches `ready`.

```
runs.install  → job: queued → wait → completed
runs.boot     → job: queued → wait → System state: ready (or boot_timeout)
```

If the System reaches `boot_timeout`, check the console artifact for boot messages. A missing
`CONFIG_RELOCATABLE` or `CONFIG_RANDOMIZE_BASE` suggests the fragment did not apply — check the
build log for the survival check result.

## 4. Drive the four capture methods

The four methods require **two** Systems because `host_dump` and `kdump` are both vmcore methods
and `ensure_method_match` (ADR-0050) binds the first captured method per System. Drive them as
in the [M2.5 capstone](remote-live-stack.md#6-four-method-capture-capstone-m25):

| method | System | how |
|--------|--------|-----|
| `gdbstub` | **B** (booted, from-source kernel) | `debug.attach kind=gdb-mi` → attach → `debugsessions.eval` |
| `kdump` | **B** (after gdbstub, or a fresh boot) | `control.force_crash` → `vmcore.fetch` → `introspect.from_vmcore` |
| `console` | **B** (over the same boot lifetime) | `artifacts.list` for the console artifact after teardown/finalize |
| `host_dump` | **A** (separate System, provisioned and crashed) | `control.force_crash` → `vmcore.fetch` via host-side `virDomainCoreDumpWithFormat` |

### 4a. gdbstub

With System B in the `ready` state, attach a gdb-MI session:

```
debug.attach system_id=<B> kind=gdb-mi
```

Confirm the session reaches `attached` and a `debugsessions.eval` call returns a non-empty
result. The DWARF debuginfo from `CONFIG_DEBUG_INFO_DWARF5` (in the kdump fragment) drives
symbolization.

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

Then run the postmortem:

```
introspect.from_vmcore system_id=<B>
```

Confirm a non-empty `tasks` dict in the response. A missing `VMCOREINFO` is a `configuration_error`
and is **not** a 4/4 pass — do not accept a missing-build-id skip as success.

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
- The worker log excerpt showing the fragment-survival check from Step 2.
- The `artifacts.list` output for each System showing the vmcore and console artifacts.

The passing run confirms that a from-source kernel built with the kdump catalog default is
kdump-capable, symbolizable (gdbstub/DWARF), and console-observable — the three previously
blocked methods now work alongside `host_dump`.
