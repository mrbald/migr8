#!/usr/bin/env python3
"""Print the exact versions and digests that back the acceptance report."""

from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
import sys


def run(args: list[str]) -> str:
    """Stdout, or a marked failure.

    A command that fails must not come back as an empty string: this output is
    the evidence an acceptance claim rests on, and a blank digest reads as "no
    digest exists" rather than "nobody could read one".
    """
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # pragma: no cover
        return f"<unavailable: {exc}>"
    if done.returncode != 0:
        first = next(iter(done.stderr.strip().splitlines()), "no stderr")
        return f"<unavailable: {args[0]} {args[1]} exited {done.returncode}: {first}>"
    return done.stdout.strip() or "<unavailable: no output>"


# Every line this script prints is an acceptance-evidence item. A value that
# could not be read is marked in place and remembered here, so the exit code can
# say the output does not back a claim -- printing "<unavailable>" and exiting 0
# let a caller record a green step over missing evidence.
INCOMPLETE: list[str] = []


def record(label: str, value: str) -> None:
    """Print one evidence line, and remember it when the value is not evidence."""
    if value.startswith("<unavailable") or value.startswith("NOT "):
        INCOMPLETE.append(label.strip())
    print(f"{label}: {value}")


def main() -> int:
    print("# Recorded test environment")
    print()
    print(f"host            : {platform.system()} {platform.release()} {platform.machine()}")
    print(f"python          : {sys.version.split()[0]} ({platform.python_implementation()})")
    print(f"sqlite library  : {sqlite3.sqlite_version}")
    try:
        import oracledb

        record("python-oracledb", oracledb.__version__)
    except ImportError:
        record("python-oracledb", "NOT INSTALLED")
    try:
        import psycopg

        record("psycopg        ", psycopg.__version__)
    except ImportError:
        record("psycopg        ", "NOT INSTALLED")
    record("docker         ", run(["docker", "--version"]))
    print()
    # Read the reference off the running container rather than naming a tag
    # here. compose.yaml pins each image as tag@sha256:..., and a host that
    # pulled that pinned reference has no bare tag to inspect -- which is every
    # clean runner, so the tag lookup reported nothing exactly where the
    # evidence mattered most. This also keeps one source of truth: whatever
    # compose started is what gets recorded.
    for container in ("migr8-oracle", "migr8-postgres"):
        ref = run(["docker", "inspect", container, "--format", "{{.Config.Image}}"])
        print(f"container       : {container}")
        record("  image        ", ref)
        if ref.startswith("<unavailable"):
            record("  architecture ", "<unavailable: no image reference>")
            continue
        record(
            "  architecture ",
            run(["docker", "image", "inspect", ref, "--format", "{{.Architecture}}"]),
        )
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
            record("oracle server  ", str(banner[0] if banner else connection.version))
            print(f"oracle thin mode: {connection.thin}")
    except Exception as exc:
        record("oracle server  ", f"NOT RUN ({type(exc).__name__}: {exc})")

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
            record("postgres server", str(row[0]))
    except Exception as exc:
        record("postgres server", f"NOT RUN ({type(exc).__name__}: {exc})")

    if INCOMPLETE:
        print()
        print(f"INCOMPLETE: could not read {', '.join(INCOMPLETE)}")
        print("This output does not back an acceptance claim.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
