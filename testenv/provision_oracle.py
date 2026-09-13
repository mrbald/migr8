#!/usr/bin/env python3
"""Create the disposable Oracle test schemas.

Runs as SYS against the explicitly identified local test instance only. It
creates two users:

* the main test schema, where the connect user and the target schema are the
  same; and
* a second fixture pair that exercises a connect user whose target schema is a
  different owner, reached either by privilege or by proxy authentication.

``DBMS_LOCK`` execute is granted directly, which is what the engine requires.
"""

from __future__ import annotations

import os
import sys

import oracledb

#: ``ORACLE_DSN`` lets the same script provision a database reached any way at
#: all: a published port, a container IP, or a remote disposable instance.
DSN = os.environ.get(
    "ORACLE_DSN", f"localhost:{os.environ.get('ORACLE_HOST_PORT', '15210')}/FREEPDB1"
)
SYS_PASSWORD = os.environ.get("ORACLE_SYS_PASSWORD", "migr8_sys_test")
TEST_USER = os.environ.get("ORACLE_TEST_USER", "MIGR8_TEST").upper()
TEST_PASSWORD = os.environ.get("ORACLE_TEST_PASSWORD", "migr8_ora_test")

#: Connect user for the separate-schema fixture, plus the schema it targets.
RUNNER_USER = f"{TEST_USER}_RUNNER"
OWNER_USER = f"{TEST_USER}_OWNER"

BASE_GRANTS = (
    "CREATE SESSION",
    "CREATE TABLE",
    "CREATE VIEW",
    "CREATE SEQUENCE",
    "CREATE PROCEDURE",
    "CREATE TYPE",
    "CREATE TRIGGER",
    "CREATE LIBRARY",
)


def execute(cursor, statement: str, *, ignore: tuple[int, ...] = ()) -> None:
    try:
        cursor.execute(statement)
    except oracledb.DatabaseError as exc:
        (error,) = exc.args
        if error.code in ignore:
            return
        raise RuntimeError(f"{statement}\n  -> ORA-{error.code:05d}: {error.message}") from None


def drop_user(cursor, name: str) -> None:
    # ORA-01918: user does not exist
    execute(cursor, f"DROP USER {name} CASCADE", ignore=(1918,))


def create_user(cursor, name: str, password: str, *, quota: bool = True) -> None:
    execute(cursor, f'CREATE USER {name} IDENTIFIED BY "{password}"')
    if quota:
        execute(cursor, f"ALTER USER {name} QUOTA UNLIMITED ON USERS")
    for privilege in BASE_GRANTS:
        execute(cursor, f"GRANT {privilege} TO {name}")
    execute(cursor, f"GRANT EXECUTE ON SYS.DBMS_LOCK TO {name}")


def main() -> int:
    with oracledb.connect(
        user="SYS", password=SYS_PASSWORD, dsn=DSN, mode=oracledb.AUTH_MODE_SYSDBA
    ) as connection:
        banner = connection.version
        cursor = connection.cursor()
        for name in (TEST_USER, RUNNER_USER, OWNER_USER):
            drop_user(cursor, name)

        create_user(cursor, TEST_USER, TEST_PASSWORD)
        # The main test user can read V$SESSION so the optional session-liveness
        # diagnostic is exercised. The RUNNER user deliberately cannot, so the
        # "report unknown without privileges" path is exercised too.
        execute(cursor, f"GRANT SELECT ON SYS.V_$SESSION TO {TEST_USER}")
        execute(cursor, f"GRANT SELECT ON SYS.V_$INSTANCE TO {TEST_USER}")

        # Separate connect-user / target-schema fixture: the runner holds no
        # CREATE TABLE of its own but may create objects in the owner schema.
        execute(cursor, f'CREATE USER {RUNNER_USER} IDENTIFIED BY "{TEST_PASSWORD}"')
        execute(cursor, f"GRANT CREATE SESSION TO {RUNNER_USER}")
        execute(cursor, f"GRANT EXECUTE ON SYS.DBMS_LOCK TO {RUNNER_USER}")
        create_user(cursor, OWNER_USER, TEST_PASSWORD)
        execute(cursor, f"GRANT CREATE ANY TABLE TO {RUNNER_USER}")
        execute(cursor, f"GRANT CREATE ANY INDEX TO {RUNNER_USER}")
        execute(cursor, f"GRANT INSERT ANY TABLE TO {RUNNER_USER}")
        execute(cursor, f"GRANT UPDATE ANY TABLE TO {RUNNER_USER}")
        execute(cursor, f"GRANT DELETE ANY TABLE TO {RUNNER_USER}")
        execute(cursor, f"GRANT SELECT ANY TABLE TO {RUNNER_USER}")
        execute(cursor, f"GRANT DROP ANY TABLE TO {RUNNER_USER}")
        execute(cursor, f"GRANT CREATE ANY PROCEDURE TO {RUNNER_USER}")
        execute(cursor, f"GRANT ALTER ANY PROCEDURE TO {RUNNER_USER}")
        execute(cursor, f"GRANT ALTER SESSION TO {RUNNER_USER}")
        execute(cursor, f"GRANT ALTER SESSION TO {TEST_USER}")
        execute(cursor, f"GRANT ALTER SESSION TO {OWNER_USER}")
        # Proxy authentication: the runner may connect *as* the owner, which is
        # the shape a deployment uses when the migration identity is a personal
        # or certificate-held account and the objects belong to a schema owner.
        execute(cursor, f"ALTER USER {OWNER_USER} GRANT CONNECT THROUGH {RUNNER_USER}")
        connection.commit()
    print(f"provisioned {TEST_USER}, {RUNNER_USER} -> {OWNER_USER} on {DSN}")
    print(f"server version: {banner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
