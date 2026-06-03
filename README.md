Kernel Debug, Inspect, Validate, Explore (KDIVE)
================================================

An MCP platform for Linux kernel build-boot-debug workflows. See
[`docs/specs/top-level-design.md`](docs/specs/top-level-design.md) for the
architecture and [`docs/plans/m0-implementation.md`](docs/plans/m0-implementation.md)
for the current milestone plan.

Requirements
------------

- Python 3.13 and [`uv`](https://docs.astral.sh/uv/).
- A host build dependency for `libvirt-python`, which has no prebuilt wheels and
  compiles against the system libvirt headers:

  ```bash
  sudo apt-get install -y libvirt-dev   # provides pkg-config metadata + headers
  ```

  The other native dependencies need nothing extra: `drgn` ships a manylinux wheel
  and `psycopg[binary]` bundles libpq.

Setup
-----

```bash
uv sync                  # create the venv and install pinned dependencies
uv run ruff check .      # lint
uv run ruff format .     # format
uv run ty check src      # type-check
uv run python -m pytest -q   # run the test suite
```

Install the git hooks once with `prek install`; run them across the tree with
`prek run -a`.

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
