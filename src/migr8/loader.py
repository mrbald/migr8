"""Importing a staged Python migration unit (spec Section 9.1).

Each unit is registered as its own private package whose ``__path__`` is the
staged directory, so sibling modules import relatively and nothing is added to
the global ``sys.path``.  The package name hex-encodes the migration id, which
makes it injective: ``a-b`` and ``a_b`` cannot collide.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from .errors import UnitError

PACKAGE_PREFIX = "_migr8_unit_"


def package_name(migration_id: str) -> str:
    return PACKAGE_PREFIX + migration_id.encode("utf-8").hex()


def unload_namespace(root: str) -> None:
    """Remove the unit package *root* and everything under it from ``sys.modules``.

    The match is the package name or one of its dot-prefixed descendants.  A
    bare prefix match would be wrong in both directions: hex-encoded ids nest,
    so ``_migr8_unit_61`` is a prefix of the unrelated ``_migr8_unit_6162``.
    """
    prefix = root + "."
    for name in [n for n in sys.modules if n == root or n.startswith(prefix)]:
        sys.modules.pop(name, None)


class LoadedUnit:
    """A loaded unit's entry callable, plus what to discard afterwards."""

    __slots__ = ("_root", "migrate")

    def __init__(self, migrate: Callable[[object], object], root: str) -> None:
        self.migrate = migrate
        self._root = root

    def unload(self) -> None:
        """Discard the unit's whole private namespace (spec Section 9.1).

        The modules are enumerated now rather than when the unit was loaded.  A
        helper imported inside ``migrate`` is not in ``sys.modules`` yet at load
        time, and if it survives it shadows its own edited source on the next
        run in the same process -- which is exactly what a recovery run reloads.
        """
        unload_namespace(self._root)


def _deferred_kind(value: object) -> str | None:
    """Name what kind of deferred work *value* is, or ``None`` if it is not any."""
    if inspect.iscoroutine(value):
        return "coroutine"
    if inspect.isasyncgen(value):
        return "async generator"
    if inspect.isgenerator(value):
        return "generator"
    return None


def _deferred_function_kind(func: object) -> str | None:
    if inspect.iscoroutinefunction(func):
        return "an async def function"
    if inspect.isasyncgenfunction(func):
        return "an async generator function"
    if inspect.isgeneratorfunction(func):
        return "a generator function"
    return None


def check_result(result: object, *, migration_id: str) -> None:
    """Enforce ``migrate(ctx) -> None`` on what the call actually returned.

    Checking the result as well as the declared function catches a synchronous
    wrapper that hands deferred work back.  The engine runs no event loop and
    consumes no generator, so a coroutine or generator returned from here would
    be recorded as a successful migration whose body never ran.
    """
    kind = _deferred_kind(result)
    if kind is not None:
        # Release it without running its body; an unclosed coroutine also warns.
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise UnitError(
            f"migration {migration_id!r} returned {kind} work from migrate(ctx) instead of "
            "doing it. The engine runs no event loop and consumes no generator, so the "
            "body never ran and no history row may be written.",
            migration_id=migration_id,
        )
    if result is not None:
        raise UnitError(
            f"migration {migration_id!r} returned {type(result).__name__} from migrate(ctx); "
            "the entry point returns None",
            migration_id=migration_id,
        )


def load_entry(*, migration_id: str, staged_dir: Path, entry: str) -> LoadedUnit:
    """Import ``entry`` from ``staged_dir`` and return its ``migrate`` callable."""
    # Never write bytecode next to a staged unit: it would be an unfingerprinted
    # file inside the unit and a forbidden artefact on the next scan.
    sys.dont_write_bytecode = True

    if not entry.endswith(".py"):
        raise UnitError(
            f"migration {migration_id!r} declares language python but entry {entry!r} "
            "is not a .py file"
        )

    root = package_name(migration_id)
    if root in sys.modules:
        raise UnitError(
            f"module namespace {root} is already registered; a previous run of "
            f"{migration_id!r} was not unloaded"
        )

    try:
        _make_namespace(root, staged_dir)

        parts = entry[: -len(".py")].split("/")
        current_dir = staged_dir
        current_name = root
        for part in parts[:-1]:
            current_dir = current_dir / part
            current_name = f"{current_name}.{part}"
            if current_name not in sys.modules:
                _make_namespace(current_name, current_dir)

        module_name = f"{current_name}.{parts[-1]}"
        entry_path = staged_dir / entry
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise UnitError(f"cannot load entry {entry!r} of migration {migration_id!r}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = current_name
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        target = getattr(module, "migrate", None)
        if target is None or not callable(target):
            raise UnitError(
                f"migration {migration_id!r} entry {entry!r} does not define a callable "
                "migrate(ctx)",
                migration_id=migration_id,
            )
        deferred = _deferred_function_kind(target)
        if deferred is not None:
            raise UnitError(
                f"migration {migration_id!r} entry {entry!r} defines migrate(ctx) as "
                f"{deferred}; the entry point is a synchronous callable returning None "
                "(spec Section 9.1)",
                migration_id=migration_id,
            )
        return LoadedUnit(target, root)
    except BaseException:
        unload_namespace(root)
        raise


def _make_namespace(name: str, directory: Path) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None, is_package=True)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(directory)]
    sys.modules[name] = module
    return module
