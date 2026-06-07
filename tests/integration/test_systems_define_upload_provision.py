"""End-to-end reachability of the rootfs-upload lane (define -> upload -> provision, #111).

DB/tool-lane reachability under a fake provider: it proves the upload-kind profile flows
through systems.define, artifacts.create_system_upload, systems.provision, and the provision
handler's _commit_uploaded_rootfs. It does NOT boot — staging the object to the libvirt
disk is the install/boot spec's concern (ADR-0048 §7).
"""

from __future__ import annotations

import asyncio

import pytest
from psycopg.rows import dict_row

from kdive.domain.models import Sensitivity
from kdive.mcp.tools import artifacts as artifacts_tools
from kdive.mcp.tools import systems as systems_tools
from kdive.planes import systems as systems_handlers
from kdive.store.objectstore import ObjectStore, artifact_key
from tests.mcp.test_systems_tools import (
    _ctx,
    _define,
    _enqueue_provision,
    _FakeProvisioning,
    _granted_allocation,
    _pool,
    _upload_profile,
)


def test_define_upload_provision_reaches_ready_with_committed_rootfs(
    migrated_url: str, minio_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(systems_handlers, "object_store_from_env", lambda: minio_store)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)

            # 1. define -> DEFINED, allocation granted->active
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id

            # 2. create_system_upload opens the window (persists the manifest, mints a PUT)
            uploads = await artifacts_tools.create_system_upload(
                pool,
                _ctx(),
                system_id=sys_id,
                artifacts=[{"name": "rootfs", "sha256": "sha256:x", "size_bytes": 18}],
                store=minio_store,
            )
            assert uploads[0].status == "upload_ready"
            assert uploads[0].suggested_next_actions == ["systems.provision"]

            # 3. the agent PUTs the qcow2 (staged directly into the store for the test)
            minio_store.put_artifact(
                "local",
                "systems",
                sys_id,
                "rootfs",
                data=b"rootfs-image-bytes",
                sensitivity=Sensitivity.SENSITIVE,
                retention_class="rootfs",
            )

            # 4. provision admits the DEFINED System (no profile re-passed)
            resp = await systems_tools.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=None
            )
            assert resp.status == "queued"

            # 5. the provision handler drives provisioning -> ready and commits the rootfs
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, _FakeProvisioning())

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT object_key, owner_kind, sensitivity FROM artifacts WHERE owner_id = %s",
                    (sys_id,),
                )
                art_rows = await cur.fetchall()
        assert sys_row is not None and sys_row["state"] == "ready"
        assert len(art_rows) == 1
        assert art_rows[0]["object_key"] == artifact_key("local", "systems", sys_id, "rootfs")
        assert art_rows[0]["owner_kind"] == "systems"
        assert art_rows[0]["sensitivity"] == "sensitive"

    asyncio.run(_run())
