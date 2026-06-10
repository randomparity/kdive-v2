# syntax=docker/dockerfile:1
# kdive control-plane image (ADR-0088): one multi-stage image for all three
# entrypoints (server/worker/reconciler) plus the migrate one-shot, built to
# drive the remote-libvirt and fault-inject providers over the network.
# local-libvirt stays a venv-on-a-libvirt-host dev/CI provider, not containerized.

# Builder: resolve the uv environment (deps first for layer caching, then project).
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 /uv /usr/local/bin/uv
# libvirt-python ships no wheels; it compiles against the libvirt headers via
# pkg-config (AGENTS.md). These build-only deps stay in the builder stage and never
# reach the final image, which carries just the runtime shared lib.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libc6-dev libvirt-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# link-mode=copy: the uv cache mount and /opt/venv are on different filesystems, so
# hardlinking falls back to a copy with a warning; ask for the copy explicitly.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Final: slim base + worker toolchain (drives remote-libvirt over the network).
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2
# All real bookworm packages. drgn is NOT installed via apt: bookworm ships only
# the python3-drgn library, whose CLI/version is unproven for the `drgn --version`
# build check; we install drgn from its pinned PyPI manylinux wheel (below) into the
# same venv so both the `drgn` CLI and `import drgn` work. libelf1/libdw1/zlib1g are
# drgn's runtime shared libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc make binutils gdb libvirt-clients openssh-client \
      libelf1 libdw1 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 /uv /usr/local/bin/uv
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
# Put the venv on PATH before the drgn install + verification so the bare `drgn`
# check resolves. PYTHONPATH backs the editable project install at the copied src path.
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src \
    KDIVE_BUILD_WORKSPACE=/var/lib/kdive/build \
    KDIVE_INSTALL_STAGING=/var/lib/kdive/install
# drgn from its prebuilt wheel into the venv (CLI + import both available).
RUN uv pip install --python /opt/venv/bin/python "drgn==0.2.0"
# Fail the build (not just the gated smoke test) if any worker tool is missing/broken.
RUN drgn --version && gdb --version && virsh --version && gcc --version && make --version
# Fixed non-root uid 10001 (k8s runAsNonRoot convention) so compose/Helm can chown the
# mounted writable volumes to a known owner. --no-log-init avoids a sparse lastlog
# allocation for the high uid; not --system (that caps the uid below SYS_UID_MAX).
RUN useradd --create-home --no-log-init --uid 10001 kdive \
    && mkdir -p /var/lib/kdive/build /var/lib/kdive/install \
    && chown -R kdive:kdive /var/lib/kdive
USER kdive
WORKDIR /app
ENTRYPOINT ["python", "-m", "kdive"]
CMD ["server"]
