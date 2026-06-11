"""Core (provider-agnostic) ``KDIVE_*`` settings (ADR-0087).

Platform settings the server/worker/reconciler/migrate processes consume directly:
database, HTTP bind, logging, OIDC, object store, lease bounds, upload limits, the
worker storage paths (build/install/crash/debug/secrets), the fixture catalog, and the
fault-injection enable gate. Provider-specific knobs are co-located with their provider
(``providers/*/…``) and aggregated through the manifest, not declared here.

Readers that apply their own domain parsing (lease windows, paths) declare ``parse=str``
and keep that parsing at the call site; this preserves their existing validation and
error details while still routing the read through the registry.
"""

from __future__ import annotations

from collections.abc import Mapping

from kdive.config.registry import RUNNABLE, Setting

_SERVER = frozenset({"server"})
_STORE_USERS = frozenset({"server", "worker", "reconciler"})
_WORKER = frozenset({"worker"})
_DISCOVERY = frozenset({"worker", "reconciler"})


def _int(raw: str) -> int:
    return int(raw)


def _str(raw: str) -> str:
    return raw


def _ratio(raw: str) -> float:
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"must be in [0, 1], got {value}")
    return value


def _always(env: Mapping[str, str]) -> bool:
    return True


DATABASE_URL = Setting(
    name="KDIVE_DATABASE_URL",
    parse=_str,
    group="database",
    processes=RUNNABLE,
    required_when=_always,
    help="Postgres DSN for the system-of-record.",
    suggest="a Postgres DSN, e.g. postgresql://host:5432/kdive",
)

HTTP_HOST = Setting(
    name="KDIVE_HTTP_HOST",
    parse=_str,
    default="127.0.0.1",
    group="http",
    processes=_SERVER,
    help="Bind host for the MCP server.",
)
HTTP_PORT = Setting(
    name="KDIVE_HTTP_PORT",
    parse=_int,
    default="8000",
    group="http",
    processes=_SERVER,
    help="Bind port for the MCP server.",
    suggest="an integer port, e.g. 8000",
)

LOG_LEVEL = Setting(
    name="KDIVE_LOG_LEVEL",
    parse=_str,
    default="INFO",
    group="logging",
    processes=RUNNABLE,
    help="Structured-logging level (overridable by --log-level).",
)

OIDC_JWKS_URI = Setting(
    name="KDIVE_OIDC_JWKS_URI",
    parse=_str,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="JWKS URI the bearer-token verifier fetches signing keys from.",
    suggest="the issuer's JWKS endpoint, e.g. http://oidc:8080/default/jwks",
)
OIDC_ISSUER = Setting(
    name="KDIVE_OIDC_ISSUER",
    parse=_str,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="Expected token issuer (iss), enforced natively.",
    suggest="the OIDC issuer URL, e.g. http://oidc:8080/default",
)
OIDC_AUDIENCE = Setting(
    name="KDIVE_OIDC_AUDIENCE",
    parse=_str,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="Expected token audience (aud), enforced natively.",
    suggest="the audience this server accepts, e.g. kdive",
)

S3_ENDPOINT_URL = Setting(
    name="KDIVE_S3_ENDPOINT_URL",
    parse=_str,
    group="objectstore",
    processes=_STORE_USERS,
    help="S3-compatible endpoint URL for bulk artifacts.",
)
S3_BUCKET = Setting(
    name="KDIVE_S3_BUCKET",
    parse=_str,
    group="objectstore",
    processes=_STORE_USERS,
    help="Bucket holding vmcores, transcripts, and uploads.",
)
S3_REGION = Setting(
    name="KDIVE_S3_REGION",
    parse=_str,
    default="us-east-1",
    group="objectstore",
    processes=_STORE_USERS,
    help="Region for the object-store client.",
)

LEASE_DEFAULT = Setting(
    name="KDIVE_LEASE_DEFAULT",
    parse=_str,
    group="lease",
    processes=_SERVER,
    help="Default lease window (hours) when a request omits one (built-in 4).",
)
LEASE_MAX = Setting(
    name="KDIVE_LEASE_MAX",
    parse=_str,
    group="lease",
    processes=_SERVER,
    help="Hard cap (hours) on a lease window / renewal (built-in 24).",
)

UPLOAD_TTL_SECONDS = Setting(
    name="KDIVE_UPLOAD_TTL_SECONDS",
    parse=_int,
    default="86400",
    group="upload",
    processes=_SERVER,
    help="Presigned upload-URL TTL in seconds.",
    suggest="an integer number of seconds, e.g. 86400",
)
MAX_UPLOAD_BYTES = Setting(
    name="KDIVE_MAX_UPLOAD_BYTES",
    parse=_int,
    default=str(5 * 1024 * 1024 * 1024),
    group="upload",
    processes=_SERVER,
    help="Maximum accepted upload size in bytes.",
    suggest="an integer number of bytes",
)

DEBUG_DIR = Setting(
    name="KDIVE_DEBUG_DIR",
    parse=_str,
    default="/var/lib/kdive/debug",
    group="debug",
    processes=_WORKER,
    help="Directory for debug-session transcripts.",
)
CRASH_DIR = Setting(
    name="KDIVE_CRASH_DIR",
    parse=_str,
    group="debug",
    processes=_WORKER,
    help="Directory for local kdump crash captures (live_vm path).",
)

