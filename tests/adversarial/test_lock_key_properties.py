"""Property-based tests for the advisory-lock key derivation (no DB).

`db/locks.py::_lock_key` folds an unbounded `(scope, key)` space onto a signed
64-bit Postgres advisory-lock key. The safety claim is that a collision only ever
*over*-serializes (two unrelated keys share a lock — safe) and never silently
mis-types one scope as another for the same UUID. These properties pin determinism,
the 64-bit signed range, and cross-scope separation.
"""

from __future__ import annotations

from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from kdive.db.locks import LockScope, _lock_key

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_uuids = st.uuids()
_scopes = st.sampled_from(list(LockScope))


@given(scope=_scopes, key=_uuids)
def test_lock_key_is_deterministic_and_in_signed_64bit_range(scope: LockScope, key: UUID) -> None:
    first = _lock_key(scope, key)
    assert first == _lock_key(scope, key)  # pure function of its inputs
    assert _INT64_MIN <= first <= _INT64_MAX  # fits pg_advisory_xact_lock(bigint)


@given(a=_scopes, b=_scopes, key=_uuids)
def test_distinct_scopes_separate_the_same_uuid(a: LockScope, b: LockScope, key: UUID) -> None:
    # Same object id under two different scopes must (overwhelmingly) get different
    # lock keys, so an ALLOCATION lock never accidentally serializes a SYSTEM op.
    if a != b:
        assert _lock_key(a, key) != _lock_key(b, key)


@given(scope=_scopes, a=_uuids, b=_uuids)
def test_distinct_keys_in_one_scope_separate(scope: LockScope, a: UUID, b: UUID) -> None:
    if a != b:
        assert _lock_key(scope, a) != _lock_key(scope, b)
