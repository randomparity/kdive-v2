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
| `just test`    | the suite, excluding the gated `live_vm` tests |
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
  Postgres + MinIO + a mock OIDC issuer (added in a later milestone issue), never on
  a real kernel or VM.
- **`live_vm` tests** are marked with the `live_vm` pytest marker and are **skipped by
  default**. They require:
  - a **KVM / nested-virtualization-capable host** with libvirt installed and running;
  - a **kdump-enabled guest image** — one built with a `crashkernel=` reservation and
    the kdump capture service, so a forced crash produces a vmcore.

  These do **not** run on stock GitHub-hosted runners. CI marks `live_vm` as a
  separate, manually-triggered job on a self-hosted KVM runner.
- **Kernel builds** (a later milestone issue) need a kernel **toolchain**
  (gcc/clang, make, bc, flex, bison, libelf-dev) and a **warm kernel source tree** in
  the build workspace. The milestone builds incrementally from the warm tree, not
  from scratch.

To run the gated suite once the prerequisites are present:

```bash
uv run python -m pytest -m live_vm
```

Releasing
---------

See [`docs/RELEASING.md`](docs/RELEASING.md) for the versioning policy
([ADR-0041](docs/adr/0041-versioning-release-process.md)) and the release process.
