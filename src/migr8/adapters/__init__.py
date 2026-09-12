"""Adapter registry.

Adapters are looked up by the explicit ``database.adapter`` name.  There is no
plugin discovery: the supported set is this list.  Optional drivers are imported
lazily so the probe adapter works without them installed.
"""

from __future__ import annotations

from ..config import Config
from ..errors import UnsupportedCapabilityError, UsageError
from .base import Adapter

SUPPORTED = ("oracle", "postgres", "sqlite-probe")


def create(config: Config) -> Adapter:
    name = config.adapter
    if name == "sqlite-probe":
        from .sqlite_probe import SqliteProbeAdapter

        return SqliteProbeAdapter(config)
    if name == "postgres":
        try:
            from .postgres import PostgresAdapter
        except ImportError as exc:
            raise UnsupportedCapabilityError(
                f"the postgres adapter requires psycopg: {exc}"
            ) from exc
        return PostgresAdapter(config)
    if name == "oracle":
        try:
            from .oracle import OracleAdapter
        except ImportError as exc:
            raise UnsupportedCapabilityError(
                f"the oracle adapter requires python-oracledb: {exc}"
            ) from exc
        return OracleAdapter(config)
    raise UsageError(
        f"unsupported database.adapter {name!r}; supported adapters are {', '.join(SUPPORTED)}"
    )


__all__ = ["Adapter", "create", "SUPPORTED"]
