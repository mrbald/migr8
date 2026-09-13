"""Adapter registry.

Adapters are looked up by the explicit ``database.adapter`` name.  There is no
plugin discovery: the supported set is this list.  Optional drivers are imported
lazily so the SQLite adapter works without either database driver installed.
"""

from __future__ import annotations

from ..config import Config
from ..errors import UnsupportedCapabilityError, UsageError
from .base import Adapter

SUPPORTED = ("oracle", "postgres", "sqlite")

#: Names this tool once used, and what an operator has to do about each.  A
#: retired name is refused by name rather than falling through to the generic
#: "unsupported adapter" message: the adapter identity is recorded in ``m8_meta``
#: and is part of the namespace binding, so silently accepting the old spelling
#: would bind a namespace under a name the tool no longer writes.
RETIRED = {
    "sqlite-probe": (
        "the sqlite-probe adapter is now 'sqlite'. Change database.adapter to "
        "'sqlite'. A namespace initialized by sqlite-probe records that name in "
        "m8_meta and is not adopted: this release does not repair or rewrite "
        "recorded metadata, so migrate a namespace that was initialized under the "
        "old name from its own source of truth."
    ),
}


def create(config: Config) -> Adapter:
    name = config.adapter
    if name in RETIRED:
        raise UsageError(RETIRED[name])
    if name == "sqlite":
        from .sqlite import SqliteAdapter

        return SqliteAdapter(config)
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


__all__ = ["RETIRED", "SUPPORTED", "Adapter", "create"]
