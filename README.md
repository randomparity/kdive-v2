Kernel Debug, Inspect, Validate, Explore (KDIVE)
================================================

An MCP platform for Linux kernel build-boot-debug workflows. See
[`docs/specs/top-level-design.md`](docs/specs/top-level-design.md) for the
architecture and the milestone plans:
[`M0`](docs/plans/m0-implementation.md) and [`M1`](docs/plans/m1-implementation.md).

Requirements
------------

- Python 3.13 and [`uv`](https://docs.astral.sh/uv/).
- [`just`](https://github.com/casey/just) (task runner) and
  [`prek`](https://github.com/j178/prek) (git hooks). `just setup` cannot bootstrap the
  runner itself, so install it first: `uv tool install rust-just && uv tool install prek`.
- A host build dependency for `libvirt-python`, which has no prebuilt wheels and
  compiles against the system libvirt headers:

  ```bash
  sudo apt-get install -y libvirt-dev   # provides pkg-config metadata + headers
  ```

  The other native runtime dependency needs nothing extra: `psycopg[binary]` bundles libpq.

Setup
-----

```bash
just setup   # check host deps, create the venv, install and run the git hooks
```

`just check-deps` reports any missing host packages — grouped by tier, with a single
distro-specific install command (apt/dnf/pacman/zypper) — and never installs anything
itself. Run `just` (or `just --list`) to see every task:

| task | what it runs |
|------|--------------|
| `just lint`    | `ruff check` + `ruff format --check` |
| `just format`  | `ruff check --fix` + `ruff format` |
| `just type`    | `ty check` (whole tree: src + tests) |
| `just test`    | the suite, excluding the gated `live_vm` and `live_stack` tests |
| `just test-live` | the gated `live_vm` suite |
| `just stack-up` | Postgres + MinIO + mock OIDC, migrated for a live host run |
| `just test-live-stack` | the gated HTTP/live-stack suite |
| `just ci`      | the full gate PR CI runs (lint, type, shell, workflows, mermaid, test) |

The underlying commands still work directly if you prefer not to use `just`:

```bash
uv sync                      # create the venv and install pinned dependencies
uv run ruff check .          # lint
uv run ty check              # type-check (whole tree)
uv run python -m pytest -q   # run the test suite
prek install && prek run -a  # install and run the git hooks
```

Test environments
-----------------

- **Unit and service tests** run anywhere. They depend only on a disposable
  Postgres, in-process fakes, and containerized test fixtures where the test needs a real
  backing service. They do not require a real kernel or VM. Tests that need Docker-backed
  services skip cleanly when Docker is absent, unless `KDIVE_REQUIRE_DOCKER=1` is set.
- **`live_stack` tests** are marked with the `live_stack` pytest marker and are skipped
  by `just test`. They drive the real MCP HTTP transport against host `server`, `worker`,
  and `reconciler` processes plus the compose backends. Use `just stack-up` to start
  Postgres, MinIO, and the mock OIDC issuer, then follow
  [`docs/runbooks/live-stack.md`](docs/runbooks/live-stack.md) for VM fixtures and host
  process startup.
- **`live_vm` tests** are marked with the `live_vm` pytest marker and are **skipped by
  default**. They require:
  - a **KVM / nested-virtualization-capable host** with libvirt installed and running;
  - a **kdump-enabled guest image** — one built with a `crashkernel=` reservation and
    the kdump capture service, so a forced crash produces a vmcore.
  - **host kernel-debugging tools** for the gated debug surfaces, including operator-
    provided `drgn`. `drgn` is intentionally not imported during normal service startup;
    the real drgn seams stay disabled until the live runner injects them.

  These do **not** run on stock GitHub-hosted runners. CI marks `live_vm` as a
  separate, manually-triggered job on a self-hosted KVM runner.
- **Kernel builds** are implemented for both the local-libvirt and remote-libvirt
  providers. Server-side builds run on the worker and need a kernel **toolchain**
  (gcc/clang, make, bc, flex, bison, libelf-dev), `git`, `rsync`, a writable build
  workspace, and a warm kernel source tree pointed to by `KDIVE_KERNEL_SRC`. KDIVE
  copies that warm tree into a per-Run workspace and builds incrementally there.
  Remote-libvirt packages `boot/vmlinuz` plus `lib/modules/...` into the install bundle
  so the guest can install the kernel in-target. External build ingestion is also
  implemented through upload manifests and `runs.complete_build`.

To run the gated suite once the prerequisites are present:

```bash
uv run python -m pytest -m live_vm
```

For the HTTP live-stack path:

```bash
just stack-up
just test-live-stack
```

Releasing
---------

See [`docs/RELEASING.md`](docs/RELEASING.md) for the versioning policy
([ADR-0041](docs/adr/0041-versioning-release-process.md)) and the release process.
