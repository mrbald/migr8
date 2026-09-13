"""CLI surface, configuration handling and exit codes (spec Sections 11.3, 11.4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import support

from migr8.errors import Exit

ENTRY = Path(__file__).resolve().parents[1] / "migr8"


def cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRY), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def project(tmp_path):
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"})
    support.manifest(
        tmp_path,
        [
            {
                "id": "create-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    support.sqlite_config(tmp_path, db_path=tmp_path / "build" / "probe.db")
    return tmp_path


def test_help_and_version(project):
    assert cli(["--help"], project).returncode == 0
    version = cli(["--version"], project)
    assert version.returncode == 0
    assert "migr8" in version.stdout


def test_a_subcommand_is_required(project):
    assert cli([], project).returncode == 2  # argparse usage error


def test_only_three_commands_exist(project):
    for forbidden in ("clean", "undo", "baseline", "repair", "unlock"):
        result = cli([forbidden], project)
        assert result.returncode != 0
        assert "invalid choice" in result.stderr


def test_default_paths_are_used(project):
    assert cli(["status"], project).returncode == Exit.NOT_INITIALIZED
    assert cli(["migrate"], project).returncode == Exit.OK
    assert cli(["status"], project).returncode == Exit.OK
    assert cli(["validate"], project).returncode == Exit.OK


def test_explicit_paths_are_resolved_once(project, tmp_path):
    elsewhere = tmp_path.parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    result = cli(
        [
            "migrate",
            "--config",
            str(project / "migr8.toml"),
            "--manifest",
            str(project / "manifest.toml"),
        ],
        elsewhere,
    )
    assert result.returncode == Exit.OK


def test_missing_config_is_a_usage_error(project):
    result = cli(["status", "--config", "nope.toml"], project)
    assert result.returncode == Exit.USAGE
    assert "does not exist" in result.stderr


def test_missing_manifest_is_a_usage_error(project):
    result = cli(["status", "--manifest", "nope.toml"], project)
    assert result.returncode == Exit.USAGE


def test_unknown_adapter_is_a_usage_error(project):
    (project / "bad.toml").write_text(
        '[database]\nadapter = "mysql"\n\n[lock]\nprovider = "x"\ntimeout_seconds = 1\n'
    )
    result = cli(["status", "--config", "bad.toml"], project)
    assert result.returncode == Exit.USAGE
    assert "supported adapters" in result.stderr


def test_unknown_config_table_is_a_usage_error(project):
    (project / "bad.toml").write_text(
        '[database]\nadapter = "sqlite-probe"\npath = "x.db"\n\n'
        '[lock]\nprovider = "file"\ntimeout_seconds = 1\n\n[mystery]\nkey = 1\n'
    )
    result = cli(["status", "--config", "bad.toml"], project)
    assert result.returncode == Exit.USAGE
    assert "unknown top-level tables" in result.stderr


def test_lock_id_out_of_range_is_a_usage_error(project):
    (project / "bad.toml").write_text(
        '[database]\nadapter = "sqlite-probe"\npath = "x.db"\n\n'
        '[lock]\nprovider = "file"\ntimeout_seconds = 1\nid = 1073741824\n'
    )
    result = cli(["status", "--config", "bad.toml"], project)
    assert result.returncode == Exit.USAGE
    assert "lock.id must be in" in result.stderr


def test_json_reports_are_machine_readable(project):
    assert cli(["migrate"], project).returncode == Exit.OK
    for command in ("status", "validate"):
        result = cli([command, "--json"], project)
        assert result.returncode == Exit.OK
        report = json.loads(result.stdout)
        assert report["command"] == command
        assert report["adapter"] == "sqlite-probe"
        assert report["initialized"] is True
        assert report["success_count"] == 1
        assert report["migrations"][0]["recorded_matches_current"] is True


def test_status_reports_pending_units_even_when_uninitialized(project):
    result = cli(["status", "--json"], project)
    assert result.returncode == Exit.NOT_INITIALIZED
    report = json.loads(result.stdout)
    assert [m["id"] for m in report["migrations"]] == ["create-t"]
    assert report["migrations"][0]["state"] == "PENDING"


def test_no_pending_migrations_is_reported(project):
    assert cli(["migrate"], project).returncode == Exit.OK
    result = cli(["migrate"], project)
    assert result.returncode == Exit.OK
    assert "no pending migrations" in result.stdout


def test_changed_successful_source_fails_validation(project):
    assert cli(["migrate"], project).returncode == Exit.OK
    (project / "m1" / "up.sql").write_text("CREATE TABLE t (id INTEGER, extra TEXT);\n")
    for command in (["migrate"], ["validate"], ["status"]):
        result = cli(command, project)
        assert result.returncode == Exit.VALIDATION, command
    out = cli(["validate"], project).stdout + cli(["validate"], project).stderr
    assert "Successful history is immutable" in out


def test_recover_is_refused_when_there_is_no_active_migration(project):
    assert cli(["migrate"], project).returncode == Exit.OK
    result = cli(["migrate", "--recover", "create-t"], project)
    assert result.returncode == Exit.VALIDATION
    assert "requires an ACTIVE restartable migration" in result.stderr


def test_password_is_read_from_the_environment_only(project):
    from migr8.config import PASSWORD_ENV, load

    config = load(project / "migr8.toml")
    assert config.password() is None
    os.environ[PASSWORD_ENV] = "secret"
    try:
        assert load(project / "migr8.toml").password() == "secret"
    finally:
        del os.environ[PASSWORD_ENV]
    # No configuration key carries a credential.
    import tomllib

    document = tomllib.loads((project / "migr8.toml").read_text())
    flat = [key for table in document.values() if isinstance(table, dict) for key in table]
    assert not {key for key in flat if "password" in key or "secret" in key}


def test_text_report_is_human_readable(project):
    assert cli(["migrate"], project).returncode == Exit.OK
    result = cli(["status"], project)
    assert "adapter:   sqlite-probe" in result.stdout
    assert "create-t" in result.stdout
    assert "exit: 0" in result.stdout
