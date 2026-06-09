"""Remote-libvirt stub planes fail fast with a typed error (ADR-0076)."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from uuid import uuid4

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction
from kdive.providers.ports import SystemHandle, TransportHandle
from kdive.providers.remote_libvirt import planes

# The stubs raise before touching any argument, so typed sentinels suffice.
_SYSTEM = cast(SystemHandle, None)
_TRANSPORT = cast(TransportHandle, None)
_ACTION = cast(PowerAction, None)
_METHOD = cast(CaptureMethod, None)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: planes.UnimplementedConnector().open_transport(_SYSTEM, "gdbstub"),
        lambda: planes.UnimplementedConnector().close_transport(_TRANSPORT),
        lambda: planes.UnimplementedController().power("dom", _ACTION),
        lambda: planes.UnimplementedController().force_crash("dom"),
        lambda: planes.UnimplementedRetriever().capture(uuid4(), _METHOD),
        lambda: planes.UnimplementedRetriever().run_crash_postmortem(
            vmcore_ref="v", debuginfo_ref="d", expected_build_id="b", commands=[]
        ),
        lambda: planes.UnimplementedIntrospector().from_vmcore(
            vmcore_ref="v", debuginfo_ref="d", expected_build_id="b"
        ),
        lambda: planes.UnimplementedIntrospector().introspect_live(
            transport_handle="t", helper="h"
        ),
    ],
)
def test_every_stub_plane_raises_missing_dependency(invoke: Callable[[], object]) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        invoke()
    assert excinfo.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert "remote-libvirt" in str(excinfo.value)
