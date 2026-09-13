#!/usr/bin/env python3
"""Print the exact versions and digests that back the acceptance report."""

from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
import sys


def run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception as exc:  # pragma: no cover
        return f"<unavailable: {exc}>"


def main() -> int:
    print("# Recorded test environment")
    print()
    print(f"host            : {platform.system()} {platform.release()} {platform.machine()}")
    print(f"python          : {sys.version.split()[0]} ({platform.python_implementation()})")
    print(f"sqlite library  : {sqlite3.sqlite_version}")
    try:
        import oracledb

        print(f"python-oracledb : {oracledb.__version__}")
    except ImportError:
        print("python-oracledb : NOT INSTALLED")
    try:
        import psycopg

        print(f"psycopg         : {psycopg.__version__}")
    except ImportError:
        print("psycopg         : NOT INSTALLED")
    print(f"docker          : {run(['docker', '--version'])}")
    print()
    for image in ("gvenzl/oracle-free:23.9-slim", "postgres:17.5"):
        digest = run(["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"])
        arch = run(["docker", "image", "inspect", image, "--format", "{{.Architecture}}"])
        print(f"image           : {image}")
        print(f"  digest        : {digest}")
        print(f"  architecture  : {arch}")
    print()

    dsn = f"localhost:{os.environ.get('ORACLE_HOST_PORT', '15210')}/FREEPDB1"
    try:
        import oracledb

        with oracledb.connect(
            user=os.environ.get("ORACLE_TEST_USER", "MIGR8_TEST"),
            password=os.environ.get("ORACLE_TEST_PASSWORD", "migr8_ora_test"),
            dsn=dsn,
        ) as connection:
            banner = connection.cursor().execute("SELECT banner_full FROM v$version").fetchone()
            print(f"oracle server   : {banner[0] if banner else connection.version}")
            print(f"oracle thin mode: {connection.thin}")
    except Exception as exc:
        print(f"oracle server   : NOT RUN ({type(exc).__name__}: {exc})")

    try:
        import psycopg

        info = (
            f"host={os.environ.get('MIGR8_PG_HOST', '127.0.0.1')} "
            f"port={os.environ.get('POSTGRES_HOST_PORT', '15433')} "
            f"dbname={os.environ.get('POSTGRES_DB', 'migr8_test')} "
            f"user={os.environ.get('POSTGRES_USER', 'migr8_test')} "
            f"password={os.environ.get('POSTGRES_PASSWORD', 'migr8_pg_test')}"
        )
        with psycopg.connect(info, autocommit=True) as connection:
            row = connection.execute("SELECT version()").fetchone()
            assert row is not None
            print(f"postgres server : {row[0]}")
    except Exception as exc:
        print(f"postgres server : NOT RUN ({type(exc).__name__}: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
