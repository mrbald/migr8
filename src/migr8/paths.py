"""Path rules for migration units (spec Sections 3.1 and 4.2).

These rules exist so that a fingerprint computed on one machine means the same
thing on another.  They are deliberately strict: anything that cannot be
represented identically as a sorted list of ``/``-separated NFC byte strings is
rejected rather than normalised.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ManifestError, UnitError

#: Directory and file names that must never appear inside a unit.  These are
#: build artefacts of the tooling itself; their presence means the unit's
#: fingerprint would depend on whether tests or an editor had run.
FORBIDDEN_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})
FORBIDDEN_FILE_SUFFIXES = (".pyc", ".pyo")


def check_relative_path(text: str, *, what: str = "path") -> PurePosixPath:
    """Validate a unit-relative path string and return it as a POSIX path.

    Raises :class:`UnitError` describing the first rule broken.
    """
    if not text:
        raise UnitError(f"{what} must not be empty")
    if "\\" in text:
        raise UnitError(f"{what} {text!r} must not contain a backslash")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise UnitError(f"{what} {text!r} must not contain control characters")
    if text.startswith("/"):
        raise UnitError(f"{what} {text!r} must be relative, not absolute")
    if unicodedata.normalize("NFC", text) != text:
        raise UnitError(f"{what} {text!r} must already be Unicode NFC")
    parts = text.split("/")
    for part in parts:
        if part == "":
            raise UnitError(f"{what} {text!r} must not contain empty components")
        if part in (".", ".."):
            raise UnitError(f"{what} {text!r} must not contain '.' or '..' components")
        if part != part.strip(" "):
            raise UnitError(
                f"{what} {text!r} must not have leading or trailing spaces in a component"
            )
    return PurePosixPath(text)


def check_no_case_collisions(relpaths: list[str]) -> None:
    """Reject two unit files whose paths differ only by case.

    A case-insensitive filesystem cannot hold both, so a unit containing such a
    pair would stage differently depending on the host.
    """
    seen: dict[str, str] = {}
    for rel in relpaths:
        folded = rel.casefold()
        other = seen.get(folded)
        if other is not None:
            raise UnitError(
                f"unit contains case-folding collision between {other!r} and {rel!r}"
            )
        seen[folded] = rel


def resolve_unit_dir(manifest_dir: Path, raw_path: str, *, migration_id: str) -> Path:
    """Resolve a manifest ``path`` field to a real directory under the manifest tree."""
    # The value came from the manifest, so a rule violation is a manifest error.
    try:
        check_relative_path(raw_path, what=f"migration {migration_id!r} path")
    except UnitError as exc:
        raise ManifestError(str(exc)) from None

    root = manifest_dir.resolve(strict=True)
    candidate = manifest_dir / raw_path
    # Walk each component so a symlinked intermediate directory is refused
    # rather than silently followed out of the manifest tree.
    current = root
    for part in PurePosixPath(raw_path).parts:
        current = current / part
        lst = _lstat_or_fail(current, migration_id)
        if stat.S_ISLNK(lst.st_mode):
            raise ManifestError(
                f"migration {migration_id!r} path component {current.name!r} is a symlink; "
                "symlinked unit roots are rejected"
            )
    resolved = candidate.resolve(strict=False)
    if not resolved.is_dir():
        raise ManifestError(
            f"migration {migration_id!r} path {raw_path!r} is not an existing directory"
        )
    if resolved == root:
        raise ManifestError(
            f"migration {migration_id!r} path {raw_path!r} must not be the manifest directory itself"
        )
    if root not in resolved.parents:
        raise ManifestError(
            f"migration {migration_id!r} path {raw_path!r} escapes the manifest tree"
        )
    return resolved


def _lstat_or_fail(path: Path, migration_id: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ManifestError(
            f"migration {migration_id!r} path component {str(path)!r} is not accessible: {exc}"
        ) from exc


def check_units_disjoint(units: list[tuple[str, Path]]) -> None:
    """Reject overlapping units: equal paths, or one containing another."""
    for i, (id_a, path_a) in enumerate(units):
        for id_b, path_b in units[i + 1:]:
            if path_a == path_b:
                raise ManifestError(
                    f"migrations {id_a!r} and {id_b!r} resolve to the same unit directory {path_a}"
                )
            if path_a in path_b.parents:
                raise ManifestError(
                    f"unit of migration {id_b!r} is contained in the unit of {id_a!r}"
                )
            if path_b in path_a.parents:
                raise ManifestError(
                    f"unit of migration {id_a!r} is contained in the unit of {id_b!r}"
                )


@dataclass(frozen=True, slots=True)
class UnitFile:
    """One regular file inside a unit."""

    relpath: str
    abspath: Path


def scan_unit(root: Path, *, migration_id: str) -> list[UnitFile]:
    """Return every regular file in ``root``, sorted by UTF-8 bytes of its relpath.

    Rejects symlinks, special files, forbidden build artefacts, unit-relative
    path-rule violations, case-folding collisions and empty units.
    """
    files: list[UnitFile] = []
    _scan_dir(root, root, files, migration_id)
    if not files:
        raise UnitError(f"migration {migration_id!r} unit {root} contains no files")
    files.sort(key=lambda f: f.relpath.encode("utf-8"))
    check_no_case_collisions([f.relpath for f in files])
    return files


def _scan_dir(root: Path, current: Path, out: list[UnitFile], migration_id: str) -> None:
    for entry in sorted(os.scandir(current), key=lambda e: e.name):
        rel = str(PurePosixPath(Path(entry.path).relative_to(root).as_posix()))
        if entry.is_symlink():
            raise UnitError(
                f"migration {migration_id!r} unit contains symlink {rel!r}; symlinks are rejected"
            )
        mode = entry.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            if entry.name in FORBIDDEN_DIR_NAMES:
                raise UnitError(
                    f"migration {migration_id!r} unit contains forbidden directory {rel!r}"
                )
            check_relative_path(rel, what=f"migration {migration_id!r} unit path")
            _scan_dir(root, Path(entry.path), out, migration_id)
        elif stat.S_ISREG(mode):
            if entry.name.endswith(FORBIDDEN_FILE_SUFFIXES):
                raise UnitError(
                    f"migration {migration_id!r} unit contains forbidden bytecode file {rel!r}"
                )
            check_relative_path(rel, what=f"migration {migration_id!r} unit path")
            out.append(UnitFile(relpath=rel, abspath=Path(entry.path)))
        else:
            raise UnitError(
                f"migration {migration_id!r} unit contains special file {rel!r}; "
                "only regular files and directories are accepted"
            )
