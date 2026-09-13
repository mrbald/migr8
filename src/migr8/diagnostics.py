"""Run diagnostics: a correlation id and an append-only event log (spec Section 11.5).

Records the order things happened in, how long each step took, which identity
and fingerprint were involved, and the phase that failed -- what a failure
diagnosed after the fact needs.

Design rules:

* **One line per event, flushed immediately.** A crash or a kill must leave the
  log usable up to the last thing that happened.
* **Failures are named by code, not by driver text.** The engine reports an
  exception type and an engine error code, with the operation, phase and
  identity around it; the driver's own message quotes the data that produced it
  and is not written here. :data:`REDACTED_FIELDS` drops the field names below
  as a further guard. Author log lines carry whatever the author passes, which
  is the author's responsibility and is documented as such.
* **No database objects.** The log is a file. Adding an audit table would change
  the metadata layout and enlarge the scope the specification fixes.
* **Off by default.** Without ``--log-file`` nothing is written, and the engine
  behaves identically.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import describe_safely

LOGGER = logging.getLogger("migr8.run")

#: Field names dropped from an event, whatever a caller passes.
REDACTED_FIELDS = frozenset({"password", "secret", "dsn", "credential", "params"})


@dataclass(slots=True)
class RunLog:
    """Append-only JSONL event log for one run, plus the run correlation id."""

    run_id: str
    path: Path | None = None
    _handle: Any = field(default=None, init=False, repr=False)
    _started: float = field(default_factory=time.monotonic, init=False)

    def __post_init__(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered append: concurrent runners each write whole lines.
        # Held open for the life of the run and closed by close(); a context
        # manager here would shut the log before the events it exists to record.
        self._handle = open(  # noqa: SIM115
            self.path, "a", encoding="utf-8", buffering=1
        )

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self._started, 4)

    def event(self, name: str, **fields: object) -> None:
        """Record one event.  Safe to call when no log file is configured."""
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "run": self.run_id,
            "event": name,
            "elapsed": self.elapsed,
            **{k: v for k, v in fields.items() if k not in REDACTED_FIELDS},
        }
        LOGGER.info("%s %s", name, _render(payload))
        if self._handle is None:
            return
        try:
            self._handle.write(json.dumps(payload, default=str, sort_keys=False) + "\n")
        except Exception as exc:
            # The log is diagnostic: no correctness rule depends on it, so a log
            # that stops working reports itself once and is abandoned rather
            # than changing what the run does or what it returns.
            LOGGER.warning("cannot write the run log: %s", describe_safely(exc))
            self._handle = None

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


def _render(payload: dict[str, object]) -> str:
    skip = {"ts", "event"}
    return " ".join(f"{k}={v}" for k, v in payload.items() if k not in skip)


def default_log_path(explicit: str | os.PathLike[str] | None) -> Path | None:
    """Resolve ``--log-file``, honouring ``MIGR8_LOG_FILE`` as the fallback."""
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = os.environ.get("MIGR8_LOG_FILE")
    return Path(from_env).expanduser() if from_env else None
