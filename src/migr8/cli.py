"""The ``migr8`` command-line interface (spec Section 11).

Three commands, no more: ``migrate``, ``validate`` and ``status``. There is no
undo, clean, baseline, repair or forced unlock, and adding one would change the
protocol rather than the tool.  ``validate --offline`` is the same command with
its database half removed, not a fourth one: it runs the checks that need only
the plan, so a pipeline can make them before a target exists.

Two behaviours here exist for operations rather than for the specification:

* ``SIGTERM`` is turned into the same interruption path as Ctrl-C, so a
  supervisor stopping the process gets an orderly, correctly classified outcome
  instead of an abrupt kill part-way through a commit.
* every failure, including an unexpected one, leaves a defined exit code and a
  run id rather than a traceback.

One rule holds the command's terminal result together: the value :func:`main`
returns is decided once, before diagnostics are torn down.  Opening the event
log is validated before any database work, and closing it can never replace an
outcome the database already made durable.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import shlex
import signal
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import adapters
from .config import DEFAULT_CONFIG_NAME, Config
from .config import load as load_config
from .diagnostics import RunLog, default_log_path
from .engine import Engine, RunReport
from .errors import OUTCOME_NAMES, Exit, Migr8Error, UsageError, describe_safely
from .manifest import DEFAULT_MANIFEST_NAME
from .manifest import load as load_manifest
from .model import Capture
from .readonly import run_offline, run_status, run_validate
from .staging import capture_in_place, cleanup, stage
from .version import TOOL_NAME, TOOL_VERSION

LOGGER = logging.getLogger("migr8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "Ordered database migration engine. Commands are migrate, validate and "
            "status. There is no undo, clean, baseline, repair or forced unlock."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="log engine progress to stderr"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--config",
            type=Path,
            default=None,
            help=f"configuration file (default ./{DEFAULT_CONFIG_NAME})",
        )
        target.add_argument(
            "--manifest",
            type=Path,
            default=None,
            help=f"manifest file (default ./{DEFAULT_MANIFEST_NAME})",
        )
        target.add_argument(
            "--log-file",
            type=Path,
            default=None,
            metavar="PATH",
            help="append a JSON event log for diagnosis (or set MIGR8_LOG_FILE)",
        )
        target.add_argument(
            "--json", action="store_true", help="emit a machine-readable report on stdout"
        )

    migrate = sub.add_parser("migrate", help="execute the pending migration suffix")
    common(migrate)
    migrate.add_argument(
        "--recover",
        metavar="ID",
        default=None,
        help="admit amended source for the existing ACTIVE restartable migration",
    )

    validate = sub.add_parser("validate", help="check the manifest and history contracts")
    common(validate)
    validate.add_argument(
        "--offline",
        action="store_true",
        help="lint the plan without connecting to a database",
    )
    validate.add_argument(
        "--baseline",
        type=Path,
        default=None,
        metavar="PATH",
        help="a previously approved --json plan; published entries must still match it",
    )
    common(sub.add_parser("status", help="report migration state"))
    return parser


def _resolve(path: Path | None, default_name: str, what: str) -> Path:
    """Resolve a CLI path once, against the process working directory."""
    candidate = Path(path) if path is not None else Path.cwd() / default_name
    resolved = candidate.expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise UsageError(f"{what} {resolved} does not exist or is not a regular file")
    return resolved


def _load(args: argparse.Namespace) -> tuple[Config, Path, Path]:
    config_path = _resolve(args.config, DEFAULT_CONFIG_NAME, "configuration")
    manifest_path = _resolve(args.manifest, DEFAULT_MANIFEST_NAME, "manifest")
    return load_config(config_path), config_path, manifest_path


def _recovery_command(
    args: argparse.Namespace, migration_id: str, config_path: Path, manifest_path: Path
) -> str:
    """The command that admits an amended ACTIVE migration, built for this invocation.

    This is the only place the command is assembled.  When the operator passed
    ``--config`` or ``--manifest``, the command carries both resolved paths, so
    it runs from any directory; the default layout gets the short form.
    """
    command = f"{TOOL_NAME} migrate --recover {shlex.quote(migration_id)}"
    if args.config is not None or args.manifest is not None:
        command += (
            f" --config {shlex.quote(str(config_path))}"
            f" --manifest {shlex.quote(str(manifest_path))}"
        )
    return command


def _install_signal_handlers() -> None:
    """Turn SIGTERM into the interruption path Ctrl-C already takes.

    Without this, a supervisor's SIGTERM ends the process with no unwinding at
    all: a commit in flight would be abandoned with nothing written to the log
    and no exit code an operator could act on.
    """

    def handler(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signal.Signals(signum).name}")

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            # Not the main thread, or a platform without the signal.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


@dataclass(slots=True)
class _MigrateRun:
    """How far ``migrate`` got, so an interrupt reaching :func:`main` is rendered once.

    ``engine`` is set as the engine is entered; ``exit_code`` is set as the
    result starts being written, and nothing is written after that.
    """

    engine: Engine | None = None
    report: RunReport | None = None
    config_path: Path | None = None
    manifest_path: Path | None = None
    exit_code: int | None = None


def _do_migrate(args: argparse.Namespace, log: RunLog, run: _MigrateRun) -> int:
    config, run.config_path, run.manifest_path = _load(args)
    manifest = load_manifest(run.manifest_path)
    adapter = adapters.create(config)
    capture: Capture | None = None
    try:
        capture = stage(manifest)
        engine = Engine(
            config=config,
            adapter=adapter,
            capture=capture,
            recover_id=args.recover,
            log=log,
        )
        run.engine = engine
        # Engine.run ends its own teardown when interrupted; an interrupt that
        # still escapes it propagates to main, which renders from run.engine.
        run.report = engine.run()
    finally:
        # Staging is removed on normal exit and on handled failure.  A crash may
        # leave a temporary directory behind; it is never reused.
        if capture is not None:
            _cleanup_staging(capture.staging_root, run)
    _add_recovery_command(args, run, run.report)
    return _render_run_report(args, run.report, run)


def _add_recovery_command(args: argparse.Namespace, run: _MigrateRun, report: RunReport) -> None:
    if report.recovery_id is None or run.config_path is None or run.manifest_path is None:
        return
    report.recovery_command = _recovery_command(
        args, report.recovery_id, run.config_path, run.manifest_path
    )


def _recorded_report(run: _MigrateRun) -> RunReport | None:
    """The engine's report once it holds the run's outcome, else ``None``."""
    if run.report is not None:
        return run.report
    if run.engine is not None and run.engine.outcome_recorded:
        return run.engine.report
    return None


def _cleanup_staging(root: Path | None, run: _MigrateRun) -> None:
    """Remove the staging directory without letting an interrupt replace the outcome.

    Once the engine was entered, the engine or the interrupt handler in
    :func:`main` decides the command's result.  An interrupt (SIGINT, SIGTERM
    or SIGHUP) during the removal is then retried once; if the removal still
    does not finish, the directory is left behind and the report, when there
    is one, carries a warning.  Before the engine was entered the interrupt
    propagates.
    """
    if run.engine is None:
        cleanup(root)
        return
    for _attempt in range(2):
        try:
            cleanup(root)
            return
        except KeyboardInterrupt:
            continue
    LOGGER.warning("removing the staging directory %s was interrupted", root)
    report = _recorded_report(run)
    if report is not None:
        report.warnings.append(
            f"removing the staging directory {root} was interrupted; it was left behind "
            "and can be deleted"
        )


def _render_run_report(args: argparse.Namespace, report: RunReport, run: _MigrateRun) -> int:
    """Write the engine's report once and return its exit code.

    The report is rendered in full before anything is written.  An interrupt
    during the write stops the writing; the exit code is the report's in every
    case.
    """
    out, err = _format_run_report(args, report)
    run.exit_code = int(report.exit_code)
    _write(out, err)
    return run.exit_code


def _format_run_report(args: argparse.Namespace, report: RunReport) -> tuple[str, str]:
    """The report as the text for stdout and the text for stderr."""
    if args.json:
        return report.to_json() + "\n", ""
    out = [f"applied {migration_id}" for migration_id in report.executed]
    err = [f"warning: {warning}" for warning in report.warnings]
    if report.message:
        err += [report.message, f"run: {report.run_id}"]
    if report.recovery_command:
        err.append(f"recovery: {report.recovery_command}")
    if not report.executed and report.ok:
        out.append("no pending migrations")
    return _lines(out), _lines(err)


def _lines(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


def _write(out: str, err: str) -> None:
    """Write a rendered result, each stream's text in one call.

    An interrupt during the write stops it.  The result is already decided,
    and writing it again would put two reports on the stream.
    """
    try:
        for stream, text in ((sys.stdout, out), (sys.stderr, err)):
            if text:
                stream.write(text)
                stream.flush()
    except KeyboardInterrupt:
        pass


def _do_readonly(args: argparse.Namespace, command: str, log: RunLog) -> int:
    config, config_path, manifest_path = _load(args)
    manifest = load_manifest(manifest_path)
    capture = capture_in_place(manifest)
    adapter = adapters.create(config)
    offline = command == "validate" and args.offline
    baseline = (
        _resolve(args.baseline, "", "baseline plan") if getattr(args, "baseline", None) else None
    )
    log.event(
        "run_start",
        command=command,
        adapter=adapter.name,
        manifest=str(manifest_path),
        units=len(capture.units),
        offline=offline,
    )
    if offline:
        report = run_offline(adapter, capture, baseline=baseline)
    elif command == "status":
        report = run_status(adapter, capture)
    else:
        report = run_validate(adapter, capture, baseline=baseline)
    report.run_id = log.run_id
    if report.recovery_id is not None:
        report.recovery_command = _recovery_command(
            args, report.recovery_id, config_path, manifest_path
        )
    log.event("run_end", command=command, exit_code=report.exit_code, problem=report.problem_kind)
    print(report.to_json() if args.json else report.to_text())
    return report.exit_code


def _render_failure(
    args: argparse.Namespace,
    run_id: str,
    code: Exit,
    message: str,
    *,
    phase: str | None = None,
    migration: str | None = None,
    run: _MigrateRun | None = None,
) -> int:
    """Write one handled command failure once and return its exit code.

    ``--json`` gets a machine-readable record here too, carrying the same run id
    and outcome name the engine's own report uses, so a script does not have to
    parse stderr to learn what happened before the engine took over.
    """
    if args.json:
        record = {
            "exit_code": int(code),
            "outcome": OUTCOME_NAMES[code],
            "run_id": run_id,
            "message": message,
            "phase": phase,
            "failed_migration": migration,
        }
        out, err = json.dumps(record, indent=2) + "\n", ""
    else:
        out, err = "", _lines([f"error: {message}", f"run: {run_id}"])
    if run is not None:
        run.exit_code = int(code)
    _write(out, err)
    return int(code)


def _render_interrupt(args: argparse.Namespace, log: RunLog, run: _MigrateRun) -> int:
    """Render an interrupt that reached :func:`main`, at most once.

    A second interrupt while this runs repeats it once and then gives up
    writing; no interrupt escapes, and the exit code is the one the run
    reached.
    """
    for _attempt in range(2):
        try:
            if run.exit_code is not None:
                return run.exit_code
            return _render_interrupt_once(args, log, run)
        except KeyboardInterrupt:
            continue
    if run.exit_code is not None:
        return run.exit_code
    return int(_interrupt_exit(run))


def _interrupt_exit(run: _MigrateRun) -> Exit:
    report = _recorded_report(run)
    if report is not None:
        return Exit(report.exit_code)
    latched = run.engine.latch.error if run.engine is not None else None
    return Exit(latched.exit_code) if latched is not None else Exit.MIGRATION_FAILED


def _render_interrupt_once(args: argparse.Namespace, log: RunLog, run: _MigrateRun) -> int:
    """Render from the engine's report, then its latched error, then the interruption.

    The text naming work that never began is used only when the engine was
    never entered.
    """
    report = _recorded_report(run)
    if report is not None:
        _add_recovery_command(args, run, report)
        return _render_run_report(args, report, run)
    engine = run.engine
    error = engine.latch.error if engine is not None else None
    if error is not None:
        code = Exit(error.exit_code)
        message, phase, migration = error.report(), error.phase, error.migration_id
        detail = error.message
    elif engine is not None:
        code, phase, migration = Exit.MIGRATION_FAILED, "interrupted", None
        message = (
            "interrupted repeatedly while the run was ending, before it could report. "
            "Run status to confirm the state."
        )
        detail = "interrupted in teardown"
    else:
        code, phase, migration = Exit.MIGRATION_FAILED, "interrupted", None
        message = "interrupted before any migration work began"
        detail = "interrupted"
    with contextlib.suppress(KeyboardInterrupt):
        log.event("run_end", exit_code=int(code), detail=detail, phase=phase)
    return _render_failure(
        args, log.run_id, code, message, phase=phase, migration=migration, run=run
    )


def _open_log(run_id: str, args: argparse.Namespace) -> RunLog:
    """Open the event log, turning a setup failure into a defined usage error.

    This runs before the command connects to anything, so a log path that cannot
    be written fails with an exit code rather than escaping the handlers that
    exist to produce one.
    """
    try:
        return RunLog(run_id, default_log_path(args.log_file))
    except OSError as exc:
        raise UsageError(
            f"the run log cannot be opened: {describe_safely(exc)}", phase="log_setup"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _install_signal_handlers()
    run_id = uuid.uuid4().hex
    try:
        log = _open_log(run_id, args)
    except Migr8Error as exc:
        return _render_failure(args, run_id, exc.exit_code, exc.report(), phase=exc.phase)
    run = _MigrateRun()
    try:
        if args.command == "migrate":
            return _do_migrate(args, log, run)
        return _do_readonly(args, args.command, log)
    except Migr8Error as exc:
        log.event("run_end", exit_code=int(exc.exit_code), detail=exc.message, phase=exc.phase)
        return _render_failure(
            args,
            run_id,
            exc.exit_code,
            exc.report(),
            phase=exc.phase,
            migration=exc.migration_id,
        )
    except KeyboardInterrupt:
        # Once Engine.run is entered, the engine classifies the interruption
        # against the durable state itself; an interrupt that still reaches here
        # is rendered from what the engine recorded.
        return _render_interrupt(args, log, run)
    except Exception as exc:
        # A defect in this tool must still produce an actionable outcome.  The
        # failure is named by type: an exception reaching here may carry driver
        # text, and the traceback certainly does, so both stay at DEBUG.
        described = describe_safely(exc)
        log.event("run_end", exit_code=int(Exit.USAGE), detail=described)
        LOGGER.debug("unexpected failure", exc_info=True)
        return _render_failure(
            args,
            run_id,
            Exit.USAGE,
            f"unexpected failure ({described}). This reached the command's fallback "
            "handler, which cannot say what the database did: the failure may have "
            "arisen while reporting a completed run. Run status to confirm the state.",
            phase="internal_error",
        )
    finally:
        # Diagnostic teardown never replaces an outcome the database already made
        # durable, so a failing close is a warning and not the command's result.
        try:
            log.close()
        except (Exception, KeyboardInterrupt) as exc:
            # An interrupt here arrives after the result is decided, so it must
            # not replace the value being returned.
            LOGGER.warning("the run log did not close cleanly: %s", describe_safely(exc))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
