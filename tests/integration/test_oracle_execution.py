"""Oracle execution, recovery, SQL details and concurrency against a real server."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
import support

from migr8.errors import Exit

pytestmark = [pytest.mark.oracle]

ENTRY = Path(__file__).resolve().parents[2] / "migr8"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "oracle"


def _orders_table(root):
    support.unit(
        root,
        "m_table",
        {
            "up.sql": (
                "CREATE TABLE orders (\n"
                "  id     NUMBER(10) NOT NULL PRIMARY KEY,\n"
                "  region VARCHAR2(2 CHAR)\n"
                ")"
            )
        },
    )
    support.unit(
        root,
        "m_seed",
        {
            "up.sql": (
                "INSERT INTO orders (id, region) SELECT LEVEL, NULL FROM dual "
                "CONNECT BY LEVEL <= 12"
            )
        },
    )
    return [
        {
            "id": "create-orders",
            "path": "m_table",
            "language": "sql",
            "mode": "restartable",
            "entry": "up.sql",
        },
        {
            "id": "seed-orders",
            "path": "m_seed",
            "language": "sql",
            "mode": "atomic",
            "entry": "up.sql",
        },
    ]


BACKFILL = '''\
def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                """SELECT id FROM orders
                    WHERE id > :after AND region IS NULL
                    ORDER BY id FETCH FIRST :batch_size ROWS ONLY""",
                {"after": last, "batch_size": 5},
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            changed = tx.executemany(
                "UPDATE orders SET region = :region WHERE id = :id AND region IS NULL",
                [{"id": key, "region": "EU"} for key in ids],
            )
            if changed != len(ids):
                raise RuntimeError("batch membership changed")
            last = max(ids)
            ctx.progress.set("last_id", str(last))
        ctx.log("batch committed", last_id=last)
'''


# --- restartable Python backfill ------------------------------------------------


def test_bounded_backfill_through_the_facade(oracle_project, oracle_query):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(root, "m_fill", {"migration.py": BACKFILL})
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "backfill",
                "path": "m_fill",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT region, COUNT(*) FROM orders GROUP BY region") == [("EU", 12)]
    assert oracle_query("SELECT COUNT(*) FROM m8_progress") == [(0,)]


def test_checkpoint_and_batch_commit_together_and_survive_a_failure(oracle_project, oracle_query):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    failing = BACKFILL.replace(
        '        ctx.log("batch committed", last_id=last)',
        "        if last >= 10:\n"
        '            raise RuntimeError("stop after the second batch")\n'
        '        ctx.log("batch committed", last_id=last)',
    )
    support.unit(root, "m_fill", {"migration.py": failing})
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "backfill",
                "path": "m_fill",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert oracle_query("SELECT COUNT(*) FROM orders WHERE region = 'EU'") == [(10,)]
    assert oracle_query("SELECT prog_key, prog_value FROM m8_progress") == [("last_id", "10")]
    assert oracle_query(
        "SELECT status, attempt FROM m8_history WHERE migration_id = 'backfill'"
    ) == [("ACTIVE", 1)]

    # Identical source: a plain retry resumes from the durable checkpoint.
    support.unit(root, "m_fill", {"migration.py": BACKFILL})
    assert support.migrate(config, manifest) == Exit.VALIDATION
    assert support.migrate(config, manifest, recover="backfill") == Exit.OK
    assert oracle_query("SELECT COUNT(*) FROM orders WHERE region = 'EU'") == [(12,)]
    row = oracle_query(
        "SELECT status, attempt, fingerprint, first_fingerprint FROM m8_history "
        "WHERE migration_id = 'backfill'"
    )[0]
    assert row[0] == "SUCCESS" and row[1] == 2
    assert row[2] != row[3]  # the first fingerprint is retained


def test_batch_rollback_discards_data_and_checkpoint(oracle_project, oracle_query):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(
        root,
        "m_fill",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    with ctx.transaction() as tx:\n"
                "        tx.execute(\"UPDATE orders SET region = 'EU' WHERE id <= 5\")\n"
                "        ctx.progress.set('last_id', '5')\n"
                "        raise RuntimeError('abandon the batch')\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "backfill",
                "path": "m_fill",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert oracle_query("SELECT COUNT(*) FROM orders WHERE region IS NOT NULL") == [(0,)]
    assert oracle_query("SELECT COUNT(*) FROM m8_progress") == [(0,)]


def test_executemany_raises_on_error_instead_of_collecting_row_errors(oracle_project, oracle_query):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(
        root,
        "m_fill",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    with ctx.transaction() as tx:\n"
                "        tx.executemany(\n"
                "            'INSERT INTO orders (id, region) VALUES (:id, :region)',\n"
                "            [{'id': 100, 'region': 'EU'},\n"
                "             {'id': 1, 'region': 'EU'},\n"
                "             {'id': 101, 'region': 'EU'}],\n"
                "        )\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "bulk",
                "path": "m_fill",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "ORA-00001" in report.message
    # The whole batch rolled back: no partial rows survived.
    assert oracle_query("SELECT COUNT(*) FROM orders WHERE id >= 100") == [(0,)]


def test_dml_returning_is_not_exposed_as_a_query_result(oracle_project, oracle_query):
    """Oracle returns these values through output binds, not a result set."""
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(
        root,
        "m_fill",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    with ctx.transaction() as tx:\n"
                "        try:\n"
                "            rows = tx.query(\n"
                "                \"UPDATE orders SET region = 'EU' "
                'WHERE id = 1 RETURNING id INTO :out"\n'
                "            )\n"
                "        except Exception as exc:\n"
                "            ctx.log('returning rejected', detail=str(exc)[:80])\n"
                "        else:\n"
                "            raise AssertionError("
                "f'RETURNING must not yield a result set: {rows}')\n"
                "        tx.execute(\"UPDATE orders SET region = 'EU' WHERE id = 1\")\n"
                "        ctx.progress.set('done', 'yes')\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "returning",
                "path": "m_fill",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT region FROM orders WHERE id = 1") == [("EU",)]


def test_progress_value_accepts_non_ascii_within_the_byte_limit(oracle_project, oracle_query):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(
        root,
        "m_fill",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    with ctx.transaction():\n"
                "        ctx.progress.set('label', 'région-Ü-\\u00e9')\n"
                "    assert ctx.progress.get('label') == 'région-Ü-\\u00e9'\n"
                "    with ctx.transaction():\n"
                "        ctx.progress.set('big', 'x' * 4000)\n"
                "    try:\n"
                "        with ctx.transaction():\n"
                "            ctx.progress.set('toobig', 'é' * 2001)\n"
                "    except Exception as exc:\n"
                "        assert 'exceeds 4000' in str(exc), exc\n"
                "    else:\n"
                "        raise AssertionError('oversized value must be rejected')\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "progress",
                "path": "m_fill",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK


# --- DDL and facade restrictions --------------------------------------------------


def test_ddl_outside_a_batch_is_allowed_and_inside_is_not(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(
        root,
        "m1",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    existing = ctx.query(\n"
                '        "SELECT COUNT(*) FROM all_tables WHERE owner = USER AND "\n'
                "        \"table_name = 'MADE_BY_DDL'\"\n"
                "    )[0][0]\n"
                "    if not existing:\n"
                "        ctx.ddl('CREATE TABLE made_by_ddl (id NUMBER(10) PRIMARY KEY)')\n"
                "    with ctx.transaction() as tx:\n"
                "        tx.execute('INSERT INTO made_by_ddl (id) VALUES (1)')\n"
                "        ctx.progress.set('seeded', 'yes')\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "ddl",
                "path": "m1",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT id FROM made_by_ddl") == [(1,)]


@pytest.mark.parametrize(
    "body,expected",
    [
        ("    ctx.ddl('ALTER SESSION SET CURRENT_SCHEMA = SYS')", "session or system state"),
        ("    ctx.ddl('SELECT 1 FROM dual')", "DDL allow-list"),
        ("    ctx.execute('CREATE TABLE nope (id NUMBER(10))')", "rejected in restartable mode"),
        ("    ctx.execute(\"UPDATE orders SET region = 'EU'\")", "rejected in restartable mode"),
        ("    ctx.execute('COMMIT')", "engine-owned"),
        ("    ctx.execute('SET TRANSACTION READ ONLY')", "engine-owned"),
        ("    ctx.ddl('DROP TABLE m8_history')", "reserved metadata object"),
    ],
)
def test_facade_refusals(oracle_project, oracle_query, body, expected):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(root, "m_bad", {"migration.py": f"def migrate(ctx):\n{body}\n"})
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "bad",
                "path": "m_bad",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert expected in report.message


def test_ddl_with_an_open_transaction_is_refused(oracle_project, oracle_query):
    """No pre-DDL implicit commit is used to flush unrelated pending work."""
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(
        root,
        "m_bad",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    with ctx.transaction() as tx:\n"
                "        tx.execute(\"UPDATE orders SET region = 'EU' WHERE id = 1\")\n"
                "        ctx.progress.set('k', 'v')\n"
                "    ctx.execute('BEGIN UPDATE orders SET region = :r WHERE id = 2; END;',\n"
                "                {'r': 'EU'})\n"
                "    ctx.ddl('CREATE TABLE after_open_txn (id NUMBER(10))')\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "bad",
                "path": "m_bad",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "requires no open transaction" in report.message
    # The first batch is durable; the uncommitted second update was rolled back.
    assert oracle_query("SELECT id FROM orders WHERE region = 'EU' ORDER BY id") == [(1,)]
    assert oracle_query(
        "SELECT COUNT(*) FROM all_tables WHERE owner = :owner AND table_name = 'AFTER_OPEN_TXN'",
        owner=schema,
    ) == [(0,)]


def test_open_transaction_on_return_is_rolled_back_and_refused(oracle_project, oracle_query):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(
        root,
        "m_bad",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    ctx.execute('BEGIN UPDATE orders SET region = :r WHERE id = 1; END;',\n"
                "                {'r': 'EU'})\n"
            )
        },
    )
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "bad",
                "path": "m_bad",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.MIGRATION_FAILED
    assert "open transaction" in report.message
    assert oracle_query("SELECT COUNT(*) FROM orders WHERE region IS NOT NULL") == [(0,)]
    assert oracle_query("SELECT status FROM m8_history WHERE migration_id = 'bad'") == [("ACTIVE",)]


# --- Oracle SQL details ------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,probe",
    [
        ("INSERT INTO orders (id, region) VALUES (99, 'EU') -- trailing ; and /\n", 99),
        ("INSERT INTO orders (id, region) /* ; and / inside */ VALUES (98, 'EU');", 98),
        ("INSERT INTO orders (id, region)\nVALUES (97, 'EU')\n;\n", 97),
    ],
)
def test_literals_and_comments_survive_normalisation(oracle_project, oracle_query, sql, probe):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(root, "m_sql", {"up.sql": sql})
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "detail",
                "path": "m_sql",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT id FROM orders WHERE id = :id", id=probe) == [(probe,)]


def test_multiline_update_with_a_set_line_and_a_slash_literal(oracle_project, oracle_query):
    root, config, schema = oracle_project
    entries = _orders_table(root)
    support.unit(root, "m_sql", {"up.sql": ("UPDATE orders\nSET region = 'EU'\nWHERE id = 1\n")})
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "detail",
                "path": "m_sql",
                "language": "sql",
                "mode": "atomic",
                "entry": "up.sql",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT region FROM orders WHERE id = 1") == [("EU",)]


def test_stored_plsql_keeps_its_terminator_and_create_library_does_not(
    oracle_project, oracle_query
):
    root, config, schema = oracle_project
    support.unit(
        root,
        "m1",
        {
            "up.sql": (
                "CREATE OR REPLACE FUNCTION f_region RETURN VARCHAR2 IS\n"
                "BEGIN\n"
                "  RETURN 'EU';  -- a comment with ; and /\n"
                "END f_region;\n"
                "/\n"
            )
        },
    )
    support.unit(
        root,
        "m2",
        {"up.sql": ("CREATE OR REPLACE LIBRARY lib_probe AS '/nonexistent/libprobe.so';\n")},
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "func",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
                "require_valid": [{"name": "F_REGION", "type": "FUNCTION"}],
            },
            {
                "id": "lib",
                "path": "m2",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK
    assert oracle_query("SELECT f_region FROM dual") == [("EU",)]
    assert oracle_query(
        "SELECT COUNT(*) FROM all_objects WHERE owner = :owner AND "
        "object_name = 'LIB_PROBE' AND object_type = 'LIBRARY'",
        owner=schema,
    ) == [(1,)]


def test_sqlplus_command_in_a_sql_entry_is_refused_before_execution(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(root, "m1", {"up.sql": "SET SERVEROUTPUT ON\n"})
    manifest = support.manifest(
        root,
        [
            {
                "id": "bad",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.VALIDATION
    # Nothing was initialized: the refusal happens in preflight.
    assert oracle_query(
        "SELECT COUNT(*) FROM all_objects WHERE owner = :owner AND object_name = 'M8_HISTORY'",
        owner=schema,
    ) == [(0,)]


# --- recovery admission --------------------------------------------------------------


def test_sql_to_python_recovery_records_consistent_metadata(oracle_project, oracle_query):
    root, config, schema = oracle_project
    # A SQL restartable migration that fails its declared validity check.
    support.unit(root, "m1", {"up.sql": "CREATE TABLE staging_t (id NUMBER(10))"})
    entry = {
        "id": "convert",
        "path": "m1",
        "language": "sql",
        "mode": "restartable",
        "entry": "up.sql",
        "require_valid": [{"name": "V_MISSING", "type": "VIEW"}],
    }
    manifest = support.manifest(root, [entry])
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    before = oracle_query('SELECT seq, "MODE", first_fingerprint, started_at FROM m8_history')[0]

    # Amend the source, switching language to Python. The identity, position,
    # mode, first fingerprint and original start time are preserved.
    support.unit(
        root,
        "m1",
        {
            "migration.py": (
                "def migrate(ctx):\n"
                "    existing = ctx.query(\n"
                '        "SELECT COUNT(*) FROM all_tables WHERE owner = USER AND "\n'
                "        \"table_name = 'STAGING_T'\"\n"
                "    )[0][0]\n"
                "    if not existing:\n"
                "        ctx.ddl('CREATE TABLE staging_t (id NUMBER(10))')\n"
                "    ctx.ddl('CREATE OR REPLACE VIEW v_missing AS SELECT id FROM staging_t')\n"
            )
        },
    )
    (root / "m1" / "up.sql").unlink()
    support.manifest(root, [{**entry, "language": "python", "entry": "migration.py"}])
    assert support.migrate(config, manifest, recover="convert") == Exit.OK
    after = oracle_query(
        'SELECT seq, "MODE", first_fingerprint, started_at, language, attempt, status '
        "FROM m8_history"
    )[0]
    assert after[0] == before[0]
    assert after[1] == before[1] == "restartable"
    assert after[2] == before[2]
    assert after[3] == before[3]
    assert after[4] == "python"
    assert after[5] == 2
    assert after[6] == "SUCCESS"


def test_recovery_converges_from_checkpoints_of_two_earlier_versions(oracle_project, oracle_query):
    """Spec Section 10.1: convergence from every admitted source version."""
    root, config, schema = oracle_project
    entries = _orders_table(root)
    # Version 1 checkpoints a plain integer and stops halfway.
    v1 = (
        "def migrate(ctx):\n"
        "    last = int(ctx.progress.get('last_id', '0'))\n"
        "    with ctx.transaction() as tx:\n"
        "        tx.execute(\"UPDATE orders SET region = 'EU' WHERE id <= 4\")\n"
        "        ctx.progress.set('last_id', '4')\n"
        "    raise RuntimeError('stop in version 1')\n"
    )
    support.unit(root, "m_fill", {"migration.py": v1})
    manifest = support.manifest(
        root,
        [
            *entries,
            {
                "id": "backfill",
                "path": "m_fill",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            },
        ],
    )
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    assert oracle_query("SELECT prog_key, prog_value FROM m8_progress") == [("last_id", "4")]

    # Version 2 changes the checkpoint format, which is part of recovery logic.
    v2 = (
        "def migrate(ctx):\n"
        "    legacy = ctx.progress.get('last_id')\n"
        "    cursor = ctx.progress.get('cursor')\n"
        "    if cursor is None:\n"
        "        start = int(legacy) if legacy else 0\n"
        "    else:\n"
        "        start = int(cursor.split(':', 1)[1])\n"
        "    with ctx.transaction() as tx:\n"
        "        tx.execute(\"UPDATE orders SET region = 'EU' WHERE id > :after\",\n"
        "                   {'after': start})\n"
        "        ctx.progress.set('cursor', 'v2:12')\n"
        "    raise RuntimeError('stop in version 2')\n"
    )
    support.unit(root, "m_fill", {"migration.py": v2})
    assert support.migrate(config, manifest, recover="backfill") == Exit.MIGRATION_FAILED
    keys = dict(oracle_query("SELECT prog_key, prog_value FROM m8_progress"))
    assert keys == {"last_id": "4", "cursor": "v2:12"}

    # Version 3 must converge from either format.
    v3 = (
        "def migrate(ctx):\n"
        "    cursor = ctx.progress.get('cursor')\n"
        "    legacy = ctx.progress.get('last_id')\n"
        "    start = int(cursor.split(':', 1)[1]) if cursor else (\n"
        "        int(legacy) if legacy else 0)\n"
        "    with ctx.transaction() as tx:\n"
        "        tx.execute(\"UPDATE orders SET region = 'EU' WHERE id > :after\",\n"
        "                   {'after': start})\n"
        "        ctx.progress.set('cursor', 'v3:done')\n"
    )
    support.unit(root, "m_fill", {"migration.py": v3})
    assert support.migrate(config, manifest, recover="backfill") == Exit.OK
    assert oracle_query("SELECT COUNT(*) FROM orders WHERE region = 'EU'") == [(12,)]
    assert oracle_query("SELECT COUNT(*) FROM m8_progress") == [(0,)]
    assert oracle_query("SELECT attempt FROM m8_history WHERE migration_id = 'backfill'") == [(3,)]


def test_recover_cannot_change_mode(oracle_project, oracle_query):
    root, config, schema = oracle_project
    support.unit(
        root, "m1", {"migration.py": ("def migrate(ctx):\n    raise RuntimeError('fail once')\n")}
    )
    entry = {
        "id": "m",
        "path": "m1",
        "language": "python",
        "mode": "restartable",
        "entry": "migration.py",
    }
    manifest = support.manifest(root, [entry])
    assert support.migrate(config, manifest) == Exit.MIGRATION_FAILED
    support.unit(root, "m1", {"migration.py": "def migrate(ctx):\n    pass\n"})
    support.manifest(root, [{**entry, "mode": "atomic"}])
    report = support.migrate_report(config, manifest, recover="m")
    assert report.exit_code == Exit.VALIDATION
    assert "restartable mode cannot change" in report.message


# --- separate connect user and target schema ----------------------------------------


def test_a_proxy_connect_string_migrates_as_the_target_user(tmp_path, oracle_settings):
    """Spec Section 12: `RUNNER[OWNER]` authenticates as one user and runs as another.

    Proxy authentication is how a deployment separates the identity that proves
    who is running from the schema that owns the objects. The session user is the
    target, so the target schema defaults to it, no `CURRENT_SCHEMA` is set, and
    the objects belong to the owner without the runner holding `ANY` privileges.
    """
    import oracledb

    runner = f"{oracle_settings['user']}_RUNNER"
    owner = f"{oracle_settings['user']}_OWNER"
    with oracledb.connect(
        user=owner, password=oracle_settings["password"], dsn=oracle_settings["dsn"]
    ) as probe:
        cursor = probe.cursor()
        for name, kind in cursor.execute(
            "SELECT object_name, object_type FROM all_objects WHERE owner = :owner "
            "AND object_type IN ('TABLE','VIEW') ORDER BY object_type DESC",
            owner=owner,
        ).fetchall():
            suffix = " CASCADE CONSTRAINTS PURGE" if kind == "TABLE" else ""
            with contextlib.suppress(oracledb.DatabaseError):
                cursor.execute(f'DROP {kind} "{owner}"."{name}"{suffix}')
        probe.commit()

    config = support.write(
        tmp_path / "migr8.toml",
        f"""
        [database]
        adapter = "oracle"
        dsn = "{oracle_settings["dsn"]}"
        user = "{runner}[{owner}]"

        [oracle]
        ddl_lock_timeout_seconds = 10
{support.oracle_mode_options()}
        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = 4715
        timeout_seconds = 20
    """,
    )
    os.environ["MIGR8_PASSWORD"] = oracle_settings["password"]
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE proxy_t (id NUMBER(10))"})
    manifest = support.manifest(
        tmp_path,
        [
            {
                "id": "create-proxy-t",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )

    assert support.migrate(config, manifest) == Exit.OK
    assert support.report_for("validate", config, manifest).exit_code == Exit.OK

    with oracledb.connect(
        user=f"{runner}[{owner}]", password=oracle_settings["password"], dsn=oracle_settings["dsn"]
    ) as probe:
        cursor = probe.cursor()
        assert cursor.execute("SELECT USER FROM dual").fetchone()[0] == owner
        assert (
            cursor.execute("SELECT sys_context('USERENV','PROXY_USER') FROM dual").fetchone()[0]
            == runner
        )
        owned = {
            row[0]
            for row in cursor.execute(
                "SELECT object_name FROM all_objects WHERE owner = :owner "
                "AND object_name IN ('PROXY_T','M8_HISTORY','M8_META')",
                owner=owner,
            ).fetchall()
        }
    assert owned == {"PROXY_T", "M8_HISTORY", "M8_META"}


def test_connect_user_differs_from_target_schema(tmp_path, oracle_settings, oracle_query):
    """Spec Section 12: CURRENT_SCHEMA changes resolution, not ownership."""
    runner = f"{oracle_settings['user']}_RUNNER"
    owner = f"{oracle_settings['user']}_OWNER"
    import oracledb

    with oracledb.connect(
        user=runner, password=oracle_settings["password"], dsn=oracle_settings["dsn"]
    ) as probe:
        cursor = probe.cursor()
        for name, kind in cursor.execute(
            "SELECT object_name, object_type FROM all_objects WHERE owner = :owner "
            "AND object_type IN ('TABLE','VIEW') ORDER BY object_type DESC",
            owner=owner,
        ).fetchall():
            suffix = " CASCADE CONSTRAINTS PURGE" if kind == "TABLE" else ""
            with contextlib.suppress(oracledb.DatabaseError):
                cursor.execute(f'DROP {kind} "{owner}"."{name}"{suffix}')
        probe.commit()

    config = support.write(
        tmp_path / "migr8.toml",
        f"""
        [database]
        adapter = "oracle"
        dsn = "{oracle_settings["dsn"]}"
        user = "{runner}"
        target_schema = "{owner}"

        [oracle]
        ddl_lock_timeout_seconds = 10
{support.oracle_mode_options()}
        [lock]
        provider = "dbms_lock"
        package = "SYS.DBMS_LOCK"
        id = 4713
        timeout_seconds = 20
    """,
    )
    os.environ["MIGR8_PASSWORD"] = oracle_settings["password"]
    support.unit(tmp_path, "m1", {"up.sql": "CREATE TABLE split_t (id NUMBER(10))"})
    manifest = support.manifest(
        tmp_path,
        [
            {
                "id": "split",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )
    assert support.migrate(config, manifest) == Exit.OK

    with oracledb.connect(
        user=runner, password=oracle_settings["password"], dsn=oracle_settings["dsn"]
    ) as probe:
        rows = (
            probe.cursor()
            .execute(
                "SELECT object_name FROM all_objects WHERE owner = :owner AND "
                "object_type = 'TABLE' ORDER BY object_name",
                owner=owner,
            )
            .fetchall()
        )
        assert [row[0] for row in rows] == ["M8_HISTORY", "M8_META", "M8_PROGRESS", "SPLIT_T"]
        namespace = (
            probe.cursor().execute(f'SELECT target_namespace FROM "{owner}".m8_meta').fetchone()
        )
        assert namespace[0] == owner
