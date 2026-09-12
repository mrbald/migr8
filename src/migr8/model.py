"""Durable state model and the in-memory capture of a manifest (spec Sections 2.1, 8.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .manifest import Language, Manifest, MigrationDef, Mode

LAYOUT_VERSION = 1

HISTORY_TABLE = "m8_history"
PROGRESS_TABLE = "m8_progress"
META_TABLE = "m8_meta"
ACTIVE_INDEX = "m8_history_one_active"

#: Names the engine reserves.  Migration code must not touch these objects
#: except through the supplied progress API.
RESERVED_OBJECT_NAMES = frozenset({HISTORY_TABLE, PROGRESS_TABLE, META_TABLE, ACTIVE_INDEX})

META_SINGLETON_KEY = "singleton"

PROGRESS_KEY_MAX_CHARS = 128
PROGRESS_VALUE_MAX_BYTES = 4000


class Status(StrEnum):
    ACTIVE = "ACTIVE"
    SUCCESS = "SUCCESS"


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One ``m8_history`` row as read from the database."""

    seq: int
    migration_id: str
    fingerprint: str
    first_fingerprint: str
    language: str
    mode: str
    status: str
    attempt: int | None
    started_at: datetime | None
    last_attempt_at: datetime | None
    finished_at: datetime | None
    runner_host: str | None = None
    runner_user: str | None = None
    runner_pid: int | None = None
    db_session: str | None = None
    tool_version: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == Status.SUCCESS

    @property
    def is_active(self) -> bool:
        return self.status == Status.ACTIVE


@dataclass(frozen=True, slots=True)
class ProgressRow:
    migration_id: str
    prog_key: str
    prog_value: str
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MetaRow:
    """The ``m8_meta`` singleton: the initialization-complete marker."""

    layout_version: int
    adapter: str
    lock_provider: str
    lock_binding: str
    target_namespace: str
    initialized_at: datetime | None = None


class MetadataState(StrEnum):
    #: No metadata object exists at all.
    ABSENT = "absent"
    #: Some objects exist, all compatible, history and progress empty, no marker.
    INCOMPLETE_COMPATIBLE = "incomplete_compatible"
    #: Marker present and the full layout verified.
    COMPLETE = "complete"
    #: Anything else: missing object after the marker, incompatible definition,
    #: or populated history without a marker.
    DAMAGED = "damaged"


@dataclass(frozen=True, slots=True)
class MetadataReport:
    """What an inspection of the namespace found, before any mutation."""

    state: MetadataState
    meta: MetaRow | None = None
    #: Human-readable reasons, always populated for DAMAGED.
    problems: tuple[str, ...] = ()
    #: Logical object names found to exist.
    present_objects: tuple[str, ...] = ()
    missing_objects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A consistent read of the namespace's metadata rows."""

    history: tuple[HistoryRow, ...]
    progress: tuple[ProgressRow, ...]
    meta: MetaRow | None = None

    def active(self) -> HistoryRow | None:
        actives = [row for row in self.history if row.is_active]
        return actives[0] if len(actives) == 1 else None

    def by_id(self, migration_id: str) -> HistoryRow | None:
        for row in self.history:
            if row.migration_id == migration_id:
                return row
        return None


@dataclass(frozen=True, slots=True)
class CapturedUnit:
    """One manifest entry together with its fingerprinted source location.

    ``source_dir`` is the staged copy during ``migrate`` and the working-tree
    unit during ``validate``/``status``.  ``staged`` records which, because the
    spec permits fingerprinting in place only for the read-only commands.
    """

    definition: MigrationDef
    source_dir: Path
    fingerprint: str
    relpaths: tuple[str, ...]
    staged: bool

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def position(self) -> int:
        return self.definition.position

    @property
    def language(self) -> Language:
        return self.definition.language

    @property
    def mode(self) -> Mode:
        return self.definition.mode


@dataclass(frozen=True, slots=True)
class Capture:
    """An immutable capture of the whole manifest plus its fingerprints."""

    manifest: Manifest
    units: tuple[CapturedUnit, ...]
    staged: bool
    staging_root: Path | None = None

    def at_position(self, position: int) -> CapturedUnit | None:
        if 1 <= position <= len(self.units):
            return self.units[position - 1]
        return None

    def by_id(self, migration_id: str) -> CapturedUnit | None:
        for unit in self.units:
            if unit.id == migration_id:
                return unit
        return None


@dataclass(frozen=True, slots=True)
class Plan:
    """The validated result of comparing a capture against durable state."""

    capture: Capture
    snapshot: Snapshot
    #: Number of leading migrations already durably successful.
    success_count: int
    #: The single ACTIVE row, if any.
    active: HistoryRow | None
    #: Units still to execute, in order, starting with the ACTIVE one.
    pending: tuple[CapturedUnit, ...] = field(default_factory=tuple)
    #: True when the ACTIVE row's recorded fingerprint differs from the capture.
    active_fingerprint_changed: bool = False
