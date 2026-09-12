"""Report structures and rendering for ``validate`` and ``status`` (spec Section 11.2)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .errors import Exit


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(slots=True)
class MigrationStatus:
    position: int
    id: str
    state: str
    mode: str
    language: str
    current_fingerprint: str
    recorded_fingerprint: str | None = None
    recorded_matches_current: bool | None = None
    first_fingerprint: str | None = None
    first_matches_latest: bool | None = None
    attempt: int | None = None
    started_at: str | None = None
    last_attempt_at: str | None = None
    finished_at: str | None = None
    runner_host: str | None = None
    runner_user: str | None = None
    runner_pid: int | None = None
    db_session: str | None = None
    tool_version: str | None = None
    session_liveness: str | None = None
    session_liveness_detail: str | None = None


@dataclass(slots=True)
class Report:
    command: str
    adapter: str
    server: str
    namespace: str
    metadata_state: str
    initialized: bool
    success_count: int = 0
    pending_count: int = 0
    active_id: str | None = None
    migrations: list[MigrationStatus] = field(default_factory=list)
    problem: str | None = None
    problem_kind: str | None = None
    recovery_command: str | None = None
    exit_code: int = int(Exit.OK)
    notes: list[str] = field(default_factory=list)
    #: Correlation id shared with the event log, so a report and its log line up.
    run_id: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)

    def to_text(self) -> str:
        lines = [
            f"command:   {self.command}",
            f"adapter:   {self.adapter}",
            f"server:    {self.server}",
            f"namespace: {self.namespace}",
            f"metadata:  {self.metadata_state}"
            + ("" if self.initialized else "  (not initialized)"),
        ]
        if self.initialized:
            lines.append(
                f"history:   {self.success_count} successful, "
                f"{self.pending_count} pending"
                + (f", ACTIVE {self.active_id}" if self.active_id else "")
            )
        if self.migrations:
            lines.append("")
            lines.append(
                f"{'pos':>4}  {'state':<8} {'mode':<12} {'lang':<7} {'fp':<5} id"
            )
            for item in self.migrations:
                if item.recorded_matches_current is None:
                    mark = "-"
                elif item.recorded_matches_current:
                    mark = "ok"
                else:
                    mark = "DIFF"
                lines.append(
                    f"{item.position:>4}  {item.state:<8} {item.mode:<12} "
                    f"{item.language:<7} {mark:<5} {item.id}"
                )
                if item.state == "ACTIVE":
                    lines.append(
                        f"        attempt={item.attempt} started={item.started_at} "
                        f"last_attempt={item.last_attempt_at}"
                    )
                    lines.append(
                        f"        recorded={item.recorded_fingerprint}"
                    )
                    lines.append(
                        f"        current ={item.current_fingerprint}"
                    )
                    lines.append(
                        f"        first={item.first_fingerprint} "
                        f"first_matches_latest={item.first_matches_latest}"
                    )
                    if item.db_session:
                        lines.append(
                            f"        session={item.db_session} "
                            f"liveness={item.session_liveness}"
                        )
                        if item.session_liveness_detail:
                            lines.append(f"        note: {item.session_liveness_detail}")
        for note in self.notes:
            lines.append(f"note: {note}")
        if self.problem:
            lines.append("")
            lines.append(f"{self.problem_kind or 'problem'}: {self.problem}")
        if self.recovery_command:
            lines.append(f"recovery: {self.recovery_command}")
        lines.append("")
        if self.run_id:
            lines.append(f"run:  {self.run_id}")
        lines.append(f"exit: {self.exit_code}")
        return "\n".join(lines)


def status_from_row(position: int, unit_id: str, mode: str, language: str,
                    current_fingerprint: str, row) -> MigrationStatus:
    """Build one status entry from a capture position and its history row."""
    if row is None:
        return MigrationStatus(
            position=position,
            id=unit_id,
            state="PENDING",
            mode=mode,
            language=language,
            current_fingerprint=current_fingerprint,
        )
    return MigrationStatus(
        position=position,
        id=unit_id,
        state=str(row.status),
        mode=row.mode,
        language=row.language,
        current_fingerprint=current_fingerprint,
        recorded_fingerprint=row.fingerprint,
        recorded_matches_current=row.fingerprint == current_fingerprint,
        first_fingerprint=row.first_fingerprint,
        first_matches_latest=row.first_fingerprint == row.fingerprint,
        attempt=row.attempt,
        started_at=_iso(row.started_at),
        last_attempt_at=_iso(row.last_attempt_at),
        finished_at=_iso(row.finished_at),
        runner_host=row.runner_host,
        runner_user=row.runner_user,
        runner_pid=row.runner_pid,
        db_session=row.db_session,
        tool_version=row.tool_version,
    )
