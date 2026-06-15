"""The bundled demo OIDC default claim set must round-trip the RBAC parser (#369).

The Helm demo issuer (`deploy/helm/kdive/templates/demo/oidc.yaml`) mints every token
with the claim set defaulted in `deploy/helm/kdive/values.yaml` under `demo.oidc.claims`.
That default exists to make a stock demo deploy RBAC-testable, so it must parse cleanly
through `context_from_claims` (no `AuthError`) and resolve to a full grant. This test
pins that contract: a typo in the shipped default (e.g. `platform-admin` for
`platform_admin`) fails here, fast, instead of only at deploy time.

Keep `_DEFAULT_DEMO_CLAIMS` byte-for-byte in sync with `demo.oidc.claims` in
`deploy/helm/kdive/values.yaml`; the helm render suite (`tests/helm/test_helm_render.py`)
guards the other half (that the template actually emits these values).
"""

from __future__ import annotations

import pytest

from kdive.security.authz.context import context_from_claims
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import PlatformRole, Role

# Mirror of `demo.oidc.claims` in deploy/helm/kdive/values.yaml (the chart default).
_DEFAULT_DEMO_CLAIMS: dict[str, object] = {
    "sub": "kdive-demo",
    "aud": ["kdive"],
    "projects": ["demo"],
    "roles": {"demo": "admin"},
    "platform_roles": ["platform_admin", "platform_operator", "platform_auditor"],
}


def test_default_demo_claims_resolve_to_a_full_rbac_grant() -> None:
    ctx = context_from_claims(_DEFAULT_DEMO_CLAIMS)

    assert ctx.principal == "kdive-demo"
    assert ctx.projects == ("demo",)
    assert ctx.roles == {"demo": Role.ADMIN}
    assert ctx.platform_roles == frozenset(
        {
            PlatformRole.PLATFORM_ADMIN,
            PlatformRole.PLATFORM_OPERATOR,
            PlatformRole.PLATFORM_AUDITOR,
        }
    )


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
def test_role_scoped_variant_claims_resolve_to_project_role_without_platform(
    role: Role,
) -> None:
    # The chart's per-role variants (oidc.yaml, ADR-0108 §4) mint a token carrying only the
    # project role and NO platform roles, so `--role viewer|operator` reaches a denial. Pin
    # that the variant claim shape parses to exactly that grant: an empty platform-role set is
    # what makes a require_platform_role check deny.
    variant: dict[str, object] = {
        "sub": f"kdive-demo-{role.value}",
        "aud": ["kdive"],
        "projects": ["demo"],
        "roles": {"demo": role.value},
    }
    ctx = context_from_claims(variant)

    assert ctx.roles == {"demo": role}
    assert ctx.platform_roles == frozenset()


def test_demo_claims_with_unknown_role_fail_closed() -> None:
    # The chart default must never trip this fail-closed path; the negative test documents
    # that an unknown role string is rejected, not silently dropped.
    bad = dict(_DEFAULT_DEMO_CLAIMS)
    bad["roles"] = {"demo": "superuser"}
    with pytest.raises(AuthError, match="unknown role"):
        context_from_claims(bad)


def test_demo_claims_with_unknown_platform_role_fail_closed() -> None:
    bad = dict(_DEFAULT_DEMO_CLAIMS)
    bad["platform_roles"] = ["platform_admin", "platform-typo"]
    with pytest.raises(AuthError, match="unknown role"):
        context_from_claims(bad)
