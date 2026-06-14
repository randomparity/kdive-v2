"""Preflight: the S3 endpoint a remote guest is handed must be guest-routable (ADR-0110).

The remote install and KDUMP-capture planes mint a presigned URL against
``KDIVE_S3_ENDPOINT_URL`` and have the *guest* `curl`/upload to it. The dev default
``http://localhost:9000`` is the guest's own loopback — there is no object store there — so the
in-guest transfer fails opaquely. This preflight rejects a loopback/localhost endpoint *before*
the presigned URL is minted, with an actionable ``configuration_error`` naming the env var, instead
of letting the downstream in-guest curl fail with no hint at the real cause.

It does no DNS resolution: only the literal ``localhost`` name and literal loopback IPs
(``127.0.0.0/8``, ``::1``) are rejected, so the check is deterministic and side-effect-free. An
*unset* endpoint is owned by ``object_store_from_env`` (which already fails naming the same var);
this preflight no-ops on a blank value so the two checks don't double-report.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import kdive.config as config
from kdive.config.core_settings import S3_ENDPOINT_URL
from kdive.domain.errors import CategorizedError, ErrorCategory

_LOOPBACK_NAME = "localhost"


def _host_of(endpoint: str) -> str:
    """The host of an S3 endpoint, tolerating a bare ``host:port`` with no scheme.

    ``urlsplit`` only populates ``hostname`` when a scheme (or ``//``) is present; a bare
    ``localhost:9000`` parses with the host in ``path``. Default the scheme so both spellings
    resolve to the same host.
    """
    parsed = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    return (parsed.hostname or "").strip()


def _is_loopback(host: str) -> bool:
    if host.lower() == _LOOPBACK_NAME:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal (a hostname). No DNS resolution — a non-localhost name is accepted.
        return False


def validate_guest_routable_endpoint() -> None:
    """Reject a loopback/localhost ``KDIVE_S3_ENDPOINT_URL`` before it reaches a remote guest.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when ``KDIVE_S3_ENDPOINT_URL`` resolves to a
            loopback host (``localhost``, ``127.0.0.0/8``, ``::1``). ``details`` carries
            ``env_var``, ``next_action`` (a literal remediation naming the var), and the offending
            ``configured_endpoint``. A blank/unset endpoint is a no-op here (owned by
            ``object_store_from_env``).
    """
    endpoint = config.get(S3_ENDPOINT_URL)
    if not endpoint:
        return
    if not _is_loopback(_host_of(endpoint)):
        return
    raise CategorizedError(
        f"{S3_ENDPOINT_URL.name}={endpoint!r} is a loopback/localhost address; a remote "
        "guest cannot reach the object store there. It must be a control-plane address "
        "routable from the remote guest network.",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "env_var": S3_ENDPOINT_URL.name,
            "configured_endpoint": endpoint,
            "next_action": (
                f"set {S3_ENDPOINT_URL.name} to a control-plane address routable from the "
                "remote guest network"
            ),
        },
    )


__all__ = ["validate_guest_routable_endpoint"]
