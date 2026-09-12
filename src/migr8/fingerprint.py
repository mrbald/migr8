"""The ``fp1`` canonical fingerprint encoding (spec Section 4.2).

Once used, this encoding is immutable.  A later encoding gets a new prefix; an
unsupported prefix fails validation rather than being rewritten.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

FORMAT_PREFIX = "fp1"
CANONICAL_HEADER = b"migr8-fingerprint/1\n"

FINGERPRINT_RE = re.compile(r"^fp1:[0-9a-f]{64}$")


def is_supported_fingerprint(value: str) -> bool:
    """True when ``value`` is a syntactically valid fingerprint of a known format."""
    return bool(FINGERPRINT_RE.match(value))


def _u32be(value: int) -> bytes:
    return value.to_bytes(4, "big", signed=False)


def _u64be(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=False)


def field(name: str, value: bytes) -> bytes:
    """``u32be(len(name)) || name || u64be(len(value)) || value``."""
    raw_name = name.encode("utf-8")
    return _u32be(len(raw_name)) + raw_name + _u64be(len(value)) + value


def encode_required(required: list[tuple[str, str]]) -> bytes:
    """Encode the canonical ``require_valid`` set.

    ``required`` is a list of ``(type, name)`` pairs.  Sorting is by UTF-8
    bytes of type then name, so reordering the manifest list does not change
    the fingerprint while adding, removing or editing an entry does.
    """
    ordered = sorted(required, key=lambda pair: (pair[0].encode("utf-8"), pair[1].encode("utf-8")))
    out = [_u64be(len(ordered))]
    for obj_type, obj_name in ordered:
        out.append(field("type", obj_type.encode("utf-8")))
        out.append(field("name", obj_name.encode("utf-8")))
    return b"".join(out)


@dataclass(frozen=True, slots=True)
class FingerprintInput:
    """Everything the fingerprint covers, already validated and ordered."""

    language: str
    mode: str
    entry: str
    required: list[tuple[str, str]]
    #: ``(unit-relative path, exact bytes)``, in any order; sorted here.
    files: list[tuple[str, bytes]]


def canonical_bytes(data: FingerprintInput) -> bytes:
    """Produce the exact byte string that is hashed."""
    ordered_files = sorted(data.files, key=lambda pair: pair[0].encode("utf-8"))
    out = [
        CANONICAL_HEADER,
        field("language", data.language.encode("utf-8")),
        field("mode", data.mode.encode("utf-8")),
        field("entry", data.entry.encode("utf-8")),
        field("require_valid", encode_required(data.required)),
        field("count", _u64be(len(ordered_files))),
    ]
    for relpath, payload in ordered_files:
        out.append(field("path", relpath.encode("utf-8")))
        out.append(field("data", payload))
    return b"".join(out)


def compute(data: FingerprintInput) -> str:
    """Return ``fp1:<lowercase sha256 hex>`` for ``data``."""
    digest = hashlib.sha256(canonical_bytes(data)).hexdigest()
    return f"{FORMAT_PREFIX}:{digest}"
