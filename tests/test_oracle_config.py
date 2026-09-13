"""Oracle driver-mode configuration, checked without a database (spec Section 12).

Thin and Thick are different client libraries, a different network stack and
different authentication paths, so which one a run uses is a configured decision
rather than whatever the process happens to be in. These cover the part of that
decision the adapter can settle before it connects.
"""

from __future__ import annotations

import pytest
import support

from migr8.errors import ConfigError

pytest.importorskip("oracledb")


def config(tmp_path, oracle: str = "", user: str = "MIGR8_TEST", extra: str = "") -> object:
    from migr8.config import load as load_config

    path = support.write(
        tmp_path / "migr8.toml",
        f"""
        [database]
        adapter = "oracle"
        dsn = "localhost:1521/FREEPDB1"
        user = "{user}"
{extra}

        [oracle]
        ddl_lock_timeout_seconds = 10
{oracle}
        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = 4711
        timeout_seconds = 20
        """,
    )
    return load_config(path)


def adapter(settings):
    from migr8.adapters.oracle import OracleAdapter

    return OracleAdapter(settings)


def test_thin_is_the_default_and_is_reported(tmp_path):
    assert "Thin mode" in adapter(config(tmp_path)).capabilities().notes[0]


def test_thick_mode_is_reported_when_it_is_selected(tmp_path):
    built = adapter(config(tmp_path, "        allow_thick_mode = true\n"))
    assert "Thick mode" in built.capabilities().notes[0]


def test_client_libraries_without_thick_mode_are_refused(tmp_path):
    """The libraries are only loaded for thick mode, so naming them alone is a mistake."""
    with pytest.raises(ConfigError, match="allow_thick_mode"):
        adapter(config(tmp_path, f'        client_lib_dir = "{tmp_path}"\n'))


@pytest.mark.parametrize("key", ["client_lib_dir", "config_dir"])
def test_a_directory_that_does_not_exist_is_refused(tmp_path, key):
    """Refused here, by name, rather than as a driver error at the first connection."""
    missing = tmp_path / "nowhere"
    with pytest.raises(ConfigError, match=key):
        adapter(
            config(
                tmp_path,
                f'        allow_thick_mode = true\n        {key} = "{missing}"\n',
            )
        )


def test_the_directories_are_accepted_when_they_exist(tmp_path):
    built = adapter(
        config(
            tmp_path,
            f'        allow_thick_mode = true\n        client_lib_dir = "{tmp_path}"\n'
            f'        config_dir = "{tmp_path}"\n',
        )
    )
    notes = built.capabilities().notes[0]
    assert str(tmp_path) in notes
    assert "driver configuration from" in notes


def test_an_unknown_oracle_option_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="thick_mode_please"):
        adapter(config(tmp_path, "        thick_mode_please = true\n"))


# --- proxy authentication ----------------------------------------------------------


def test_a_proxy_connect_string_takes_the_target_as_the_schema(tmp_path):
    """After proxy authentication the session user is the target, so it is the default."""
    built = adapter(config(tmp_path, user="RUNNER[APP_DBA]"))
    assert built.normalized_namespace() == "APP_DBA"
    notes = " ".join(built.capabilities().notes)
    assert "Session user APP_DBA" in notes
    assert "through RUNNER" in notes


def test_an_explicit_target_schema_still_wins(tmp_path):
    built = adapter(
        config(tmp_path, user="RUNNER[APP_DBA]", extra='        target_schema = "REPORTING"\n')
    )
    assert built.normalized_namespace() == "REPORTING"


def test_a_target_only_connect_string_is_external_authentication(tmp_path):
    built = adapter(config(tmp_path, "        allow_thick_mode = true\n", user="[APP_DBA]"))
    assert built.normalized_namespace() == "APP_DBA"
    assert "no connecting user" in " ".join(built.capabilities().notes)


@pytest.mark.parametrize(
    "user",
    ["runner[APP_DBA]", "RUNNER[app_dba]", "RUNNER[]", "[]", "RUNNER[A][B]", "RUNNER[APP DBA]"],
)
def test_a_malformed_connect_string_is_refused(tmp_path, user):
    """Both halves are held to the identifier rule a plain user is held to."""
    with pytest.raises(ConfigError, match=r"database\.user"):
        adapter(config(tmp_path, user=user))


def test_a_plain_user_is_unchanged(tmp_path):
    built = adapter(config(tmp_path))
    assert built.normalized_namespace() == "MIGR8_TEST"
    assert "Session user MIGR8_TEST." in " ".join(built.capabilities().notes)
