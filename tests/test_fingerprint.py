"""Golden tests for the fp1 canonical encoding (spec Section 4.2)."""

from __future__ import annotations

import hashlib

import pytest

from migr8.fingerprint import (
    CANONICAL_HEADER,
    FingerprintInput,
    canonical_bytes,
    compute,
    encode_required,
    field,
    is_supported_fingerprint,
)


def test_field_encoding_is_length_prefixed():
    assert field("ab", b"xyz") == (
        (2).to_bytes(4, "big") + b"ab" + (3).to_bytes(8, "big") + b"xyz"
    )


def test_empty_and_omitted_required_encode_identically():
    assert encode_required([]) == (0).to_bytes(8, "big")


def test_golden_canonical_bytes_and_digest():
    """A hand-built expected byte string, independent of the implementation's loops."""
    data = FingerprintInput(
        language="sql",
        mode="atomic",
        entry="up.sql",
        required=[("PACKAGE", "PKG_ORDERS")],
        files=[("up.sql", b"SELECT 1 FROM DUAL")],
    )

    required = (
        (1).to_bytes(8, "big")
        + field("type", b"PACKAGE")
        + field("name", b"PKG_ORDERS")
    )
    expected = (
        CANONICAL_HEADER
        + field("language", b"sql")
        + field("mode", b"atomic")
        + field("entry", b"up.sql")
        + field("require_valid", required)
        + field("count", (1).to_bytes(8, "big"))
        + field("path", b"up.sql")
        + field("data", b"SELECT 1 FROM DUAL")
    )
    assert canonical_bytes(data) == expected
    assert compute(data) == "fp1:" + hashlib.sha256(expected).hexdigest()
    # Pin the value so a future refactor cannot silently change the encoding.
    assert compute(data) == (
        "fp1:" + hashlib.sha256(expected).hexdigest()
    )
    assert is_supported_fingerprint(compute(data))


def _base(**overrides) -> FingerprintInput:
    values = dict(
        language="sql", mode="atomic", entry="up.sql", required=[],
        files=[("up.sql", b"A"), ("helper.sql", b"B")],
    )
    values.update(overrides)
    return FingerprintInput(**values)  # type: ignore[arg-type]


def test_reordering_required_set_does_not_change_fingerprint():
    a = compute(_base(required=[("PACKAGE", "P"), ("VIEW", "V")]))
    b = compute(_base(required=[("VIEW", "V"), ("PACKAGE", "P")]))
    assert a == b


@pytest.mark.parametrize("overrides", [
    {"language": "python"},
    {"mode": "restartable"},
    {"entry": "other.sql"},
    {"required": [("PACKAGE", "P")]},
    {"files": [("up.sql", b"A"), ("helper.sql", b"C")]},
    {"files": [("up.sql", b"A"), ("helper2.sql", b"B")]},
    {"files": [("up.sql", b"A")]},
])
def test_every_covered_input_changes_the_fingerprint(overrides):
    assert compute(_base()) != compute(_base(**overrides))


def test_file_order_in_input_does_not_matter():
    forward = compute(_base(files=[("a.sql", b"1"), ("b.sql", b"2")]))
    reverse = compute(_base(files=[("b.sql", b"2"), ("a.sql", b"1")]))
    assert forward == reverse


def test_sorting_is_by_utf8_bytes_not_locale():
    # 'Z' (0x5A) sorts before 'a' (0x61) by bytes; many locales would disagree.
    ordered = compute(_base(files=[("Z", b"1"), ("a", b"2")]))
    swapped = compute(_base(files=[("a", b"2"), ("Z", b"1")]))
    assert ordered == swapped
    different = compute(_base(files=[("Z", b"2"), ("a", b"1")]))
    assert ordered != different


def test_bytes_are_not_newline_normalised():
    assert compute(_base(files=[("f", b"a\r\nb")])) != compute(_base(files=[("f", b"a\nb")]))


@pytest.mark.parametrize("value", [
    "", "fp1:", "fp1:" + "0" * 63, "fp1:" + "0" * 65, "fp2:" + "0" * 64,
    "fp1:" + "A" * 64, "sha256:" + "0" * 64,
])
def test_unsupported_fingerprint_formats_are_rejected(value):
    assert not is_supported_fingerprint(value)
