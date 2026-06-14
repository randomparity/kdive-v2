# Kernel Debug, Inspect, Validate, Explore (KDIVE)

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

KDIVE is an MCP platform for the Linux kernel build-boot-debug lifecycle. A
single multi-user service owns the full chain across heterogeneous resources —
claim a resource, provision a system, build and install a kernel, boot it, attach
a debugger, crash it, and retrieve the vmcore — exposed through one uniform MCP
tool surface. See [`docs/design/top-level-design.md`](docs/design/top-level-design.md)
for the architecture.

## Where to start

- **Use KDIVE (agents and users)** — drive the tool surface from an MCP client:
  [`docs/guide/index.md`](docs/guide/index.md).
- **Run KDIVE (operators)** — install and deploy the processes:
  [`docs/operating/install.md`](docs/operating/install.md).
- **Develop KDIVE (contributors)** — the dev loop and the PR gate:
  [`CONTRIBUTING.md`](CONTRIBUTING.md).

The full documentation index is [`docs/README.md`](docs/README.md).

## Quickstart (development)

KDIVE is Python 3.13, managed with [`uv`](https://docs.astral.sh/uv/). `just
setup` cannot bootstrap its own runner, so install `just` and `prek` first:

```bash
uv tool install rust-just prek
just setup   # check host deps, sync the venv, install and run the git hooks
```

`libvirt-python` has no prebuilt wheels and compiles against the system libvirt
headers, so install `libvirt-dev` (or your distro's equivalent) first;
`just check-deps` reports any missing host packages without installing anything.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the development loop and
[`docs/operating/install.md`](docs/operating/install.md) for the full
host-prerequisite list.

## Test environments

`just test` runs the suite, excluding the gated `live_vm` and `live_stack` tests:

- **Unit and service tests** run anywhere. They depend only on a disposable
  Postgres, in-process fakes, and containerized test fixtures. Tests that need
  Docker skip cleanly when it is absent unless `KDIVE_REQUIRE_DOCKER=1` is set.
- **`live_stack` tests** drive the real MCP HTTP transport against host `server`,
  `worker`, and `reconciler` processes plus the compose backends. Run
  `just stack-up` then `just test-live-stack`; see
  [`docs/operating/runbooks/live-stack.md`](docs/operating/runbooks/live-stack.md).
- **`live_vm` tests** need a KVM-capable host with libvirt, a kdump-enabled guest
  image (built with a `crashkernel=` reservation and the kdump capture service),
  and operator-provided host kernel-debugging tools including `drgn`. They are
  skipped by default and do not run on stock GitHub-hosted runners. Run them with
  `uv run python -m pytest -m live_vm`.

Server-side kernel builds run on the worker and need a kernel toolchain
(gcc/clang, make, bc, flex, bison, libelf-dev), `git`, `rsync`, a writable build
workspace, and a warm source tree pointed to by `KDIVE_KERNEL_SRC`. Both the
local-libvirt and remote-libvirt providers build, and external build ingestion
is supported through upload manifests and `runs.complete_build`.

## Releasing

See [`docs/development/releasing.md`](docs/development/releasing.md) for the
versioning policy ([ADR-0041](docs/adr/0041-versioning-release-process.md)) and
the release process.

## License

KDIVE is licensed under the Apache License 2.0 — see [`LICENSE`](LICENSE).
