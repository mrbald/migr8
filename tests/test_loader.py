"""Importing staged Python units (spec Section 9.1)."""

from __future__ import annotations

import sys

import pytest

import support
from migr8.errors import UnitError
from migr8.loader import PACKAGE_PREFIX, load_entry, package_name


def test_package_name_is_injective_for_separator_variants():
    assert package_name("a-b") != package_name("a_b")
    assert package_name("a-b").startswith(PACKAGE_PREFIX)


def test_entry_and_relative_sibling_import(tmp_path):
    unit = support.unit(tmp_path, "u", {
        "migration.py": "from .helper import VALUE\n\n\ndef migrate(ctx):\n    return VALUE\n",
        "helper.py": "VALUE = 41\n",
    })
    loaded = load_entry(migration_id="m", staged_dir=unit, entry="migration.py")
    try:
        assert loaded.migrate(None) == 41
        assert package_name("m") in sys.modules
    finally:
        loaded.unload()
    assert package_name("m") not in sys.modules


def test_ids_differing_only_by_separator_load_distinct_helpers(tmp_path):
    """Spec Section 14.2 group 1: ``a-b`` and ``a_b`` must not collide."""
    first = support.unit(tmp_path, "one", {
        "migration.py": "from .helper import VALUE\n\n\ndef migrate(ctx):\n    return VALUE\n",
        "helper.py": "VALUE = 'dash'\n",
    })
    second = support.unit(tmp_path, "two", {
        "migration.py": "from .helper import VALUE\n\n\ndef migrate(ctx):\n    return VALUE\n",
        "helper.py": "VALUE = 'underscore'\n",
    })
    a = load_entry(migration_id="a-b", staged_dir=first, entry="migration.py")
    b = load_entry(migration_id="a_b", staged_dir=second, entry="migration.py")
    try:
        assert a.migrate(None) == "dash"
        assert b.migrate(None) == "underscore"
    finally:
        a.unload()
        b.unload()


def test_unit_is_not_added_to_global_sys_path(tmp_path):
    unit = support.unit(tmp_path, "u", {"migration.py": "def migrate(ctx):\n    pass\n"})
    before = list(sys.path)
    loaded = load_entry(migration_id="m", staged_dir=unit, entry="migration.py")
    try:
        assert sys.path == before
    finally:
        loaded.unload()


def test_entry_in_a_subdirectory_is_supported(tmp_path):
    unit = support.unit(tmp_path, "u", {
        "pkg/migration.py": "def migrate(ctx):\n    return 'sub'\n",
    })
    loaded = load_entry(migration_id="m", staged_dir=unit, entry="pkg/migration.py")
    try:
        assert loaded.migrate(None) == "sub"
    finally:
        loaded.unload()


def test_missing_migrate_callable_is_an_error(tmp_path):
    unit = support.unit(tmp_path, "u", {"migration.py": "VALUE = 1\n"})
    with pytest.raises(UnitError, match="does not define a callable migrate"):
        load_entry(migration_id="m", staged_dir=unit, entry="migration.py")
    assert package_name("m") not in sys.modules


def test_non_python_entry_for_a_python_migration_is_an_error(tmp_path):
    unit = support.unit(tmp_path, "u", {"up.sql": "SELECT 1;\n"})
    with pytest.raises(UnitError, match="is not a .py file"):
        load_entry(migration_id="m", staged_dir=unit, entry="up.sql")


def test_import_error_cleans_up_the_namespace(tmp_path):
    unit = support.unit(tmp_path, "u", {"migration.py": "raise RuntimeError('boom')\n"})
    with pytest.raises(RuntimeError, match="boom"):
        load_entry(migration_id="m", staged_dir=unit, entry="migration.py")
    assert not [n for n in sys.modules if n.startswith(package_name("m"))]


def test_double_load_without_unload_is_refused(tmp_path):
    unit = support.unit(tmp_path, "u", {"migration.py": "def migrate(ctx):\n    pass\n"})
    loaded = load_entry(migration_id="m", staged_dir=unit, entry="migration.py")
    try:
        with pytest.raises(UnitError, match="already registered"):
            load_entry(migration_id="m", staged_dir=unit, entry="migration.py")
    finally:
        loaded.unload()


def test_bytecode_writing_is_disabled(tmp_path):
    unit = support.unit(tmp_path, "u", {
        "migration.py": "from .helper import VALUE\n\n\ndef migrate(ctx):\n    return VALUE\n",
        "helper.py": "VALUE = 1\n",
    })
    loaded = load_entry(migration_id="m", staged_dir=unit, entry="migration.py")
    try:
        assert sys.dont_write_bytecode
        assert not (unit / "__pycache__").exists()
    finally:
        loaded.unload()
