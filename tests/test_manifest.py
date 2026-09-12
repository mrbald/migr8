"""Manifest loading and unit path rules (spec Sections 3.1, 3.2, 4.2)."""

from __future__ import annotations

import os
import sys

import pytest

import support
from migr8.errors import ManifestError, UnitError
from migr8.manifest import Language, Mode, load
from migr8.paths import scan_unit
from migr8.staging import capture_in_place


def _ok_entry(**overrides):
    entry = {"id": "m", "path": "u", "language": "sql", "mode": "atomic", "entry": "up.sql"}
    entry.update(overrides)
    return entry


@pytest.fixture
def project(tmp_path):
    support.unit(tmp_path, "u", {"up.sql": "SELECT 1;\n"})
    return tmp_path


def test_loads_a_minimal_manifest(project):
    path = support.manifest(project, [_ok_entry()])
    manifest = load(path)
    assert [m.id for m in manifest.migrations] == ["m"]
    entry = manifest.migrations[0]
    assert entry.position == 1
    assert entry.language is Language.SQL
    assert entry.mode is Mode.ATOMIC
    assert entry.required == ()


def test_array_order_is_execution_order(tmp_path):
    for name in ("zzz", "aaa", "mmm"):
        support.unit(tmp_path, name, {"up.sql": "SELECT 1;\n"})
    path = support.manifest(tmp_path, [
        _ok_entry(id="third", path="zzz"),
        _ok_entry(id="first", path="aaa"),
        _ok_entry(id="second", path="mmm"),
    ])
    manifest = load(path)
    assert [m.id for m in manifest.migrations] == ["third", "first", "second"]
    assert [m.position for m in manifest.migrations] == [1, 2, 3]


def test_missing_manifest_version_is_an_error(project):
    path = support.manifest(project, [_ok_entry()], version=None)
    with pytest.raises(ManifestError, match="missing manifest_version"):
        load(path)


def test_unsupported_manifest_version_is_an_error(project):
    path = support.manifest(project, [_ok_entry()], version=2)
    with pytest.raises(ManifestError, match="unsupported manifest_version 2"):
        load(path)


def test_unknown_top_level_key_is_an_error(project):
    path = support.manifest(project, [_ok_entry()], extra='flavour = "vanilla"')
    with pytest.raises(ManifestError, match="unknown top-level keys: flavour"):
        load(path)


def test_unknown_migration_key_is_an_error(project):
    path = support.manifest(project, [_ok_entry(comment="hi")])
    with pytest.raises(ManifestError, match="unknown keys: comment"):
        load(path)


@pytest.mark.parametrize("missing", ["id", "path", "language", "mode", "entry"])
def test_every_required_field_is_required(project, missing):
    entry = _ok_entry()
    del entry[missing]
    path = support.manifest(project, [entry])
    with pytest.raises(ManifestError, match=f"missing required keys: {missing}"):
        load(path)


def test_duplicate_ids_are_rejected(tmp_path):
    support.unit(tmp_path, "u1", {"up.sql": "SELECT 1;\n"})
    support.unit(tmp_path, "u2", {"up.sql": "SELECT 2;\n"})
    path = support.manifest(tmp_path, [
        _ok_entry(id="same", path="u1"), _ok_entry(id="same", path="u2"),
    ])
    with pytest.raises(ManifestError, match="duplicate migration id 'same'"):
        load(path)


def test_duplicate_unit_paths_are_rejected(project):
    path = support.manifest(project, [
        _ok_entry(id="a", path="u"), _ok_entry(id="b", path="u"),
    ])
    with pytest.raises(ManifestError, match="same unit directory"):
        load(path)


def test_nested_units_are_rejected(tmp_path):
    support.unit(tmp_path, "outer", {"up.sql": "SELECT 1;\n"})
    support.unit(tmp_path, "outer/inner", {"up.sql": "SELECT 2;\n"})
    path = support.manifest(tmp_path, [
        _ok_entry(id="a", path="outer"), _ok_entry(id="b", path="outer/inner"),
    ])
    with pytest.raises(ManifestError, match="contained in the unit"):
        load(path)


@pytest.mark.parametrize("bad_id", [
    "", "_leading", ".leading", "-leading", "has space", "has/slash", "é",
    "x" * 201, "tab\there",
])
def test_invalid_ids_are_rejected(project, bad_id):
    path = support.manifest(project, [_ok_entry(id=bad_id)])
    with pytest.raises(ManifestError):
        load(path)


def test_ids_are_case_sensitive(tmp_path):
    support.unit(tmp_path, "u1", {"up.sql": "SELECT 1;\n"})
    support.unit(tmp_path, "u2", {"up.sql": "SELECT 2;\n"})
    path = support.manifest(tmp_path, [
        _ok_entry(id="Orders", path="u1"), _ok_entry(id="orders", path="u2"),
    ])
    assert [m.id for m in load(path).migrations] == ["Orders", "orders"]


@pytest.mark.parametrize("bad_path", ["/abs", "../escape", "u/../u", "./u", "u//x"])
def test_invalid_unit_paths_are_rejected(project, bad_path):
    path = support.manifest(project, [_ok_entry(path=bad_path)])
    with pytest.raises(ManifestError):
        load(path)


def test_unit_path_must_not_be_the_manifest_directory(project):
    path = support.manifest(project, [_ok_entry(path=".")])
    with pytest.raises(ManifestError):
        load(path)


