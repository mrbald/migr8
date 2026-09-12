"""Deterministic commit-acknowledgement failures (spec Section 14.3).

Each scenario is induced with a real TCP proxy that controls the two directions
independently, synchronised at the engine's own commit boundary through
``migr8.testing.hooks``.  A signal from migration code would not be enough,
because further driver calls precede the COMMIT.

**Request not delivered.** The runner is held immediately before the commit, the
client-to-server direction is dropped so the commit request never reaches the
server, and then the transport is terminated.  Once the original server session
has ended, an independent session asserts the transaction's work and history are
absent.

**Commit durable, response not received.** The commit request is allowed through
and only its response is withheld.  An independent observer connection confirms
the durable state *while the runner is still blocked*.  The transport is then
terminated so the waiting runner receives an error.

Both branches assert exit code 4, no in-run continuation, no cleanup SQL after
the unknown outcome, and correct fresh-run reconciliation.  A test that accepted
either durable outcome would establish nothing, so each scenario states which
branch it induced and checks that branch's state.

This is transport-level evidence. Wrapper simulations of the same contract live
in ``tests/test_unknown_outcome.py`` and are labelled as such there.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

import support
from migr8.adapters.base import Boundary
from migr8.errors import Exit
from migr8.testing import hooks
from proxy import DirectionalProxy

REQUEST_NOT_DELIVERED = "request_not_delivered"
RESPONSE_WITHHELD = "response_withheld"
BRANCHES = (REQUEST_NOT_DELIVERED, RESPONSE_WITHHELD)


class Interceptor:
    """Arms one directional transport failure at one engine-owned boundary."""

    def __init__(self, proxy: DirectionalProxy, boundary: Boundary, *, branch: str,
                 occurrence: int = 1, observe=None, delay: float = 1.0) -> None:
        self.proxy = proxy
        self.boundary = boundary
        self.branch = branch
        self.occurrence = occurrence
        self.observe = observe
        self.delay = delay
        self.seen = 0
        self.fired = False
        self.observed: bool | None = None
        self.rollbacks_after_latch = 0
        self._timer: threading.Thread | None = None

    def install(self, adapter) -> None:
        hooks.register(self._on_boundary)
        real_rollback = adapter.rollback

        def rollback():
            if adapter.latch is not None and adapter.latch.latched:
                self.rollbacks_after_latch += 1
            return real_rollback()

        adapter.rollback = rollback

    def _on_boundary(self, boundary: str, phase: str) -> None:
        if boundary != self.boundary or phase != hooks.BEFORE_COMMIT:
            return
        self.seen += 1
        if self.seen != self.occurrence or self.fired:
            return
        self.fired = True
        if self.branch == REQUEST_NOT_DELIVERED:
            self.proxy.drop_requests()
        else:
            self.proxy.hold_responses()
        self._timer = threading.Thread(target=self._sever, daemon=True)
        self._timer.start()

    def _sever(self) -> None:
        time.sleep(self.delay)
        if self.branch == RESPONSE_WITHHELD and self.observe is not None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.observe():
                    self.observed = True
                    break
                time.sleep(0.2)
            else:
                self.observed = False
        self.proxy.reset_clients()

    def join(self) -> None:
        if self._timer is not None:
            self._timer.join(timeout=40)


def _poll(predicate, *, seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


# --- shared project shape -------------------------------------------------------

ORACLE_CREATE = """\
DECLARE
  already_exists EXCEPTION;
  PRAGMA EXCEPTION_INIT(already_exists, -955);
BEGIN
  EXECUTE IMMEDIATE 'CREATE TABLE items (id NUMBER(10) PRIMARY KEY, tag VARCHAR2(4 CHAR))';
EXCEPTION
  WHEN already_exists THEN NULL;
