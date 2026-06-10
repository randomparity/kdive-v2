"""Unit tests for the shared host-reachability policy (ADR-0083 §2)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.hostpolicy import allow_acl_remote, require_loopback


def test_require_loopback_accepts_loopback_literal():
    require_loopback("127.0.0.1")  # no raise
    require_loopback("::1")  # no raise


@pytest.mark.parametrize("host", ["10.0.0.5", "example.com", "", "0.0.0.0"])
def test_require_loopback_rejects_non_loopback(host):
    with pytest.raises(CategorizedError) as exc:
        require_loopback(host)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_allow_acl_remote_accepts_routable_literal_and_hostname():
    allow_acl_remote("10.0.0.5")  # no raise — operator-trusted gdb_addr
    allow_acl_remote("gdbhost.internal")  # no raise — hostname is allowed for remote


@pytest.mark.parametrize("host", ["", "   ", "has space", "a\tb"])
def test_allow_acl_remote_rejects_empty_or_malformed(host):
    with pytest.raises(CategorizedError) as exc:
        allow_acl_remote(host)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
