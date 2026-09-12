"""Error taxonomy and process exit codes (spec Sections 7 and 11.3).

Every failure the engine reports maps onto exactly one exit code.  The
distinction between an ordinary failure, an unknown operation outcome and a
detected transaction-contract violation is a load-bearing part of the
recovery protocol, so those three live in separate exception types and are
never collapsed into a generic error.
"""

from __future__ import annotations

from enum import IntEnum


class Exit(IntEnum):
    """Spec Section 11.3."""

    OK = 0
    USAGE = 1
    VALIDATION = 2
    MIGRATION_FAILED = 3
    UNKNOWN_OUTCOME = 4
    LOCK_NOT_ACQUIRED = 5
    NOT_INITIALIZED = 6
    METADATA_DAMAGED = 7
    CONTRACT_VIOLATION = 8


class Migr8Error(Exception):
    """Base class for every failure the CLI turns into an exit code."""

    exit_code: Exit = Exit.USAGE

    def __init__(self, message: str, *, phase: str | None = None,
                 migration_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.phase = phase
        self.migration_id = migration_id

    def report(self) -> str:
        parts = []
        if self.phase:
            parts.append(f"phase={self.phase}")
        if self.migration_id:
            parts.append(f"migration={self.migration_id}")
        suffix = f" [{', '.join(parts)}]" if parts else ""
        return f"{self.message}{suffix}"


class UsageError(Migr8Error):
    """Usage, configuration, unsupported capability, connection or binding error."""

    exit_code = Exit.USAGE


class ConfigError(UsageError):
    pass


class UnsupportedCapabilityError(UsageError):
    """The adapter cannot honour something the manifest or migration asked for."""


class ValidationError(Migr8Error):
    """Manifest / history / source validation failure, including recovery admission."""

    exit_code = Exit.VALIDATION


class ManifestError(ValidationError):
    pass


class UnitError(ValidationError):
    """A staged unit violates the path, content or fingerprint-input rules."""


class SqlSyntaxError(ValidationError):
    """The lexical scanner refused to classify a statement (spec Section 5.3)."""


class RecoveryRequiredError(ValidationError):
    """An ACTIVE migration's source changed; `migrate --recover ID` is required."""


class MigrationFailedError(Migr8Error):
    """Ordinary migration failure: atomic work rolled back, or ACTIVE retained."""

    exit_code = Exit.MIGRATION_FAILED


class UnknownOutcomeError(Migr8Error):
    """A commit-capable operation's outcome is unknown (spec Section 7.2).

    Deliberately an ``Exception`` and not a ``BaseException``: the spec
    anticipates that author code may catch this accidentally, and requires the
    run latch -- not Python's exception hierarchy -- to prevent continuation.
    """

    exit_code = Exit.UNKNOWN_OUTCOME

    def __init__(self, message: str, *, operation: str, phase: str | None = None,
                 migration_id: str | None = None) -> None:
        super().__init__(message, phase=phase, migration_id=migration_id)
        self.operation = operation

    def report(self) -> str:
        return f"unknown outcome for {self.operation}: {super().report()}"


class LockNotAcquiredError(Migr8Error):
    """The namespace lock was not obtained within the configured policy."""

    exit_code = Exit.LOCK_NOT_ACQUIRED


class NotInitializedError(Migr8Error):
    """The namespace has no migration metadata; read-only commands only."""

    exit_code = Exit.NOT_INITIALIZED


class MetadataDamagedError(Migr8Error):
    """Metadata is missing, incompatible or inconsistent with the supported layout."""

    exit_code = Exit.METADATA_DAMAGED


class ContractViolationError(Migr8Error):
    """A detected transaction-contract violation (spec Section 5.1).

    Durable effects may require manual remediation; this is never reported as
    a clean rollback.
    """

    exit_code = Exit.CONTRACT_VIOLATION
