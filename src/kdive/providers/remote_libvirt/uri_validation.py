"""Remote-libvirt URI validation shared by config and transport."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from kdive.domain.errors import CategorizedError, ErrorCategory

_REQUIRED_SCHEME = "qemu+tls"


def _query_param_names(query: str) -> set[str]:
    """The lowercased parameter names of a URI query, split the way libvirt splits it.

    libvirt's URI parser treats both ``&`` and ``;`` as separators, percent-unescapes
    parameter names, and matches them case-insensitively (``STRCASEEQ`` in the remote
    driver), so the fail-closed check must see every spelling libvirt would honor.
    """
    names: set[str] = set()
    for chunk in query.replace(";", "&").split("&"):
        if not chunk:
            continue
        names.add(unquote(chunk.split("=", 1)[0]).lower())
    return names


def validate_remote_uri(uri: str) -> None:
    """Reject any URI that would weaken mutual TLS (fail-closed, ADR-0077).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-``qemu+tls`` scheme, a
            ``no_verify`` parameter (server-cert verification must stay on), or an
            operator-set ``pkipath`` (each op composes its own private pkipath) —
            in any casing or ``;``-separated spelling libvirt would accept.
    """
    parsed = urlsplit(uri)
    if parsed.scheme != _REQUIRED_SCHEME:
        raise CategorizedError(
            f"remote-libvirt URI {uri!r} must use the qemu+tls:// scheme",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    names = _query_param_names(parsed.query)
    if "no_verify" in names:
        raise CategorizedError(
            "no_verify is forbidden on the remote-libvirt URI: server-cert "
            "verification is mandatory (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if "pkipath" in names:
        raise CategorizedError(
            "pkipath must not be set on the remote-libvirt URI: each op "
            "materializes its own private pkipath (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
