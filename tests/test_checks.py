"""Preflight: what is checked before a connection or any migration code.

These run against the real :func:`migr8.checks.preflight`, with the smallest
adapter that can answer its questions, so a refusal an adapter declares is shown
to reach the caller rather than being assumed to.
"""

from __future__ import annotations

import pytest
import support

from migr8.checks import preflight
from migr8.errors import UnsupportedCapabilityError
from migr8.manifest import Language, Mode
from migr8.manifest import load as load_manifest
from migr8.staging import capture_in_place


class _StubAdapter:
    """Answers only what preflight asks, and records what it was asked."""

    name = "stub"

    def __init__(self, *, refuse: tuple[Language, Mode] | None = None) -> None:
        self.refuse = refuse
        self.seen: list[tuple[Language, Mode]] = []

    def admit_combination(self, language: Language, mode: Mode) -> None:
        self.seen.append((language, mode))
        if self.refuse == (language, mode):
            raise UnsupportedCapabilityError(
                f"the stub adapter does not support {language.value} in {mode.value} mode"
            )

    def admit_required_objects(self, required) -> None:
        return

    def admit_statement(self, statement, *, mode, in_batch) -> None:
        return

    def admit_ddl(self, statement) -> None:
        return


def _capture(root):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE t (id INTEGER);\n"})
    support.unit(root, "m2", {"up.sql": "INSERT INTO t (id) VALUES (1);\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {
                "id": "insert-t",
                "path": "m2",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
        ],
    )
    return capture_in_place(load_manifest(manifest))


def test_every_declared_combination_is_offered_to_the_adapter(tmp_path):
    adapter = _StubAdapter()
    preflight(_capture(tmp_path), adapter)
    assert adapter.seen == [
        (Language.SQL, Mode.RESTARTABLE),
        (Language.SQL, Mode.ATOMIC),
    ]


def test_a_combination_the_adapter_refuses_fails_preflight(tmp_path):
    """The refusal is the adapter's to make; preflight must not swallow it."""
    adapter = _StubAdapter(refuse=(Language.SQL, Mode.ATOMIC))
    with pytest.raises(UnsupportedCapabilityError, match="does not support sql in atomic"):
        preflight(_capture(tmp_path), adapter)
