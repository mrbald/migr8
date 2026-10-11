# migr8

[![tests](https://github.com/mrbald/migr8/actions/workflows/ci.yml/badge.svg)](https://github.com/mrbald/migr8/actions/workflows/ci.yml)
[![vulnerabilities](https://github.com/mrbald/migr8/actions/workflows/audit.yml/badge.svg)](https://github.com/mrbald/migr8/actions/workflows/audit.yml)
[![secrets](https://github.com/mrbald/migr8/actions/workflows/secrets.yml/badge.svg)](https://github.com/mrbald/migr8/actions/workflows/secrets.yml)
[![PyPI](https://img.shields.io/pypi/v/migr8)](https://pypi.org/project/migr8/)
[![Python](https://img.shields.io/pypi/pyversions/migr8)](https://pypi.org/project/migr8/)
[![license](https://img.shields.io/badge/license-AGPL--3.0%20%7C%20commercial-blue)](LICENSING.md)

migr8 is a database migration engine for Oracle, PostgreSQL and SQLite. It
executes SQL and Python migrations in the order declared by a TOML manifest
and records their identities, source fingerprints and execution state in the
target database. It supports atomic execution, restartable batches and
explicit roll-forward recovery.

The protocol and engine are database-agnostic. Adapters implement database
access, statement admission, transaction handling, locking and metadata
inspection. Migration SQL and supported operations depend on the adapter.

migr8 was inspired by the
[Flyway database migration tool](https://github.com/flyway/flyway).

## Execution model

One manifest defines one migration stream for one database namespace: a target
schema on Oracle or PostgreSQL, or a local database file on SQLite. Array order
in the manifest determines execution order; filenames do not. Each migration
has a permanent identity, a source directory, an entry point, a language and
an execution mode.

The database retains an immutable successful prefix and at most one `ACTIVE`
restartable migration immediately after it. Changed source for a successful
migration is refused. Before execution, `migrate` stages and fingerprints the
source units. The input artifact must remain stable during capture. A namespace
lock spans execution, including DDL and batch commits.

| Mode | Transaction and history behavior |
|---|---|
| Atomic | Supported migration work and its `SUCCESS` record commit in one transaction. An ordinary execution or validation failure rolls back both. |
| Restartable | The engine commits an `ACTIVE` record before invoking migration code. Python migrations can commit batches through `ctx.transaction()`; data and checkpoint updates in a batch commit together. Completion changes `ACTIVE` to `SUCCESS` and removes progress records in one transaction. |

A SQL file contains one statement or one procedural block. Python entry points
implement `migrate(ctx)` and can sequence statements and local SQL files.
Migration code must obey the selected mode's transaction contract. External
connections and durable external effects are outside the atomicity guarantee.

Restartable code is invoked from its entry point on every attempt. The author
must make it converge from any durable state an earlier admitted version could
have left, including completed DDL or data changes without a success record.
The [checkpointed backfill](examples/sqlite/migrations/003-backfill-region/migration.py)
shows how to select, update and checkpoint a bounded batch in one transaction.

## Supported adapters

| Adapter | Namespace lock | DDL in atomic migrations |
|---|---|---|
| Oracle | `DBMS_LOCK` | Prohibited; DDL uses restartable mode. |
| PostgreSQL | Session-level advisory lock | Supported statements only. |
| SQLite | POSIX advisory file lock | Supported statements only. |

DDL inside a restartable Python migration uses `ctx.ddl()` outside a batch.
No adapter admits DDL inside `ctx.transaction()`.

SQLite support is limited to one local database file on a POSIX host, accessed
by cooperating processes through the same canonical path. Network filesystems,
Windows and shared in-memory databases are outside the
[supported profile](docs/MANUAL.md#running-on-sqlite).
[Adapter differences](docs/MANUAL.md#adapter-differences-that-affect-how-you-write-migrations)
cover parameter binding, object validity checks and schema selection. Tested
server and driver versions are recorded in the [acceptance report](docs/ACCEPTANCE.md).

## Installation

Requires Python 3.12 or later on a POSIX host. In a virtual environment,
install the profile for the target database:

| Database | Install |
|---|---|
| Oracle | `python -m pip install 'migr8[oracle]'` |
| PostgreSQL | `python -m pip install 'migr8[postgres]'` |
| SQLite | `python -m pip install migr8` |

The base package has no runtime dependencies. SQLite uses the Python standard
library; the Oracle and PostgreSQL extras install their respective drivers.
For deployment, pin the approved version and driver dependencies as described
in the [installation guide](docs/MANUAL.md#install).

## SQLite example

Run the bundled example from a checkout using a fresh virtual environment:

```bash
git clone https://github.com/mrbald/migr8.git
cd migr8
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cd examples/sqlite
migr8 validate --offline
migr8 migrate
migr8 status
migr8 validate
```

This creates `examples/sqlite/build/probe.db`. The three migrations create an
orders table, insert 2,500 rows and backfill their region in batches of 500.
`status` shows three `SUCCESS` entries. A subsequent `migr8 migrate` reports
`no pending migrations`.

The backfill resumes using its `last_id` checkpoint. It does not revisit rows
inserted below that checkpoint; handling concurrent application writes is part
of the migration's design.

For Oracle or PostgreSQL, use the [configuration guide](docs/MANUAL.md#configure)
and [example projects](examples/).

## Commands

```text
migr8 migrate  [--config PATH] [--manifest PATH] [--recover ID]
migr8 validate [--config PATH] [--manifest PATH] [--offline] [--baseline PATH]
migr8 status   [--config PATH] [--manifest PATH]
```

Configuration and manifest paths default to `migr8.toml` and `manifest.toml`
in the current directory. `migrate` executes pending migrations. `validate`
checks the plan against database history; `status` reports history, pending
work and source differences. Both read-only commands leave the namespace
unchanged and do not execute migration code.

`validate --offline` checks manifest structure, identities, fingerprints,
statement admission and Python compilation without connecting to a database.
It does not establish that SQL will execute successfully or that restartable
code converges. Save its JSON report to compare later plans against an
approved artifact:

```bash
migr8 validate --offline --json > plan.json
migr8 validate --offline --baseline plan.json
```

All three commands accept `--json` for a machine-readable report and
`--log-file PATH` for a JSONL event log. Reports and log events include a run
correlation id. See [diagnostics](docs/MANUAL.md#diagnose-a-failure) for event
fields and error reporting.

## Failure and recovery

Recovery is roll-forward. Rerunning unchanged source retries the active
restartable migration from its entry point, retaining its committed progress.
Amended source requires `migr8 migrate --recover ID`; the amendment must
understand the states and checkpoints left by every earlier admitted version.
The flag does not permit changes to a successful migration or an active
migration's identity, position or execution mode.

If a database operation loses its reply, migr8 may be unable to determine
whether the operation committed. It stops with exit code 4 and discards the
connection. Rerun `migrate` to acquire the lock and reconcile durable state;
do not replay the individual statement manually.

| Exit | Meaning and action |
|---|---|
| 0 | Completed and applicable validation passed. |
| 1 | Usage, configuration, unsupported capability, connection or binding error. Correct the reported cause. |
| 2 | Manifest, history or source validation failed, including recovery admission. Inspect the reported difference. |
| 3 | Ordinary migration failure: atomic work rolled back, or restartable work remains `ACTIVE`. Correct the cause and rerun. |
| 4 | Operation outcome unknown. Rerun `migrate` to reconcile. |
| 5 | Namespace lock not acquired within policy. Wait or investigate the holder. |
| 6 | Namespace not initialized; returned by read-only commands. `migrate` initializes it. |
| 7 | Metadata damaged or incompatible with the supported layout. Stop and investigate; no automatic repair. |
| 8 | Transaction contract violated. Stop and inspect durable effects; manual remediation may be required. |

There is no undo, `clean`, baselining of an existing database, history repair,
forced unlock, skipping or out-of-order execution. `--baseline` compares plans;
it does not adopt an existing schema. See the
[recovery procedure](docs/MANUAL.md#recovery),
[operator runbooks](docs/MANUAL.md#runbooks) and
[scope limits](docs/MANUAL.md#not-in-this-tool-on-purpose).

## Tests

CI runs lint, formatting, type checks, service-free tests and integration tests
against real Oracle and PostgreSQL servers on pushes to `main` and pull
requests. The `tests` badge covers both test tiers.

The [acceptance report](docs/ACCEPTANCE.md) records tested versions, dates,
failure scenarios, results and open gates. Oracle 19c compatibility and
performance at scale remain [open gates](docs/ACCEPTANCE.md#open-gates).

From the repository root, with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv sync --all-extras
uv run pytest -m "not oracle and not postgres"
testenv/dbctl.sh up
testenv/dbctl.sh test
testenv/dbctl.sh down
```

The [test environment guide](testenv/README.md) covers the disposable database
rig. [AGENTS.md](AGENTS.md#checks-before-a-change-is-done) lists all required
checks.

## Documentation

| Document | Contents |
|---|---|
| [Manual](docs/MANUAL.md) | Configuration, migration authoring, deployment and recovery. |
| [Specification](docs/SPEC.md) | Protocol, invariants and guarantees. |
| [Architecture](docs/ARCHITECTURE.md) | Core layers, adapter contract and design decisions. |
| [Acceptance report](docs/ACCEPTANCE.md) | Tested versions, evidence and remaining gaps. |
| [Oracle connections](docs/ORACLE-CONNECTIONS.md) | Driver modes, TNS aliases, proxy authentication, wallets and TLS. |
| [Container job](deploy/container-job/) | Building and running a migration job against the test databases. |
| [Releasing](docs/RELEASING.md) | Versioning, release rehearsal, artifact checks and publication. |

## Licensing

AGPL-3.0-only ([LICENSE](LICENSE)), with commercial licenses available
separately ([COMMERCIAL.md](COMMERCIAL.md)). The model and dependency audit are
in [LICENSING.md](LICENSING.md).
