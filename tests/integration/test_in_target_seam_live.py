"""Operator-run e2e for the in-target artifact channel (issue #202, ADR-0078).

``live_vm``-gated and preflighted to a clean skip: it needs an operator-provided libvirt
domain that is running, has a connected qemu-guest-agent, and can reach the object store
with ``/usr/bin/curl`` in-guest, plus the ``KDIVE_S3_*`` object-store env. CI deselects
``live_vm`` (`just test` runs ``-m "not live_vm and not live_stack"``), so this is safe on
any host; an operator runs it with ``just test-live``.

It proves both of issue #202's acceptance bullets in one pass against the **real** seam:

1. a guest-agent ``exec`` runs in the fixtured guest and returns output — the guest pulls
   exactly the published bytes through the minted presigned GET URL;
2. the minted presigned URL is masked in the persisted transcript (re-fetched from the
   store) and in the returned snippet.

Required env: ``KDIVE_SEAM_DOMAIN`` (a running, agent-ready domain name), ``KDIVE_SEAM_URI``
(the libvirt connect URI for it), and ``KDIVE_S3_ENDPOINT_URL`` + ``KDIVE_S3_BUCKET``.
"""

from __future__ import annotations

import os
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest
from kdive.providers.remote_libvirt.guest.agent import GuestAgentExec, qemu_agent_command
from kdive.providers.remote_libvirt.guest.artifact_channel import InTargetArtifactChannel
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

pytestmark = pytest.mark.live_vm

_DOMAIN_ENV = "KDIVE_SEAM_DOMAIN"
_URI_ENV = "KDIVE_SEAM_URI"
_GUEST_CURL = "/usr/bin/curl"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; the in-target seam e2e needs an operator guest")
    return value


def test_guest_agent_pull_returns_output_and_masks_the_capability_url() -> None:
    domain_name = _require_env(_DOMAIN_ENV)
    uri = _require_env(_URI_ENV)
    if not os.environ.get("KDIVE_S3_ENDPOINT_URL") or not os.environ.get("KDIVE_S3_BUCKET"):
        pytest.skip("KDIVE_S3_* object-store env is not set")

    store = object_store_from_env()
    payload = f"published-kernel-{uuid4().hex}".encode()
    stored = store.put_artifact(
        ArtifactWriteRequest(
            tenant="remote-libvirt-e2e",
            owner_kind="runs",
            owner_id=uuid4().hex,
            name="kernel",
            data=payload,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        )
    )
    capability_url = store.presign_get(stored.key, expires_in=300)

    conn = libvirt.open(uri)
    try:
        domain = conn.lookupByName(domain_name)
        channel = InTargetArtifactChannel(
            registry=SecretRegistry(),
            agent_exec=GuestAgentExec(
                agent_command=qemu_agent_command,
                allowed_programs=frozenset({_GUEST_CURL}),
            ),
            store_factory=object_store_from_env,
            scope=object(),
        )
        output = channel.exec_with_capability(
            domain,
            capability_url=capability_url,
            argv=[_GUEST_CURL, "-fsS", capability_url],
            owner_kind="systems",
            owner_id=uuid4().hex,
        )
    finally:
        conn.close()

    # The guest-agent exec returned output: the guest fetched exactly the published bytes.
    assert output.result.exit_status == 0
    assert output.result.stdout == payload

    # The capability URL is masked in the returned snippet and in the persisted transcript.
    assert capability_url not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet
    persisted = store.get_artifact(output.artifact.key, output.artifact.etag)
    assert capability_url.encode() not in persisted.data
    assert REDACTION.encode() in persisted.data
    assert persisted.sensitivity is Sensitivity.REDACTED
