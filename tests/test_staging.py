"""Staging and capture behaviour (spec Section 4.3)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import support

from migr8.errors import Exit, UnitError
from migr8.manifest import ID_RE, load
from migr8.manifest import load as load_manifest
from migr8.staging import capture_in_place, cleanup, stage


def _project(tmp_path: Path) -> Path:
    support.unit(
        tmp_path,
        "u",
        {
            "up.sql": "SELECT 1 FROM DUAL;\n",
            "data/rows.csv": "id,region\n1,EU\n",
            "helper.py": "VALUE = 1\n",
        },
    )
    return support.manifest(
        tmp_path,
        [
            {
                "id": "m",
                "path": "u",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            }
        ],
    )


def test_staging_copies_every_unit_file(tmp_path):
    capture = stage(load(_project(tmp_path)))
    try:
        unit = capture.units[0]
        assert sorted(unit.relpaths) == ["data/rows.csv", "helper.py", "up.sql"]
        for rel in unit.relpaths:
            assert (unit.source_dir / rel).is_file()
        assert unit.staged
    finally:
        cleanup(capture.staging_root)


def test_staged_fingerprint_matches_the_in_place_fingerprint(tmp_path):
    manifest = load(_project(tmp_path))
    in_place = capture_in_place(manifest)
    staged = stage(manifest)
    try:
        assert staged.units[0].fingerprint == in_place.units[0].fingerprint
        assert not in_place.units[0].staged
    finally:
        cleanup(staged.staging_root)


def test_working_tree_edits_after_staging_do_not_change_the_attempt(tmp_path):
    manifest_path = _project(tmp_path)
    manifest = load(manifest_path)
    staged = stage(manifest)
    try:
        before = staged.units[0].fingerprint
        (tmp_path / "u" / "up.sql").write_text("SELECT 2 FROM DUAL;\n")
        assert (staged.units[0].source_dir / "up.sql").read_text() == "SELECT 1 FROM DUAL;\n"
        assert staged.units[0].fingerprint == before
        # A fresh capture of the working tree sees the edit.
        assert capture_in_place(load(manifest_path)).units[0].fingerprint != before
    finally:
        cleanup(staged.staging_root)


def test_staged_files_are_read_only_as_a_guardrail(tmp_path):
    capture = stage(load(_project(tmp_path)))
    try:
        target = capture.units[0].source_dir / "up.sql"
        mode = stat.S_IMODE(target.stat().st_mode)
        assert not mode & stat.S_IWUSR
    finally:
        cleanup(capture.staging_root)


def test_cleanup_removes_the_staging_root(tmp_path):
    capture = stage(load(_project(tmp_path)))
    root = capture.staging_root
    assert root is not None and root.is_dir()
    cleanup(root)
    assert not root.exists()


def test_each_staged_unit_gets_an_injective_directory(tmp_path):
    """Ids differing only in a separator must not share a staged directory."""
    support.unit(tmp_path, "u1", {"up.sql": "SELECT 1;\n"})
    support.unit(tmp_path, "u2", {"up.sql": "SELECT 2;\n"})
    manifest_path = support.manifest(
        tmp_path,
        [
            {"id": "a-b", "path": "u1", "language": "sql", "mode": "atomic", "entry": "up.sql"},
            {"id": "a_b", "path": "u2", "language": "sql", "mode": "atomic", "entry": "up.sql"},
        ],
    )
    capture = stage(load(manifest_path))
    try:
        first, second = capture.units
        assert first.source_dir != second.source_dir
        assert first.source_dir.name != second.source_dir.name
    finally:
        cleanup(capture.staging_root)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_staging_refuses_a_symlink_inside_a_unit(tmp_path):
    unit_dir = support.unit(tmp_path, "u", {"up.sql": "SELECT 1;\n"})
    os.symlink(unit_dir / "up.sql", unit_dir / "alias.sql")
    manifest_path = support.manifest(
        tmp_path,
        [
            {
                "id": "m",
                "path": "u",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            }
        ],
    )
    with pytest.raises(UnitError, match="symlink"):
        stage(load(manifest_path))


def test_failed_staging_leaves_no_directory_behind(tmp_path, monkeypatch):
    manifest = load(_project(tmp_path))
    roots: list[Path] = []
    real_mkdtemp = __import__("tempfile").mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        roots.append(Path(path))
        return path

    monkeypatch.setattr("migr8.staging.tempfile.mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(
        "migr8.staging._fingerprint_unit",
        lambda *a, **k: (_ for _ in ()).throw(UnitError("boom")),
    )
    with pytest.raises(UnitError, match="boom"):
        stage(manifest)
    assert roots and not roots[0].exists()


# --- identifier length (spec Section 3.1) --------------------------------------------

MAX_ID = "a" * 200


def test_the_longest_permitted_id_stages_and_runs(tmp_path):
    """Section 3.1 allows a 200-character id, so staging must accept one.

    The staged directory name is internal. Hex-encoding the id there doubled a
    length the manifest already allows to reach 200, producing a path component
    no filesystem this runs on accepts.
    """
    assert ID_RE.match(MAX_ID)
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t (id INTEGER);\n"})
    manifest_path = support.manifest(
        tmp_path,
        [{"id": MAX_ID, "path": "m1", "language": "sql", "mode": "restartable", "entry": "up.sql"}],
    )
    config = support.sqlite_config(tmp_path)

    capture = stage(load_manifest(manifest_path))
    try:
        staged = capture.units[0].source_dir
        assert staged.is_dir()
        assert len(staged.name) <= 255
        assert (staged / "up.sql").is_file()
    finally:
        cleanup(capture.staging_root)

    # And the whole run works, not just the parse and the copy.
    assert support.migrate(config, manifest_path) == Exit.OK
    assert [row[1] for row in support.history(tmp_path / "build" / "probe.db")] == [MAX_ID]


def test_long_ids_stay_in_their_own_staged_directories(tmp_path):
    """Two ids sharing a 199-character prefix must not share a directory."""
    first, second = MAX_ID[:-1] + "b", MAX_ID[:-1] + "c"
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t1 (id INTEGER);\n"})
    support.unit(tmp_path, "m2", {"up.sql": "CREATE TABLE t2 (id INTEGER);\n"})
    manifest_path = support.manifest(
        tmp_path,
        [
            {
                "id": first,
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
            {
                "id": second,
                "path": "m2",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
        ],
    )
    capture = stage(load_manifest(manifest_path))
    try:
        directories = [unit.source_dir for unit in capture.units]
        assert directories[0] != directories[1]
        assert (directories[0] / "up.sql").read_text() != (directories[1] / "up.sql").read_text()
    finally:
        cleanup(capture.staging_root)


def test_a_long_python_id_imports_and_executes(tmp_path):
    """The unit package name is derived from the id too; it is not a path component."""
    support.unit(tmp_path, "m1", {"up.py": 'def migrate(ctx):\n    ctx.log("ran")\n'})
    manifest_path = support.manifest(
        tmp_path,
        [
            {
                "id": MAX_ID,
                "path": "m1",
                "language": "python",
                "mode": "restartable",
                "entry": "up.py",
            }
        ],
    )
    config = support.sqlite_config(tmp_path)
    assert support.migrate(config, manifest_path) == Exit.OK
