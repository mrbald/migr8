"""Checks every command performs, before and around execution.

Neither function belongs to the engine. ``validate`` and ``status`` run both
without executing anything, and keeping them here is what lets the read-only
path avoid importing the execution engine and, through it, the Python unit
loader. The read-only commands import nothing that can import migration code.
"""

from __future__ import annotations

from .adapters.base import Adapter
from .errors import MetadataDamagedError, PythonSyntaxError, UsageError
from .manifest import Language, Mode
from .model import LAYOUT_VERSION, Capture, CapturedUnit
from .sqltext import StatementKind, normalize

#: What :func:`preflight` checked, named for the report that says so.
CHECKS = (
    "manifest order, identity, paths and fingerprints",
    "supported language and mode combinations",
    "required-object declarations",
    "SQL statement admission for every SQL unit",
    "Python compilation of every source in every Python unit",
)


def preflight(capture: Capture, adapter: Adapter) -> None:
    """Everything checkable without a connection or migration code (spec Section 11.1 step 1).

    Covers supported language/mode combinations, required-object declarations,
    the lexical rules for every SQL unit and the compilation of every Python
    unit.  Applying these to the whole manifest rather than only the pending
    suffix is deliberate: the spec requires structural errors to surface before
    execution, and a published unit's bytes never change.
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
        else:
            compile_python(unit)


def compile_python(unit: CapturedUnit) -> None:
    """Compile every Python source in one unit without importing or running it.

    A syntax error is a property of the source, so it is found here rather than
    at the import that happens after the migration has been admitted and marked
    ACTIVE.  :func:`compile` neither executes the module nor writes bytecode;
    ``dont_inherit`` keeps this process's own compiler flags out of the result.

    Helper modules are compiled too: the entry point imports them, so a helper
    that does not parse fails the migration just as the entry would.  What this
    cannot say is whether the code is correct, only that Python can read it.
    """
    for relpath in unit.relpaths:
        if not relpath.endswith(".py"):
            continue
        path = unit.source_dir / relpath
        source = path.read_bytes()
        try:
            compile(source, relpath, "exec", dont_inherit=True)
        except SyntaxError as exc:
            location = f"{relpath}:{exc.lineno or 0}"
            if exc.offset:
                location += f":{exc.offset}"
            raise PythonSyntaxError(
                f"migration {unit.id!r} does not compile: {location}: {exc.msg}",
                migration_id=unit.id,
                phase="preflight",
            ) from exc
        except ValueError as exc:
            # A null byte or a source the compiler refuses outright.
            raise PythonSyntaxError(
                f"migration {unit.id!r} has a {relpath} the Python compiler refuses: {exc}",
                migration_id=unit.id,
                phase="preflight",
            ) from exc


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


__all__ = ["CHECKS", "compile_python", "preflight", "verify_bindings"]