def test_missing_entry_file_is_an_error(project):
    path = support.manifest(project, [_ok_entry(entry="absent.sql")])
    with pytest.raises(ManifestError, match="is not an existing regular file"):
        load(path)


def test_entry_that_is_a_directory_is_an_error(tmp_path):
    support.unit(tmp_path, "u", {"sub/file.sql": "SELECT 1;\n"})
    path = support.manifest(tmp_path, [_ok_entry(entry="sub")])
    with pytest.raises(ManifestError):
        load(path)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_symlinked_unit_root_is_rejected(tmp_path):
    real = support.unit(tmp_path, "real", {"up.sql": "SELECT 1;\n"})
    os.symlink(real, tmp_path / "link")
    path = support.manifest(tmp_path, [_ok_entry(path="link")])
    with pytest.raises(ManifestError, match="symlink"):
        load(path)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_symlink_inside_a_unit_is_rejected(tmp_path):
    unit_dir = support.unit(tmp_path, "u", {"up.sql": "SELECT 1;\n"})
    os.symlink(unit_dir / "up.sql", unit_dir / "alias.sql")
    with pytest.raises(UnitError, match="symlink"):
        scan_unit(unit_dir, migration_id="m")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fifos")
def test_special_file_inside_a_unit_is_rejected(tmp_path):
    unit_dir = support.unit(tmp_path, "u", {"up.sql": "SELECT 1;\n"})
    os.mkfifo(unit_dir / "pipe")
    with pytest.raises(UnitError, match="special file"):
        scan_unit(unit_dir, migration_id="m")


@pytest.mark.parametrize("artefact", [
    "__pycache__/mod.cpython-314.pyc", "helper.pyc", ".pytest_cache/v/cache/lastfailed",
    ".mypy_cache/3.14/x.json",
])
def test_bytecode_and_tool_caches_inside_units_are_errors(tmp_path, artefact):
    unit_dir = support.unit(tmp_path, "u", {"up.sql": "SELECT 1;\n", artefact: "x"})
    with pytest.raises(UnitError, match="forbidden"):
        scan_unit(unit_dir, migration_id="m")


def test_empty_unit_is_an_error(tmp_path):
    (tmp_path / "u").mkdir()
    with pytest.raises(UnitError, match="contains no files"):
        scan_unit(tmp_path / "u", migration_id="m")


def test_case_folding_collision_within_a_unit_is_rejected(tmp_path, monkeypatch):
    from migr8 import paths

    # Build the colliding list directly: a case-insensitive filesystem cannot
    # hold both names, which is exactly why the pair is refused.
    with pytest.raises(UnitError, match="case-folding collision"):
        paths.check_no_case_collisions(["Helper.sql", "helper.sql"])


def test_duplicate_required_object_is_rejected(project):
    path = support.manifest(project, [_ok_entry(require_valid=[
        {"name": "PKG", "type": "PACKAGE"}, {"name": "PKG", "type": "PACKAGE"},
    ])])
    with pytest.raises(ManifestError, match="duplicate required object"):
        load(path)


def test_required_objects_are_stored_in_canonical_order(project):
    path = support.manifest(project, [_ok_entry(require_valid=[
        {"name": "V", "type": "VIEW"},
        {"name": "B", "type": "PACKAGE BODY"},
        {"name": "A", "type": "PACKAGE"},
    ])])
    required = load(path).migrations[0].required
    assert [(o.type, o.name) for o in required] == [
        ("PACKAGE", "A"), ("PACKAGE BODY", "B"), ("VIEW", "V"),
    ]


def test_required_object_needs_both_fields(project):
    path = support.manifest(project, [{
        "id": "m", "path": "u", "language": "sql", "mode": "atomic", "entry": "up.sql",
        "require_valid": [{"name": "PKG", "type": ""}],
    }])
    with pytest.raises(ManifestError, match="empty name or type"):
        load(path)


@pytest.mark.parametrize("field,value", [("language", "plsql"), ("mode", "transactional")])
def test_unsupported_language_and_mode_are_rejected(project, field, value):
    path = support.manifest(project, [_ok_entry(**{field: value})])
    with pytest.raises(ManifestError, match=f"{field} must be one of"):
        load(path)


def test_unit_location_is_not_part_of_the_fingerprint(tmp_path):
    """Moving a unit does not change its fingerprint (spec Section 4.1)."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    support.unit(first, "alpha", {"up.sql": "SELECT 1;\n", "extra/data.csv": "a,b\n"})
    support.unit(second, "renamed-dir", {"up.sql": "SELECT 1;\n", "extra/data.csv": "a,b\n"})
    a = capture_in_place(load(support.manifest(first, [_ok_entry(path="alpha")])))
    b = capture_in_place(load(support.manifest(second, [_ok_entry(path="renamed-dir")])))
    assert a.units[0].fingerprint == b.units[0].fingerprint


def test_manifest_position_is_not_part_of_the_fingerprint(tmp_path):
    support.unit(tmp_path, "a", {"up.sql": "SELECT 1;\n"})
    support.unit(tmp_path, "b", {"up.sql": "SELECT 1;\n"})
    capture = capture_in_place(load(support.manifest(tmp_path, [
        _ok_entry(id="first", path="a"), _ok_entry(id="second", path="b"),
    ])))
    assert capture.units[0].fingerprint == capture.units[1].fingerprint
    assert capture.units[0].id != capture.units[1].id
