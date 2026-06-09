"""qemu+tls:// connection lifecycle for the remote-libvirt provider (ADR-0077).

Mutual TLS, fail-closed: the worker presents a client cert and verifies the libvirtd
server cert against the configured CA + hostname; ``no_verify`` is forbidden. Because
``SecretBackend.resolve`` returns strings while libvirt's TLS client reads on-disk
files, each op materializes the resolved cert/key/CA into a private per-op pkipath
(dir ``0700``, files ``0600``), points the URI at it via ``?pkipath=``, and deletes
the directory on every exit path. The on-disk lifetime, not text masking, is the
control for the private key (it is consumed by the TLS layer and never echoed).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secrets import SecretBackend

if TYPE_CHECKING:
    from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs

_REQUIRED_SCHEME = "qemu+tls"
# libvirt resolves exactly these names inside a pkipath.
_CLIENT_CERT_NAME = "clientcert.pem"
_CLIENT_KEY_NAME = "clientkey.pem"  # pragma: allowlist secret - libvirt file name
_CA_CERT_NAME = "cacert.pem"
_log = logging.getLogger(__name__)


class _LibvirtConn(Protocol):
    """The slice of a libvirt connection the remote provider uses (duck-typed seam)."""

    def getInfo(self) -> list[Any]: ...  # noqa: N802 - libvirt binding name
    def getCapabilities(self) -> str: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenConnection = Callable[[str], _LibvirtConn]


def validate_remote_uri(uri: str) -> None:
    """Reject any URI that would weaken mutual TLS (fail-closed, ADR-0077).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-``qemu+tls`` scheme, a
            ``no_verify`` parameter (server-cert verification must stay on), or an
            operator-set ``pkipath`` (each op composes its own private pkipath).
    """
    parsed = urlsplit(uri)
    if parsed.scheme != _REQUIRED_SCHEME:
        raise CategorizedError(
            f"remote-libvirt URI {uri!r} must use the qemu+tls:// scheme",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "no_verify" in query:
        raise CategorizedError(
            "no_verify is forbidden on the remote-libvirt URI: server-cert "
            "verification is mandatory (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if "pkipath" in query:
        raise CategorizedError(
            "pkipath must not be set on the remote-libvirt URI: each op "
            "materializes its own private pkipath (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def compose_pkipath_uri(uri: str, pkipath: Path) -> str:
    """Append ``pkipath=<dir>`` to the URI query, preserving existing parameters.

    ``/`` stays literal (``safe='/'``) so the value is valid whether or not the
    consumer percent-unescapes query parameters; ``mkdtemp`` paths contain no
    reserved characters that would need escaping.
    """
    parsed = urlsplit(uri)
    pki_param = f"pkipath={quote(str(pkipath), safe='/')}"
    query = f"{parsed.query}&{pki_param}" if parsed.query else pki_param
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _write_private(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)


@contextmanager
def materialized_pkipath(
    secret_backend: SecretBackend, refs: TlsCertRefs, *, base_dir: Path | None = None
) -> Iterator[Path]:
    """Resolve the cert/key/CA refs into a private per-op pkipath; delete on every exit.

    The directory is ``0700`` (``mkdtemp``), files ``0600``. Resolution goes through
    ``SecretBackend`` so each value registers into the redaction registry before use
    (defense-in-depth, ADR-0027); the primary control is the bounded on-disk lifetime.
    A cleanup failure is logged at error level rather than raised, so it never
    replaces the op's typed in-flight error; the residue is bounded by worker-local
    storage (ADR-0077).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when a ref cannot be resolved
            (missing file, escapes the secrets root, oversized) — mapped from
            ``PathSafetyError`` so the platform's typed taxonomy holds; the message
            names the refs, never the values.
    """
    try:
        client_cert = secret_backend.resolve(refs.client_cert_ref)
        client_key = secret_backend.resolve(refs.client_key_ref)
        ca_cert = secret_backend.resolve(refs.ca_cert_ref)
    except PathSafetyError as exc:
        raise CategorizedError(
            f"remote-libvirt TLS secret refs {refs.client_cert_ref!r}/"
            f"{refs.client_key_ref!r}/{refs.ca_cert_ref!r} could not be resolved: {exc}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc
    pkipath = Path(tempfile.mkdtemp(prefix="kdive-remote-pki-", dir=base_dir))
    try:
        _write_private(pkipath / _CLIENT_CERT_NAME, client_cert)
        _write_private(pkipath / _CLIENT_KEY_NAME, client_key)
        _write_private(pkipath / _CA_CERT_NAME, ca_cert)
        yield pkipath
    finally:
        try:
            shutil.rmtree(pkipath)
        except OSError:
            _log.exception(
                "failed to delete pkipath %s; private key material may remain on disk",
                pkipath,
            )


@contextmanager
def remote_connection(
    config: RemoteLibvirtConfig,
    secret_backend: SecretBackend,
    *,
    open_connection: OpenConnection,
    pki_base_dir: Path | None = None,
) -> Iterator[_LibvirtConn]:
    """Open a mutual-TLS libvirt connection for one op; close it and the pkipath after.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unsafe URI or unresolvable
            secret refs, or ``TRANSPORT_FAILURE`` when the TLS connect fails.
    """
    validate_remote_uri(config.uri)
    with materialized_pkipath(secret_backend, config.cert_refs, base_dir=pki_base_dir) as pki:
        uri = compose_pkipath_uri(config.uri, pki)
        try:
            conn = open_connection(uri)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                f"qemu+tls connect to {config.uri!r} failed",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"uri": config.uri},
            ) from exc
        try:
            yield conn
        finally:
            conn.close()


def open_libvirt(uri: str) -> _LibvirtConn:
    """The production opener (live-host path; unit tests inject a fake)."""
    # libvirt ships no type stubs; ty infers `virConnect`, which does not structurally
    # match the protocol. Duck-typed at the seam — scoped ignore, as in
    # local_libvirt/discovery.py.
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]
