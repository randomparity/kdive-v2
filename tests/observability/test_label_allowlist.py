"""Identifier-label allowlist (ADR-0090 §4): metric/span labels are a fixed key set.

Raw tenant / principal / project / secret-ref identifiers must not travel as
free-cardinality labels — both a metrics-cost footgun and (per ADR-0089) recon data.
Identifiers travel as log attributes, not metric/trace labels.
"""

from __future__ import annotations

from kdive.observability import labels


def test_allowed_keys_pass_through() -> None:
    attrs = {key: "v" for key in labels.ALLOWED_LABEL_KEYS}
    filtered = labels.filter_label_keys(attrs)
    assert set(filtered) == set(attrs)


def test_identifier_keys_are_dropped() -> None:
    attrs = {
        "principal": "alice",
        "tenant": "acme",
        "project": "proj-1",
        "secret_ref": "file:///x",  # pragma: allowlist secret - identifier key, not a value
        "object_id": "sys-7",
    }
    filtered = labels.filter_label_keys(attrs)
    assert filtered == {}


def test_mixed_attrs_keep_only_allowlisted() -> None:
    allowed_key = next(iter(labels.ALLOWED_LABEL_KEYS))
    attrs = {allowed_key: "ok", "principal": "alice"}
    filtered = labels.filter_label_keys(attrs)
    assert filtered == {allowed_key: "ok"}


def test_allowlist_excludes_known_identifier_keys() -> None:
    for ident in ("principal", "tenant", "project", "object_id", "secret_ref"):
        assert ident not in labels.ALLOWED_LABEL_KEYS
