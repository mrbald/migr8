"""Capturing and staging migration units (spec Section 4.3).

``migrate`` stages every unit into a private per-run directory and fingerprints
the staged copies, so a working-tree edit made after staging cannot change the
attempt.  ``validate`` and ``status`` hash the working tree in place: they
import nothing and execute nothing, and their result describes the artifact
they inspected.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .errors import UnitError
from .fingerprint import FingerprintInput, compute
from .manifest import Manifest, MigrationDef
from .model import Capture, CapturedUnit
from .paths import scan_unit

STAGING_PREFIX = "migr8-stage-"


def _fingerprint_unit(definition: MigrationDef, root: Path) -> tuple[str, tuple[str, ...]]:
    files = scan_unit(root, migration_id=definition.id)
    relpaths = [item.relpath for item in files]
    if definition.entry not in relpaths:
        raise UnitError(
            f"migration {definition.id!r} entry {definition.entry!r} is not a regular file "
            f"inside its unit"
        )
    payload = [(item.relpath, item.abspath.read_bytes()) for item in files]
    fingerprint = compute(
        FingerprintInput(
            language=definition.language.value,
            mode=definition.mode.value,
            entry=definition.entry,
            required=definition.required_pairs,
            files=payload,
        )
    )
    return fingerprint, tuple(relpaths)


def capture_in_place(manifest: Manifest) -> Capture:
    """Fingerprint the working-tree units without staging, importing or executing."""
    units = []
    for definition in manifest.migrations:
        fingerprint, relpaths = _fingerprint_unit(definition, definition.unit_dir)
        units.append(
            CapturedUnit(
                definition=definition,
                source_dir=definition.unit_dir,
                fingerprint=fingerprint,
                relpaths=relpaths,
                staged=False,
            )
        )
    return Capture(manifest=manifest, units=tuple(units), staged=False)


def _staged_dir_name(definition: MigrationDef) -> str:
    """A bounded, injective directory name for one staged unit.

    The position alone identifies the unit: it is unique within a manifest, and
    the directory is private to one run.  Hex-encoding the id instead would be
    injective too, but it doubles a length the manifest allows to reach 200
    characters, and a 400-character path component exceeds the limit on every
    filesystem this runs on.  The name is internal and is not fingerprinted.
    """
    return f"{definition.position:05d}"


def stage(manifest: Manifest, *, parent: Path | None = None) -> Capture:
    """Copy every unit into a fresh private directory and fingerprint the copies.

    Copying refuses symlinks and special files rather than following them.  The
    caller must keep the input artifact stable for the duration of the capture;
    this is not an atomic filesystem snapshot.
    """
    root = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent))
    try:
        units = []
        for definition in manifest.migrations:
            target = root / _staged_dir_name(definition)
            _copy_unit(definition, target)
            fingerprint, relpaths = _fingerprint_unit(definition, target)
            units.append(
                CapturedUnit(
                    definition=definition,
                    source_dir=target,
                    fingerprint=fingerprint,
                    relpaths=relpaths,
                    staged=True,
                )
            )
        _make_read_only(root)
        return Capture(manifest=manifest, units=tuple(units), staged=True, staging_root=root)
    except BaseException:
        cleanup(root)
        raise


def _copy_unit(definition: MigrationDef, target: Path) -> None:
    files = scan_unit(definition.unit_dir, migration_id=definition.id)
    target.mkdir(parents=True, exist_ok=False)
    for item in files:
        destination = target / item.relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        # ``scan_unit`` already refused symlinks and special files; open the
        # source without following links so a race cannot substitute one.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(item.abspath, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise UnitError(
                    f"migration {definition.id!r} unit file {item.relpath!r} is not a regular file"
                )
            with open(fd, "rb", closefd=False) as source, open(destination, "wb") as sink:
                shutil.copyfileobj(source, sink)
        finally:
            os.close(fd)


def _make_read_only(root: Path) -> None:
    """Mark staged files read-only as a guardrail.

    This is not a security boundary against trusted Python; it catches an
    author who accidentally writes into the staged tree.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            with contextlib.suppress(OSError):
                path.chmod(0o444)


def cleanup(root: Path | None) -> None:
    """Remove a staging directory.  Safe to call more than once."""
    if root is None:
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            with contextlib.suppress(OSError):
                (Path(dirpath) / name).chmod(0o644)
    shutil.rmtree(root, ignore_errors=True)
