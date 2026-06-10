"""TransportResetter port + NullResetter default (#216, ADR-0086)."""

from __future__ import annotations

import asyncio

from kdive.providers.transport_reset import NullResetter, TransportResetter


def test_null_resetter_satisfies_the_port() -> None:
    assert isinstance(NullResetter(), TransportResetter)


def test_null_resetter_reset_is_a_noop() -> None:
    async def scenario() -> None:
        # No transport touched; returns None for every input shape.
        assert (
            await NullResetter().reset(
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
                domain_name="kdive-sys",
            )
            is None
        )
        assert (
            await NullResetter().reset(
                transport="drgn-live", transport_handle=None, domain_name=None
            )
            is None
        )

    asyncio.run(scenario())