END;
/
"""

PG_CREATE = "CREATE TABLE IF NOT EXISTS items (id integer PRIMARY KEY, tag text)"

ORACLE_SEED = "INSERT INTO items (id, tag) SELECT LEVEL, NULL FROM dual CONNECT BY LEVEL <= 6"
PG_SEED = "INSERT INTO items (id, tag) SELECT g, NULL FROM generate_series(1, 6) g"

ORACLE_BATCH = '''\
def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                "SELECT id FROM items WHERE id > :after AND tag IS NULL "
                "ORDER BY id FETCH FIRST 3 ROWS ONLY",
                {"after": last},
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            tx.executemany(
                "UPDATE items SET tag = :tag WHERE id = :id AND tag IS NULL",
                [{"id": key, "tag": "EU"} for key in ids],
            )
            last = max(ids)
            ctx.progress.set("last_id", str(last))
'''

PG_BATCH = '''\
def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                "SELECT id FROM items WHERE id > %s AND tag IS NULL ORDER BY id LIMIT 3",
                (last,),
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            tx.executemany(
                "UPDATE items SET tag = %s WHERE id = %s AND tag IS NULL",
                [("EU", key) for key in ids],
            )
            last = max(ids)
            ctx.progress.set("last_id", str(last))
'''


def _build_project(root: Path, *, create: str, seed: str, batch: str) -> Path:
    support.unit(root, "m1", {"up.sql": create})
    support.unit(root, "m2", {"up.sql": seed})
    support.unit(root, "m3", {"migration.py": batch})
    return support.manifest(root, [
        {"id": "create-items", "path": "m1", "language": "sql", "mode": "restartable",
         "entry": "up.sql"},
        {"id": "seed-items", "path": "m2", "language": "sql", "mode": "atomic",
         "entry": "up.sql"},
        {"id": "tag-items", "path": "m3", "language": "python", "mode": "restartable",
         "entry": "migration.py"},
    ])


#: ``boundary``, ``occurrence``, and a predicate name resolved per database.
SCENARIOS = {
    "initialization": (Boundary.INITIALIZATION_COMPLETE, 1, "marker"),
    "admission": (Boundary.RESTARTABLE_ADMISSION, 1, "active_row"),
    "restartable_completion": (Boundary.RESTARTABLE_COMPLETION, 1, "first_success"),
    "atomic_completion": (Boundary.ATOMIC_COMPLETION, 1, "seed_success"),
    "restartable_batch": (Boundary.RESTARTABLE_BATCH, 1, "first_batch"),
}


def _predicates(query, schema: str, *, oracle: bool) -> dict:
    prefix = "" if oracle else f"{schema}."

    def count(sql: str, params=()) -> int:
        rows = query(sql) if oracle else query(sql, params)
        return int(rows[0][0]) if rows else 0

    def safe(sql: str) -> int:
        try:
            return count(sql)
        except Exception:
            return 0

    return {
        "marker": lambda: safe(f"SELECT count(*) FROM {prefix}m8_meta"),
        "active_row": lambda: safe(
            f"SELECT count(*) FROM {prefix}m8_history WHERE migration_id = 'create-items' "
            "AND status = 'ACTIVE'"
        ),
        "first_success": lambda: safe(
            f"SELECT count(*) FROM {prefix}m8_history WHERE migration_id = 'create-items' "
            "AND status = 'SUCCESS'"
        ),
        "seed_success": lambda: safe(
            f"SELECT count(*) FROM {prefix}m8_history WHERE migration_id = 'seed-items' "
            "AND status = 'SUCCESS'"
        ),
        "first_batch": lambda: safe(
            f"SELECT count(*) FROM {prefix}m8_progress WHERE prog_key = 'last_id'"
        ),
    }


def _drive(*, config: Path, manifest: Path, proxy: DirectionalProxy, scenario: str,
           branch: str, predicates: dict) -> Interceptor:
    boundary, occurrence, key = SCENARIOS[scenario]
    predicate = predicates[key]
    interceptor = Interceptor(
        proxy, boundary, branch=branch, occurrence=occurrence,
        observe=(lambda: predicate() > 0),
    )
    report = support.migrate_report(
        config, manifest, adapter_hook=interceptor.install
    )
    interceptor.join()
    assert interceptor.fired, f"the {boundary} boundary was never reached"
    assert report.exit_code == Exit.UNKNOWN_OUTCOME, report.message
    assert report.connection_discarded
    assert "unknown outcome" in report.message
    # No cleanup SQL was issued after the unknown outcome.
    assert interceptor.rollbacks_after_latch == 0
    if branch == RESPONSE_WITHHELD:
        # The observer saw the durable state while the runner was still blocked.
        assert interceptor.observed is True, (
            "the commit was supposed to be durable before the response was withheld"
        )
    return interceptor


# --- Oracle ---------------------------------------------------------------------

@pytest.fixture
def oracle_proxy(oracle_project, oracle_settings):
    root, config, schema = oracle_project
    host, rest = oracle_settings["dsn"].split(":", 1)
    port, service = rest.split("/", 1)
    with DirectionalProxy(host, int(port)) as proxy:
        proxied = support.write(
            root / "proxied.toml",
            config.read_text().replace(
                f'dsn = "{oracle_settings["dsn"]}"',
                f'dsn = "{proxy.host}:{proxy.port}/{service}"',
            ),
        )
        os.environ["MIGR8_PASSWORD"] = oracle_settings["password"]
        yield root, config, proxied, schema, proxy


@pytest.mark.oracle
@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("branch", BRANCHES)
def test_oracle_commit_acknowledgement_branches(oracle_proxy, oracle_query, scenario,
                                                branch):
    root, direct, proxied, schema, proxy = oracle_proxy
    manifest = _build_project(
        root, create=ORACLE_CREATE, seed=ORACLE_SEED, batch=ORACLE_BATCH
    )
    predicates = _predicates(oracle_query, schema, oracle=True)
    _drive(config=proxied, manifest=manifest, proxy=proxy, scenario=scenario,
           branch=branch, predicates=predicates)

    key = SCENARIOS[scenario][2]
    if branch == REQUEST_NOT_DELIVERED:
        # The original server session has ended with the transport, so the
        # uncommitted work is gone. Assert absence from an independent session.
        assert _poll(lambda: predicates[key]() == 0), (
            f"{key} should be absent: the commit request never reached the server"
        )
    else:
        assert predicates[key]() > 0

    # A fresh invocation reacquires the lock and reconciles.
    assert support.migrate(direct, manifest) == Exit.OK
    assert oracle_query("SELECT count(*) FROM items WHERE tag = 'EU'") == [(6,)]
    assert oracle_query(
        "SELECT status FROM m8_history ORDER BY seq"
    ) == [("SUCCESS",), ("SUCCESS",), ("SUCCESS",)]
    assert oracle_query("SELECT count(*) FROM m8_progress") == [(0,)]


# --- PostgreSQL -------------------------------------------------------------------

@pytest.fixture
def pg_proxy(pg_project, pg_settings):
    root, config, schema = pg_project
    host = os.environ["MIGR8_PG_HOST"]
    port = int(os.environ["MIGR8_PG_PORT"])
    with DirectionalProxy(host, port) as proxy:
        proxied = support.write(
            root / "proxied.toml",
            config.read_text()
            .replace(f"host={host}", f"host={proxy.host}")
            .replace(f"port={port}", f"port={proxy.port}"),
        )
        os.environ["MIGR8_PASSWORD"] = os.environ["MIGR8_PG_PASSWORD"]
        yield root, config, proxied, schema, proxy


@pytest.mark.postgres
@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("branch", BRANCHES)
def test_postgres_commit_acknowledgement_branches(pg_proxy, pg_query, scenario, branch):
    root, direct, proxied, schema, proxy = pg_proxy
    manifest = _build_project(root, create=PG_CREATE, seed=PG_SEED, batch=PG_BATCH)
    predicates = _predicates(pg_query, schema, oracle=False)
    _drive(config=proxied, manifest=manifest, proxy=proxy, scenario=scenario,
           branch=branch, predicates=predicates)

    key = SCENARIOS[scenario][2]
    if branch == REQUEST_NOT_DELIVERED:
        assert _poll(lambda: predicates[key]() == 0), (
            f"{key} should be absent: the commit request never reached the server"
        )
    else:
        assert predicates[key]() > 0

    assert support.migrate(direct, manifest) == Exit.OK
    assert pg_query("SELECT count(*) FROM items WHERE tag = 'EU'") == [(6,)]
    assert pg_query(
        f"SELECT status FROM {schema}.m8_history ORDER BY seq"
    ) == [("SUCCESS",), ("SUCCESS",), ("SUCCESS",)]
    assert pg_query(f"SELECT count(*) FROM {schema}.m8_progress") == [(0,)]
