"""accounting.* handler tests — `estimate` called directly with an injected pool.

`estimate` is a pure read-side price of a hypothetical selector + window: no
allocation, ledger, or budget row is touched. It requires `viewer` on the project and
fails closed on a missing coefficient or a malformed selector/window.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.mcp.auth import AuthError, RequestContext
from kdive.mcp.tool_payloads import EstimateRequestPayload
from kdive.mcp.tools.accounting.estimate import estimate
from kdive.security.authz.rbac import AuthorizationError, Role


def _ctx(
    role: Role | None = Role.VIEWER, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


def _request(
    *, vcpus: int = 1, memory_gb: int = 1, window: object = 1, cost_class: str = "local"
) -> EstimateRequestPayload:
    return EstimateRequestPayload.model_validate(
        {"vcpus": vcpus, "memory_gb": memory_gb, "window": window, "cost_class": cost_class}
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def test_estimate_returns_rate_window_product_and_breakdown(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(
                pool, _ctx(), project="proj", request=_request(vcpus=2, memory_gb=4, window=3)
            )
        # coeff(local)=1.0; rate = 1.0*(1.0*2 + 0.25*4) = 3.0; estimate = 3.0*3 = 9.0000.
        assert resp.status == "ok"
        assert resp.error_category is None
        assert resp.suggested_next_actions == ["allocations.request"]
        assert resp.data["project"] == "proj"
        assert resp.data["cost_class"] == "local"
        assert resp.data["estimate_kcu"] == "9.0000"
        assert resp.data["rate_kcu_per_hr"] == "3.0000"
        assert resp.data["breakdown_vcpu_kcu_per_hr"] == "2.0000"
        assert resp.data["breakdown_memory_kcu_per_hr"] == "1.0000"

    asyncio.run(_run())


def test_estimate_fractional_window(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(
                pool, _ctx(), project="proj", request=_request(memory_gb=0, window="1.5")
            )
        # rate = 1.0; estimate = 1.0 * 1.5 = 1.5.
        assert resp.data["estimate_kcu"] == "1.5000"
        assert resp.data["rate_kcu_per_hr"] == "1.0000"

    asyncio.run(_run())


def test_estimate_never_negative_on_zero_memory(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(pool, _ctx(), project="proj", request=_request(memory_gb=0))
        assert resp.status == "ok"
        assert resp.data["estimate_kcu"] == "1.0000"

    asyncio.run(_run())


def test_estimate_unknown_cost_class_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(
                pool, _ctx(), project="proj", request=_request(cost_class="cloud")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["accounting.estimate"]

    asyncio.run(_run())


def test_estimate_payload_model_rejects_malformed_shape() -> None:
    with pytest.raises(ValidationError):
        EstimateRequestPayload.model_validate({"vcpus": 1, "memory_gb": 1, "unexpected": "value"})


def test_estimate_negative_memory_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(pool, _ctx(), project="proj", request=_request(memory_gb=-1))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_estimate_zero_vcpus_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(pool, _ctx(), project="proj", request=_request(vcpus=0))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_estimate_zero_window_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(pool, _ctx(), project="proj", request=_request(window=0))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_estimate_negative_window_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(pool, _ctx(), project="proj", request=_request(window=-3))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_estimate_unparseable_window_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(
                pool, _ctx(), project="proj", request=_request(window="not-a-number")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_estimate_nan_window_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(pool, _ctx(), project="proj", request=_request(window="NaN"))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_estimate_huge_finite_window_fails_closed(migrated_url: str) -> None:
    # validate_window has no upper bound (clamping is admission-only), so a viewer can
    # price an arbitrarily large finite window. The quantize boundary must map the
    # value-too-large case to configuration_error, never an unhandled exception.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(
                pool, _ctx(), project="proj", request=_request(memory_gb=0, window="1e30")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_estimate_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            try:
                await estimate(pool, _ctx(role=None), project="proj", request=_request())
                raise AssertionError("expected AuthorizationError")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_estimate_foreign_project_refused(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            other = _ctx(projects=("elsewhere",), role=Role.VIEWER)
            try:
                await estimate(pool, other, project="proj", request=_request())
                raise AssertionError("expected AuthError")
            except AuthError:
                pass

    asyncio.run(_run())


def test_estimate_operator_may_call(migrated_url: str) -> None:
    # viewer is the floor; a higher role satisfies it.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await estimate(
                pool, _ctx(role=Role.OPERATOR), project="proj", request=_request()
            )
        assert resp.status == "ok"

    asyncio.run(_run())
