"""The ``migr8`` command-line interface (spec Section 11).

Three commands, no more: ``migrate``, ``validate`` and ``status``. There is no
undo, clean, baseline, repair or forced unlock, and adding one would change the
protocol rather than the tool.

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
import signal
import sys
import uuid
from pathlib import Path

from . import adapters
from .config import DEFAULT_CONFIG_NAME, Config
from .config import load as load_config
from .diagnostics import RunLog, default_log_path
from .engine import Engine
from .errors import OUTCOME_NAMES, Exit, Migr8Error, UsageError, describe_safely
from .manifest import DEFAULT_MANIFEST_NAME
from .manifest import load as load_manifest
from .model import Capture
from .readonly import run_status, run_validate
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

    common(sub.add_parser("validate", help="check the manifest and history contracts"))
    common(sub.add_parser("status", help="report migration state"))
    return parser


def _resolve(path: Path | None, default_name: str, what: str) -> Path:
    """Resolve a CLI path once, against the process working directory."""
    candidate = Path(path) if path is not None else Path.cwd() / default_name
    resolved = candidate.expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise UsageError(f"{what} {resolved} does not exist or is not a regular file")
    return resolved


def _load(args: argparse.Namespace) -> tuple[Config, Path]:
    config_path = _resolve(args.config, DEFAULT_CONFIG_NAME, "configuration")
    manifest_path = _resolve(args.manifest, DEFAULT_MANIFEST_NAME, "manifest")
    return load_config(config_path), manifest_path


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


def _do_migrate(args: argparse.Namespace, log: RunLog) -> int:
    config, manifest_path = _load(args)
    manifest = load_manifest(manifest_path)
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
        report = engine.run()
    finally:
        # Staging is removed on normal exit and on handled failure.  A crash may
        # leave a temporary directory behind; it is never reused.
        if capture is not None:
            cleanup(capture.staging_root)

    if args.json:
        print(report.to_json())
        return int(report.exit_code)

    for migration_id in report.executed:
        print(f"applied {migration_id}", flush=True)
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    if report.message:
        print(report.message, file=sys.stderr, flush=True)
        print(f"run: {report.run_id}", file=sys.stderr, flush=True)
    if report.recovery_command:
        print(f"recovery: {report.recovery_command}", file=sys.stderr, flush=True)
    if not report.executed and report.ok:
        print("no pending migrations")
    return int(report.exit_code)


def _do_readonly(args: argparse.Namespace, command: str, log: RunLog) -> int:
    config, manifest_path = _load(args)
    manifest = load_manifest(manifest_path)
    capture = capture_in_place(manifest)
    adapter = adapters.create(config)
    log.event(
        "run_start",
        command=command,
        adapter=adapter.name,
        manifest=str(manifest_path),
        units=len(capture.units),
    )
    runner = run_status if command == "status" else run_validate
    report = runner(adapter, capture)
    report.run_id = log.run_id
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
) -> int:
    """Render one handled command failure and return its exit code.

    ``--json`` gets a machine-readable record here too, carrying the same run id
    and outcome name the engine's own report uses, so a script does not have to
    parse stderr to learn what happened before the engine took over.
    """
    if args.json:
        print(
            json.dumps(
                {
                    "exit_code": int(code),
                    "outcome": OUTCOME_NAMES[code],
                    "run_id": run_id,
                    "message": message,
                    "phase": phase,
                    "failed_migration": migration,
                },
                indent=2,
            )
        )
    else:
        print(f"error: {message}", file=sys.stderr)
        print(f"run: {run_id}", file=sys.stderr)
    return int(code)


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
    try:
        if args.command == "migrate":
            return _do_migrate(args, log)
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
        # Reached only before the engine owns the run; once it does, it classifies
        # the interruption against the durable state itself.
        log.event("run_end", exit_code=int(Exit.MIGRATION_FAILED), detail="interrupted")
        return _render_failure(
            args,
            run_id,
            Exit.MIGRATION_FAILED,
            "interrupted before any migration work began",
            phase="interrupted",
        )
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
        except Exception as exc:  # pragma: no cover - exercised with an injected handle
            LOGGER.warning("the run log did not close cleanly: %s", describe_safely(exc))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
