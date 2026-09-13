"""Oracle exclusive execution and read-only inspection against a real server.

These use real cooperating OS processes and real server sessions. The lock is a
real ``DBMS_LOCK`` user lock held with ``release_on_commit => FALSE``.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import support
from proxy import DirectionalProxy

from migr8.errors import Exit

pytestmark = [pytest.mark.oracle]

ENTRY = Path(__file__).resolve().parents[2] / "migr8"
TIMEOUT = 120

SLOW = """\
import pathlib
import time

READY = pathlib.Path({ready!r})
GO = pathlib.Path({go!r})


def migrate(ctx):
    # Convergent from every durable state this migration can leave behind.
    existing = ctx.query(
        "SELECT COUNT(*) FROM all_tables WHERE owner = USER AND table_name = 'SLOW_T'"
    )[0][0]
    if not existing:
        ctx.ddl("CREATE TABLE slow_t (id NUMBER(10) PRIMARY KEY)")
    last = int(ctx.progress.get("last_id", "0"))
    if last < 1:
        with ctx.transaction() as tx:
            tx.execute("INSERT INTO slow_t (id) VALUES (1)")
            ctx.progress.set("last_id", "1")
        last = 1
    READY.write_text("holding the lock after DDL and one batch commit")
    deadline = time.monotonic() + 90
    while not GO.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if last < 2:
        with ctx.transaction() as tx:
            tx.execute("INSERT INTO slow_t (id) VALUES (2)")
            ctx.progress.set("last_id", "2")