SECRETS_ROOT = Setting(
    name="KDIVE_SECRETS_ROOT",  # pragma: allowlist secret - env var name, not a value
    parse=_str,
    default="/var/lib/kdive/secrets",
    group="secrets",
    processes=_STORE_USERS,
    help="Root directory for the file-ref secret backend.",
)

SSH_KEY_DIR = Setting(
    name="KDIVE_SSH_KEY_DIR",
    parse=_str,
    group="ssh",
    processes=_WORKER,
    help=(
        "Override for the managed SSH keypair directory (absolute). Read standalone by "
        "the builder's host python; documented here for the contract."
    ),
)

BUILD_WORKSPACE = Setting(
    name="KDIVE_BUILD_WORKSPACE",
    parse=_str,
    default="/var/lib/kdive/build",
    group="build",
    processes=_WORKER,
    help="Worker scratch root for kernel builds.",
)
KERNEL_SRC = Setting(
    name="KDIVE_KERNEL_SRC",
    parse=_str,
    default="",
    group="build",
    processes=_WORKER,
    help="Kernel source tree the worker builds from.",
)
BUILD_COMPONENT_ROOTS = Setting(
    name="KDIVE_BUILD_COMPONENT_ROOTS",
    parse=_str,
    group="build",
    processes=_WORKER,
    help="Colon-separated extra component roots merged into a build.",
)
INSTALL_STAGING = Setting(
    name="KDIVE_INSTALL_STAGING",
    parse=_str,
    default="/var/lib/kdive/install",
    group="install",
    processes=_WORKER,
    help="Worker staging root for install artifacts.",
)

FIXTURE_CATALOG_PATH = Setting(
    name="KDIVE_FIXTURE_CATALOG_PATH",
    parse=_str,
    group="catalog",
    processes=_DISCOVERY,
    help="Override path to the provider fixture catalog.",
)

FAULT_INJECT = Setting(
    name="KDIVE_FAULT_INJECT",
    parse=_str,
    group="fault-inject",
    processes=RUNNABLE,
    help="Presence (1/true/yes) registers the fault-injection provider.",
)

OTEL_ENABLED = Setting(
    name="KDIVE_OTEL_ENABLED",
    parse=_str,
    group="otel",
    processes=RUNNABLE,
    help="Presence (1/true/yes) enables OTLP export of logs/metrics/traces (default off).",
)
OTEL_EXPORTER_OTLP_ENDPOINT = Setting(
    name="KDIVE_OTEL_EXPORTER_OTLP_ENDPOINT",
    parse=_str,
    group="otel",
    processes=RUNNABLE,
    help="OTLP/gRPC collector endpoint; required when KDIVE_OTEL_ENABLED is set.",
    suggest="a gRPC collector endpoint, e.g. http://otel-collector:4317",
)
OTEL_TRACES_SAMPLER_RATIO = Setting(
    name="KDIVE_OTEL_TRACES_SAMPLER_RATIO",
    parse=_ratio,
    default="0.1",
    group="otel",
    processes=RUNNABLE,
    help="Parent-based ratio trace sampler ratio in [0, 1] (default 0.1).",
    suggest="a float in [0, 1], e.g. 0.1",
)
OTEL_SERVICE_NAMESPACE = Setting(
    name="KDIVE_OTEL_SERVICE_NAMESPACE",
    parse=_str,
    default="kdive",
    group="otel",
    processes=RUNNABLE,
    help="service.namespace resource attribute on all emitted telemetry.",
)

HEALTH_BIND_ADDR = Setting(
    name="KDIVE_HEALTH_BIND_ADDR",
    parse=_str,
    default="127.0.0.1:9464",
    group="health",
    processes=frozenset({"server", "worker", "reconciler"}),
    help=(
        "host:port for the aux health/metrics listener (/livez /readyz /metrics), "
        "distinct from the MCP port. Loopback by default — the network boundary is its "
        "access control; widening it is an explicit act."
    ),
    suggest="a host:port, e.g. 127.0.0.1:9464 (loopback) or 0.0.0.0:9464 (pod-local)",
)

SETTINGS = [
    DATABASE_URL,
    HTTP_HOST,
    HTTP_PORT,
    LOG_LEVEL,
    OIDC_JWKS_URI,
    OIDC_ISSUER,
    OIDC_AUDIENCE,
    S3_ENDPOINT_URL,
    S3_BUCKET,
    S3_REGION,
    LEASE_DEFAULT,
    LEASE_MAX,
    UPLOAD_TTL_SECONDS,
    MAX_UPLOAD_BYTES,
    DEBUG_DIR,
    CRASH_DIR,
    SECRETS_ROOT,
    SSH_KEY_DIR,
    BUILD_WORKSPACE,
    KERNEL_SRC,
    BUILD_COMPONENT_ROOTS,
    INSTALL_STAGING,
    FIXTURE_CATALOG_PATH,
    FAULT_INJECT,
    OTEL_ENABLED,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_TRACES_SAMPLER_RATIO,
    OTEL_SERVICE_NAMESPACE,
    HEALTH_BIND_ADDR,
]
