"""Checks every command performs, before and around execution.

Neither function belongs to the engine. ``validate`` and ``status`` run both
without executing anything, and keeping them here is what lets the read-only
path avoid importing the execution engine and, through it, the Python unit
loader. The read-only commands import nothing that can import migration code.
"""

from __future__ import annotations

from .adapters.base import Adapter
from .errors import MetadataDamagedError, UsageError
from .manifest import Language, Mode
from .model import LAYOUT_VERSION, Capture
from .sqltext import StatementKind, normalize


def preflight(capture: Capture, adapter: Adapter) -> None:
    """Everything checkable without a connection or migration code (spec Section 11.1 step 1).

    Covers supported language/mode combinations, required-object declarations and
    the lexical rules for every SQL unit.  Applying the lexical check to the
    whole manifest rather than only the pending suffix is deliberate: the spec
    requires structural errors to surface before execution, and a published
    unit's bytes never change.
    """
    for unit in capture.units:
        adapter.admit_combination(unit.language, unit.mode)
        adapter.admit_required_objects(unit.definition.required)
        if unit.language is Language.SQL:
            entry_path = unit.source_dir / unit.definition.entry
            text = entry_path.read_text(encoding="utf-8")
            statement = normalize(text)
            if unit.mode is Mode.ATOMIC:
                adapter.admit_statement(statement, mode=Mode.ATOMIC, in_batch=False)
            elif statement.kind is not StatementKind.PLSQL_BLOCK:
                adapter.admit_ddl(statement)


def verify_bindings(adapter: Adapter, plan_meta) -> None:
    """Confirm the recorded namespace, lock and adapter bindings (spec Section 8.1)."""
    if plan_meta is None:
        raise MetadataDamagedError("initialization marker is missing after initialization")
    expected_namespace = adapter.normalized_namespace()
    expected_lock = adapter.lock_binding()
    if plan_meta.layout_version != LAYOUT_VERSION:
        raise MetadataDamagedError(
            f"metadata layout_version {plan_meta.layout_version} is not supported "
            f"(expected {LAYOUT_VERSION})"
        )
    if plan_meta.adapter != adapter.name:
        raise UsageError(
            f"namespace was initialized by adapter {plan_meta.adapter!r} but this run uses "
            f"{adapter.name!r}"
        )
    if plan_meta.target_namespace != expected_namespace:
        raise UsageError(
            f"namespace binding mismatch: metadata records {plan_meta.target_namespace!r}, "
            f"this run targets {expected_namespace!r}"
        )
    if plan_meta.lock_binding != expected_lock:
        raise UsageError(
            f"lock binding mismatch: metadata records {plan_meta.lock_binding!r}, this run is "
            f"configured for {expected_lock!r}. The runner does not switch locks mid-run."
        )


__all__ = ["preflight", "verify_bindings"]