"""


def _config_with_timeout(root: Path, config: Path, seconds: int, name: str) -> Path:
    text = config.read_text()
    assert "timeout_seconds = 20" in text
    return support.write(
        root / name, text.replace("timeout_seconds = 20", f"timeout_seconds = {seconds}")
    )


def run_cli(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRY), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env=env,
    )


def start_cli(args: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ENTRY), *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_until(predicate, *, seconds: float = 90) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def _wait_for(path: Path, *, seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


@pytest.fixture
def slow_project(oracle_project, oracle_settings):
    root, config, schema = oracle_project
    ready = root / "ready"
    go = root / "go"
    support.unit(root, "m1", {"migration.py": SLOW.format(ready=str(ready), go=str(go))})
    support.manifest(
        root,
        [
            {
                "id": "slow",
                "path": "m1",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            }
        ],
    )
    env = {**os.environ, "MIGR8_PASSWORD": oracle_settings["password"]}
    return root, config, schema, ready, go, env


def test_lock_is_held_across_ddl_and_batch_commits(slow_project, oracle_query):
    root, config, schema, ready, go, env = slow_project
    _config_with_timeout(root, config, 0, "zero.toml")
    holder = start_cli(["migrate"], root, env)
    try:
        _wait_for(ready)
        # The holder has already committed DDL and one batch, yet still owns the lock.
        assert oracle_query("SELECT id FROM slow_t ORDER BY id") == [(1,)]
        contender = run_cli(["migrate", "--config", "zero.toml"], root, env)
        assert contender.returncode == Exit.LOCK_NOT_ACQUIRED
        assert "not acquired" in contender.stderr
        assert "not a lock-test failure" in contender.stderr
    finally:
        go.write_text("continue")
        holder.wait(timeout=TIMEOUT)
    assert holder.returncode == Exit.OK
    assert oracle_query("SELECT id FROM slow_t ORDER BY id") == [(1,), (2,)]


def test_status_is_read_only_and_prompt_during_a_long_migration(slow_project, oracle_query):
    root, config, schema, ready, go, env = slow_project
    holder = start_cli(["migrate"], root, env)
    try:
        _wait_for(ready)
        started = time.monotonic()
        inspect = run_cli(["status", "--json"], root, env)
        elapsed = time.monotonic() - started
        assert inspect.returncode == Exit.OK
        # Committed metadata is returned without waiting for the migration lock.
        assert elapsed < 20, f"status took {elapsed:.1f}s"
        report = json.loads(inspect.stdout)
        entry = report["migrations"][0]
        assert entry["state"] == "ACTIVE" and entry["attempt"] == 1
        assert entry["recorded_matches_current"] is True
        assert entry["db_session"] and "sid=" in entry["db_session"]
        assert entry["session_liveness"] == "present"
        assert "not proof that the migration is executing" in entry["session_liveness_detail"]
        assert run_cli(["validate"], root, env).returncode == Exit.OK
    finally:
        go.write_text("continue")
        holder.wait(timeout=TIMEOUT)
    assert holder.returncode == Exit.OK


def test_waiter_acquires_the_lock_after_the_holder_finishes(slow_project, oracle_query):
    root, config, schema, ready, go, env = slow_project
    holder = start_cli(["migrate"], root, env)
    _wait_for(ready)
    waiter = start_cli(["migrate"], root, env)
    time.sleep(1.0)
    assert waiter.poll() is None, "the waiter should still be blocked on the lock"
    go.write_text("continue")
    holder.wait(timeout=TIMEOUT)
    waiter_out, _waiter_err = waiter.communicate(timeout=TIMEOUT)
    assert holder.returncode == Exit.OK
    assert waiter.returncode == Exit.OK
    # Validating, finding nothing pending and exiting successfully is correct.
    assert "no pending migrations" in waiter_out


def test_dead_client_leaves_a_live_server_session_holding_the_lock(
    slow_project, oracle_query, oracle_sys, oracle_settings
):
    """A fresh runner proceeds only after acquiring the lock, not after noticing
    that the previous operating-system process has disappeared.

    Killing a local process normally closes its socket, which would end the
    server session immediately. To create a genuinely live server session behind
    a dead client, the holder connects through a proxy that keeps the upstream
    socket open after the client vanishes.
    """
    root, config, schema, ready, go, env = slow_project
    host, rest = oracle_settings["dsn"].split(":", 1)
    port, service = rest.split("/", 1)

    with DirectionalProxy(host, int(port)) as proxy:
        support.write(
            root / "proxied.toml",
            config.read_text().replace(
                f'dsn = "{oracle_settings["dsn"]}"',
                f'dsn = "{proxy.host}:{proxy.port}/{service}"',
            ),
        )
        _config_with_timeout(root, config, 0, "zero.toml")
        holder = start_cli(["migrate", "--config", "proxied.toml"], root, env)
        try:
            _wait_for(ready)
            recorded = oracle_query("SELECT db_session FROM m8_history WHERE status = 'ACTIVE'")
            assert recorded, "the ACTIVE row should record the runner's session"
            fields = dict(part.split("=", 1) for part in recorded[0][0].split(",") if "=" in part)
            audsid = int(fields["audsid"])

            # Keep the upstream socket open, then kill the client process.
            proxy.orphan_server_sessions()
            holder.kill()
            holder.wait(timeout=TIMEOUT)
            assert holder.returncode != 0

            # The client is gone; the server session is still there.
            sessions = oracle_sys(
                "SELECT sid, serial# FROM v$session WHERE audsid = :audsid", audsid=audsid
            )
            assert sessions, "the server session should outlive the killed client"

            # The lock is still held by that session, so a fresh runner waits.
            contender = run_cli(["migrate", "--config", "zero.toml"], root, env)
            assert contender.returncode == Exit.LOCK_NOT_ACQUIRED
            assert "may not have ended" in contender.stderr

            # Only once the server session ends is the lock released.
            sid, serial = sessions[0]
            oracle_sys(f"ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE")
            assert _wait_until(
                lambda: (
                    not oracle_sys(
                        "SELECT sid FROM v$session WHERE audsid = :audsid", audsid=audsid
                    )
                )
            ), "the killed server session did not end"
        finally:
            proxy.close_server_sessions()

    go.write_text("continue")
    resumed = run_cli(["migrate"], root, env)
    assert resumed.returncode == Exit.OK, resumed.stderr
    # The durable first batch survived; the retry converged.
    assert oracle_query("SELECT id FROM slow_t ORDER BY id") == [(1,), (2,)]
    assert oracle_query("SELECT status, attempt FROM m8_history WHERE migration_id = 'slow'") == [
        ("SUCCESS", 2)
    ]


def test_session_liveness_is_unknown_without_privileges(oracle_project, oracle_settings):
    """The RUNNER fixture cannot read V$SESSION, so the diagnostic reports unknown."""
    root, config, schema = oracle_project
    runner = f"{oracle_settings['user']}_RUNNER"
    owner = f"{oracle_settings['user']}_OWNER"
    import oracledb

    with oracledb.connect(
        user=runner, password=oracle_settings["password"], dsn=oracle_settings["dsn"]
    ) as probe:
        cursor = probe.cursor()
        for name, kind in cursor.execute(
            "SELECT object_name, object_type FROM all_objects WHERE owner = :owner AND "
            "object_type IN ('TABLE','VIEW') ORDER BY object_type DESC",
            owner=owner,
        ).fetchall():
            suffix = " CASCADE CONSTRAINTS PURGE" if kind == "TABLE" else ""
            with contextlib.suppress(oracledb.DatabaseError):
                cursor.execute(f'DROP {kind} "{owner}"."{name}"{suffix}')
        probe.commit()

    split = support.write(
        root / "split.toml",
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
        id = 4714
        timeout_seconds = 20
    """,
    )
    os.environ["MIGR8_PASSWORD"] = oracle_settings["password"]
    support.unit(
        root,
        "m_fail",
        {"migration.py": ("def migrate(ctx):\n    raise RuntimeError('stay active')\n")},
    )
    manifest = support.manifest(
        root,
        [
            {
                "id": "stuck",
                "path": "m_fail",
                "language": "python",
                "mode": "restartable",
                "entry": "migration.py",
            }
        ],
    )
    assert support.migrate(split, manifest) == Exit.MIGRATION_FAILED
    report = support.report_for("status", split, manifest)
    entry = report.migrations[0]
    assert entry.state == "ACTIVE"
    assert entry.session_liveness == "unknown"
    assert "serial" in entry.session_liveness_detail
