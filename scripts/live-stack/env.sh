#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
default_database_url="postgresql://kdive:kdive@localhost:5432/kdive" # pragma: allowlist secret

export KDIVE_DATABASE_URL="${KDIVE_DATABASE_URL:-${default_database_url}}"
export KDIVE_OIDC_ISSUER="${KDIVE_OIDC_ISSUER:-http://localhost:8090/default}"
export KDIVE_OIDC_JWKS_URI="${KDIVE_OIDC_JWKS_URI:-http://localhost:8090/default/jwks}"
export KDIVE_OIDC_AUDIENCE="${KDIVE_OIDC_AUDIENCE:-kdive}"
export KDIVE_S3_ENDPOINT_URL="${KDIVE_S3_ENDPOINT_URL:-http://localhost:9000}"
export KDIVE_S3_BUCKET="${KDIVE_S3_BUCKET:-kdive-artifacts}"
export KDIVE_S3_REGION="${KDIVE_S3_REGION:-us-east-1}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-minioadmin}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-minioadmin}"
export KDIVE_HTTP_HOST="${KDIVE_HTTP_HOST:-127.0.0.1}"
export KDIVE_HTTP_PORT="${KDIVE_HTTP_PORT:-8000}"
export KDIVE_STACK_BASE_URL="${KDIVE_STACK_BASE_URL:-http://${KDIVE_HTTP_HOST}:${KDIVE_HTTP_PORT}/mcp}"
export KDIVE_BUILD_WORKSPACE="${KDIVE_BUILD_WORKSPACE:-${repo_root}/.live-build}"
export KDIVE_BUILD_COMPONENT_ROOTS="${KDIVE_BUILD_COMPONENT_ROOTS:-${repo_root}/fixtures/local-libvirt:${repo_root}/.live-components}"
export KDIVE_INSTALL_STAGING="${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
export KDIVE_KERNEL_SRC="${KDIVE_KERNEL_SRC:-${HOME}/src/linux}"
