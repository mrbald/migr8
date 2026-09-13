"""The ordered TOML manifest (spec Section 3).

Array order is execution order.  Nothing is sorted by filename, number or
timestamp, and display numbers derived from position are never identities.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .errors import ManifestError, UnitError
from .paths import check_relative_path, check_units_disjoint, resolve_unit_dir

MANIFEST_VERSION = 1
DEFAULT_MANIFEST_NAME = "manifest.toml"

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,199}$")

_TOP_LEVEL_KEYS = frozenset({"manifest_version", "migration"})
_MIGRATION_KEYS = frozenset({"id", "path", "language", "mode", "entry", "require_valid"})
_REQUIRED_MIGRATION_KEYS = ("id", "path", "language", "mode", "entry")
_REQUIRED_OBJECT_KEYS = frozenset({"name", "type"})


class Language(StrEnum):
    SQL = "sql"
    PYTHON = "python"


class Mode(StrEnum):
    ATOMIC = "atomic"
    RESTARTABLE = "restartable"


@dataclass(frozen=True, slots=True)
class RequiredObject:
    """One declared final-validity requirement.  The owner is the target schema."""

    type: str
    name: str

    def as_pair(self) -> tuple[str, str]:
        return (self.type, self.name)


@dataclass(frozen=True, slots=True)
class MigrationDef:
    """One manifest entry, resolved but not yet staged or fingerprinted."""

    position: int
    id: str
    raw_path: str
    language: Language
    mode: Mode
    entry: str
    required: tuple[RequiredObject, ...]
    unit_dir: Path

    @property
    def required_pairs(self) -> list[tuple[str, str]]:
        return [obj.as_pair() for obj in self.required]


@dataclass(frozen=True, slots=True)
class Manifest:
    """An immutable in-memory capture of the manifest."""

    path: Path
    directory: Path
    migrations: tuple[MigrationDef, ...]


def load(manifest_path: Path) -> Manifest:
    """Read, structurally validate and resolve a manifest.

    No migration code is imported and no file inside a unit is read beyond
    confirming that each declared entry file exists.
    """
    path = Path(manifest_path)
    if not path.is_file():
        raise ManifestError(f"manifest {path} is not a regular file")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"manifest {path} cannot be read: {exc}") from exc
    try:
        document = tomllib.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid TOML: {exc}") from exc

    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ManifestError(f"manifest {path} has unknown top-level keys: {', '.join(unknown)}")

    if "manifest_version" not in document:
        raise ManifestError(f"manifest {path} is missing manifest_version")
    version = document["manifest_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ManifestError(f"manifest_version must be an integer, got {type(version).__name__}")
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"unsupported manifest_version {version}; this tool supports {MANIFEST_VERSION}"
        )

    entries = document.get("migration", [])
    if not isinstance(entries, list):
        raise ManifestError("[[migration]] must be an array of tables")

    directory = path.parent.resolve(strict=True)
    migrations: list[MigrationDef] = []
    seen_ids: dict[str, int] = {}

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ManifestError(f"migration at position {index} is not a table")
        migration = _build_migration(entry, index, directory)
        previous = seen_ids.get(migration.id)
        if previous is not None:
            raise ManifestError(
                f"duplicate migration id {migration.id!r} at positions {previous} and {index}"
            )
        seen_ids[migration.id] = index
        migrations.append(migration)

    check_units_disjoint([(m.id, m.unit_dir) for m in migrations])
    return Manifest(path=path, directory=directory, migrations=tuple(migrations))


def _build_migration(entry: dict[str, object], position: int, directory: Path) -> MigrationDef:
    unknown = sorted(set(entry) - _MIGRATION_KEYS)
    if unknown:
        raise ManifestError(
            f"migration at position {position} has unknown keys: {', '.join(unknown)}"
        )
    missing = [key for key in _REQUIRED_MIGRATION_KEYS if key not in entry]
    if missing:
        raise ManifestError(
            f"migration at position {position} is missing required keys: {', '.join(missing)}"
        )

    migration_id = _require_str(entry["id"], f"migration at position {position} id")
    if not ID_RE.match(migration_id):
        raise ManifestError(
            f"migration id {migration_id!r} at position {position} does not match "
            r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$"
        )

    raw_path = _require_str(entry["path"], f"migration {migration_id!r} path")
    language = _require_enum(Language, entry["language"], f"migration {migration_id!r} language")
    mode = _require_enum(Mode, entry["mode"], f"migration {migration_id!r} mode")
    entry_file = _require_str(entry["entry"], f"migration {migration_id!r} entry")
    try:
        check_relative_path(entry_file, what=f"migration {migration_id!r} entry")
    except UnitError as exc:
        raise ManifestError(str(exc)) from None

    required = _build_required(entry.get("require_valid", []), migration_id)

    unit_dir = resolve_unit_dir(directory, raw_path, migration_id=migration_id)
    entry_abs = unit_dir / entry_file
    if entry_abs.is_symlink() or not entry_abs.is_file():
        raise ManifestError(
            f"migration {migration_id!r} entry {entry_file!r} is not an existing regular "
            f"file inside {raw_path!r}"
        )

    return MigrationDef(
        position=position,
        id=migration_id,
        raw_path=raw_path,
        language=language,
        mode=mode,
        entry=entry_file,
        required=required,
        unit_dir=unit_dir,
    )


def _build_required(value: object, migration_id: str) -> tuple[RequiredObject, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"migration {migration_id!r} require_valid must be an array of tables")
    objects: list[RequiredObject] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ManifestError(
                f"migration {migration_id!r} require_valid entry {index} is not a table"
            )
        unknown = sorted(set(item) - _REQUIRED_OBJECT_KEYS)
        if unknown:
            raise ManifestError(
                f"migration {migration_id!r} require_valid entry {index} has unknown keys: "
                f"{', '.join(unknown)}"
            )
        missing = sorted(_REQUIRED_OBJECT_KEYS - set(item))
        if missing:
            raise ManifestError(
                f"migration {migration_id!r} require_valid entry {index} is missing: "
                f"{', '.join(missing)}"
            )
        name = _require_str(item["name"], f"migration {migration_id!r} require_valid name")
        obj_type = _require_str(item["type"], f"migration {migration_id!r} require_valid type")
        if not name.strip() or not obj_type.strip():
            raise ManifestError(
                f"migration {migration_id!r} require_valid entry {index} has an empty name or type"
            )
        pair = (obj_type, name)
        if pair in seen:
            raise ManifestError(
                f"migration {migration_id!r} declares duplicate required object {obj_type} {name}"
            )
        seen.add(pair)
        objects.append(RequiredObject(type=obj_type, name=name))
    # Canonical order (spec Section 4.2) so the manifest capture is already sorted.
    return tuple(sorted(objects, key=lambda o: (o.type.encode("utf-8"), o.name.encode("utf-8"))))


def _require_str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{what} must be a string, got {type(value).__name__}")
    return value


def _require_enum[E: StrEnum](enum_cls: type[E], value: object, what: str) -> E:
    text = _require_str(value, what)
    try:
        return enum_cls(text)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_cls)
        raise ManifestError(f"{what} must be one of {allowed}, got {text!r}") from None
