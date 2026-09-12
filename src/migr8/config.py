"""Configuration loading (spec Section 11.4).

Credentials never come from the configuration file.  The password is read from
``MIGR8_PASSWORD``, or external/wallet authentication is used, so a committed
configuration and a log line can never carry one.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

PASSWORD_ENV = "MIGR8_PASSWORD"
DEFAULT_CONFIG_NAME = "migr8.toml"

_TOP_LEVEL_KEYS = frozenset({"database", "lock", "oracle", "postgres", "sqlite"})
_DATABASE_KEYS = frozenset({"adapter", "dsn", "user", "target_schema", "path"})
_LOCK_KEYS = frozenset({"provider", "package", "id", "timeout_seconds"})

#: DBMS_LOCK accepts user lock ids in this range (spec Section 8.1).
LOCK_ID_MIN = 0
LOCK_ID_MAX = 1073741823


@dataclass(frozen=True, slots=True)
class LockConfig:
    provider: str
    timeout_seconds: int
    id: int | None = None
    package: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    path: Path | None
    adapter: str
    dsn: str | None = None
    user: str | None = None
    target_schema: str | None = None
    #: SQLite probe only: the canonical database file path.
    database_path: Path | None = None
    lock: LockConfig = field(default_factory=lambda: LockConfig("", 0))
    #: Adapter-specific sub-table, validated by the adapter itself.
    options: dict[str, object] = field(default_factory=dict)

    def password(self) -> str | None:
        """Return the password from the environment, or ``None`` for external auth."""
        return os.environ.get(PASSWORD_ENV) or None


def load(config_path: Path) -> Config:
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"configuration {path} is not a regular file")
    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration {path} is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration {path} is not valid TOML: {exc}") from exc

    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(
            f"configuration {path} has unknown top-level tables: {', '.join(unknown)}"
        )

    database = document.get("database")
    if not isinstance(database, dict):
        raise ConfigError(f"configuration {path} is missing the [database] table")
    db_unknown = sorted(set(database) - _DATABASE_KEYS)
    if db_unknown:
        raise ConfigError(f"[database] has unknown keys: {', '.join(db_unknown)}")

    adapter = _require_str(database.get("adapter"), "database.adapter")

    dsn = database.get("dsn")
    if dsn is not None:
        dsn = _require_str(dsn, "database.dsn")
    user = database.get("user")
    if user is not None:
        user = _require_str(user, "database.user")
    target_schema = database.get("target_schema")
    if target_schema is not None:
        target_schema = _require_str(target_schema, "database.target_schema")

    database_path = None
    raw_path = database.get("path")
    if raw_path is not None:
        text = _require_str(raw_path, "database.path")
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = (path.parent / candidate).resolve(strict=False)
        database_path = candidate

    lock = _build_lock(document.get("lock"), path)

    options: dict[str, object] = {}
    for table_name in ("oracle", "postgres", "sqlite"):
        section = document.get(table_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ConfigError(f"[{table_name}] must be a table")
        options[table_name] = section

    return Config(
        path=path,
        adapter=adapter,
        dsn=dsn,
        user=user,
        target_schema=target_schema,
        database_path=database_path,
        lock=lock,
        options=options,
    )


def _build_lock(section: object, path: Path) -> LockConfig:
    if section is None:
        raise ConfigError(f"configuration {path} is missing the [lock] table")
    if not isinstance(section, dict):
        raise ConfigError("[lock] must be a table")
    unknown = sorted(set(section) - _LOCK_KEYS)
    if unknown:
        raise ConfigError(f"[lock] has unknown keys: {', '.join(unknown)}")

    provider = _require_str(section.get("provider"), "lock.provider")
    timeout = section.get("timeout_seconds")
    if timeout is None:
        raise ConfigError("lock.timeout_seconds is required")
    timeout = _require_int(timeout, "lock.timeout_seconds")
    if timeout < 0:
        raise ConfigError("lock.timeout_seconds must not be negative")

    lock_id = section.get("id")
    if lock_id is not None:
        lock_id = _require_int(lock_id, "lock.id")
        if not LOCK_ID_MIN <= lock_id <= LOCK_ID_MAX:
            raise ConfigError(
                f"lock.id must be in {LOCK_ID_MIN}..{LOCK_ID_MAX}, got {lock_id}"
            )
    package = section.get("package")
    if package is not None:
        package = _require_str(package, "lock.package")
    return LockConfig(provider=provider, timeout_seconds=timeout, id=lock_id, package=package)


def _require_str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{what} must be a non-empty string")
    return value


def _require_int(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{what} must be an integer")
    return value
