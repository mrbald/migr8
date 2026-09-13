"""Importing a staged Python migration unit (spec Section 9.1).

Each unit is registered as its own private package whose ``__path__`` is the
staged directory, so sibling modules import relatively and nothing is added to
the global ``sys.path``.  The package name hex-encodes the migration id, which
makes it injective: ``a-b`` and ``a_b`` cannot collide.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from .errors import UnitError

PACKAGE_PREFIX = "_migr8_unit_"


def package_name(migration_id: str) -> str:
    return PACKAGE_PREFIX + migration_id.encode("utf-8").hex()


class LoadedUnit:
    """A loaded unit's entry callable, plus what to discard afterwards."""

    __slots__ = ("migrate", "_names")

    def __init__(self, migrate: Callable[[object], object], names: list[str]) -> None:
        self.migrate = migrate
        self._names = names

    def unload(self) -> None:
        """Remove this unit's modules so a later run in the same process is fresh."""
        for name in self._names:
            sys.modules.pop(name, None)


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

    created: list[str] = []
    try:
        _make_namespace(root, staged_dir)
        created.append(root)

        parts = entry[: -len(".py")].split("/")
        current_dir = staged_dir
        current_name = root
        for part in parts[:-1]:
            current_dir = current_dir / part
            current_name = f"{current_name}.{part}"
            if current_name not in sys.modules:
                _make_namespace(current_name, current_dir)
                created.append(current_name)

        module_name = f"{current_name}.{parts[-1]}"
        entry_path = staged_dir / entry
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise UnitError(f"cannot load entry {entry!r} of migration {migration_id!r}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = current_name
        sys.modules[module_name] = module
        created.append(module_name)
        spec.loader.exec_module(module)

        target = getattr(module, "migrate", None)
        if target is None or not callable(target):
            raise UnitError(
                f"migration {migration_id!r} entry {entry!r} does not define a callable "
                "migrate(ctx)"
            )
        # Capture every module the unit imported under its own namespace so the
        # whole namespace can be discarded afterwards.
        names = sorted(
            name for name in sys.modules
            if name == root or name.startswith(root + ".")
        )
        return LoadedUnit(target, names)
    except BaseException:
        for name in sorted(
            {*created, *(n for n in list(sys.modules) if n.startswith(root))},
            reverse=True,
        ):
            sys.modules.pop(name, None)
        raise


def _make_namespace(name: str, directory: Path) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None, is_package=True)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(directory)]
    sys.modules[name] = module
    return module
