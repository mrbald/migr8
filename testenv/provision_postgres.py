#!/usr/bin/env python3
"""Create the disposable PostgreSQL test schema.

Uses a dedicated schema inside the test database, separate from ``public``, so
the namespace binding is explicit and cleanup is scoped.
"""

from __future__ import annotations

import os
import sys

import psycopg

SCHEMA = os.environ.get("MIGR8_PG_SCHEMA", "migr8")


def conninfo() -> str:
    """Build the admin connection string.

    ``PG_CONNINFO`` overrides everything, so the same script provisions a
    database reached by a published port, a container IP, or anything else.
    """
    override = os.environ.get("PG_CONNINFO")
    if override:
        return override
    return (
        f"host={os.environ.get('MIGR8_PG_HOST', '127.0.0.1')} "
        f"port={os.environ.get('POSTGRES_HOST_PORT', '15433')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'migr8_test')} "
        f"user={os.environ.get('POSTGRES_USER', 'migr8_test')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', 'migr8_pg_test')}"
    )


def main() -> int:
    with psycopg.connect(conninfo(), autocommit=True) as connection:
        connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        connection.execute(f'CREATE SCHEMA "{SCHEMA}"')
        row = connection.execute(
            "SELECT version(), current_setting('synchronous_commit'), "
            "current_setting('fsync')"
        ).fetchone()
    print(f"provisioned schema {SCHEMA}")
    print(f"server version: {row[0]}")
    print(f"synchronous_commit={row[1]} fsync={row[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
