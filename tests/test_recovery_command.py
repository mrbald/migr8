"""The recovery command is rendered once, at the CLI boundary (spec Section 10.1).

The engine and the read-only commands report the id of the amended ACTIVE
migration.  The CLI turns it into a command: the short form for the default
layout, and a form that carries the resolved ``--config`` and ``--manifest``
when the operator passed either, so it runs from any directory.
"""

from __future__ import annotations

import json
import shlex

import pytest
import support

from migr8.errors import Exit

pytestmark = pytest.mark.sqlite

FAILS = "def migrate(ctx):\n    raise RuntimeError('stop')\n"
ENTRY = {
    "id": "py",
    "path": "m1",
    "language": "python",
    "mode": "restartable",
    "entry": "migration.py",
}


def active_migration(root, *, config_name="migr8.toml", manifest_name="manifest.toml"):
    """A project whose only unit failed and stayed ACTIVE, then had its source amended."""
    config = support.sqlite_config(root, name=config_name)
    manifest = support.manifest(root, [ENTRY], name=manifest_name)
    support.unit(root, "m1", {"migration.py": FAILS})
    return config, manifest


def amend(root):
    support.unit(root, "m1", {"migration.py": FAILS + "# amended\n"})


def recovery_command(capsys, argv, cwd) -> str:
    code = support.run_cli([*argv, "--json"], cwd=cwd)
    assert code == Exit.VALIDATION
    report = json.loads(capsys.readouterr().out)
    assert report["recovery_id"] == "py"
    return report["recovery_command"]


@pytest.mark.parametrize("command", ["migrate", "status"])
def test_the_default_layout_gets_the_short_form(tmp_path, capsys, command):
    active_migration(tmp_path)
    assert support.run_cli(["migrate"], cwd=tmp_path) == Exit.MIGRATION_FAILED
    amend(tmp_path)
    capsys.readouterr()

    assert recovery_command(capsys, [command], tmp_path) == "migr8 migrate --recover py"


@pytest.mark.parametrize("command", ["migrate", "status"])
def test_nondefault_paths_with_a_space_are_carried_into_the_command(tmp_path, capsys, command):
    root = tmp_path / "release candidate"
    config, manifest = active_migration(
        root, config_name="prod config.toml", manifest_name="prod manifest.toml"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    flags = ["--config", str(config), "--manifest", str(manifest)]
    assert support.run_cli(["migrate", *flags], cwd=elsewhere) == Exit.MIGRATION_FAILED
    amend(root)
    capsys.readouterr()

    rendered = recovery_command(capsys, [command, *flags], elsewhere)

    words = shlex.split(rendered)
    assert words[:4] == ["migr8", "migrate", "--recover", "py"]
    assert words[words.index("--config") + 1] == str(config.resolve())
    assert words[words.index("--manifest") + 1] == str(manifest.resolve())
    assert len(words) == 8


def test_one_explicit_path_still_carries_both(tmp_path, capsys):
    """The default for the other path is relative to the current directory."""
    config, manifest = active_migration(tmp_path, manifest_name="other.toml")
    assert (
        support.run_cli(["migrate", "--manifest", str(manifest)], cwd=tmp_path)
        == Exit.MIGRATION_FAILED
    )
    amend(tmp_path)
    capsys.readouterr()

    rendered = recovery_command(capsys, ["migrate", "--manifest", str(manifest)], tmp_path)

    words = shlex.split(rendered)
    assert words[words.index("--config") + 1] == str(config.resolve())
    assert words[words.index("--manifest") + 1] == str(manifest.resolve())
