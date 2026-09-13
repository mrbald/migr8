# migr8

[![tests](https://github.com/mrbald/migr8/actions/workflows/ci.yml/badge.svg)](https://github.com/mrbald/migr8/actions/workflows/ci.yml)
[![vulnerabilities](https://github.com/mrbald/migr8/actions/workflows/audit.yml/badge.svg)](https://github.com/mrbald/migr8/actions/workflows/audit.yml)
[![secrets](https://github.com/mrbald/migr8/actions/workflows/secrets.yml/badge.svg)](https://github.com/mrbald/migr8/actions/workflows/secrets.yml)
[![PyPI](https://img.shields.io/pypi/v/migr8)](https://pypi.org/project/migr8/)
[![Python](https://img.shields.io/pypi/pyversions/migr8)](https://pypi.org/project/migr8/)
[![license](https://img.shields.io/badge/license-AGPL--3.0%20%7C%20commercial-blue)](LICENSING.md)

An inspectable database migration engine. Oracle is the primary target,
PostgreSQL is the second adapter, and SQLite is a supported local-file target
within a stated profile.

The `tests` badge covers the whole suite, including the Oracle and PostgreSQL
adapters running against real servers.
[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) records what each tier proves and
which gates are open.

| Document | What it is for |
|---|---|
| [`docs/MANUAL.md`](docs/MANUAL.md) | **Start here.** Install, configure, write a migration, run it, recover, diagnose. |
| [`docs/SPEC.md`](docs/SPEC.md) | The maintained specification: the protocol and its guarantees. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module layering, what each adapter owns, and the standing design positions. |
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | What was tested, against which versions, and which gates are open. |
| [`docs/ORACLE-CONNECTIONS.md`](docs/ORACLE-CONNECTIONS.md) | Thin and thick drivers, TNS aliases, proxy authentication, wallets and TLS. |
| [`deploy/apple-container/`](deploy/apple-container/) | Running the migration job as a container under Apple's `container` runtime. |

## What it does

One ordered TOML manifest defines one migration stream for one database
namespace. The database holds an immutable successful prefix and at most one
active restartable migration immediately after it. Migrations are SQL or Python,
atomic or restartable, with permanent identities and fingerprinted source.

Command surface:

```text
migr8 migrate  [--config PATH] [--manifest PATH] [--recover ID]
migr8 validate [--config PATH] [--manifest PATH] [--json] [--offline] [--baseline PATH]
migr8 status   [--config PATH] [--manifest PATH] [--json]
```

`validate --offline` is the plan lint for CI: it checks the manifest, the
fingerprints, statement admission and Python compilation with no database, no
secret and no namespace, and `--baseline` holds a published plan to the artifact
that was approved.

There is no undo, no `clean`, no baseline, no history repair and no forced
unlock. Those omissions are deliberate.

## Quick start on SQLite

```bash
uv sync --all-extras
uv run migr8 --help
cd examples/sqlite
uv run migr8 status   --config migr8.toml --manifest manifest.toml
uv run migr8 migrate  --config migr8.toml --manifest manifest.toml
uv run migr8 validate --config migr8.toml --manifest manifest.toml
```

## Tests

```bash
uv run pytest -m "not oracle and not postgres"   # 617 tests, no services needed
testenv/dbctl.sh up                              # disposable Oracle + PostgreSQL
testenv/dbctl.sh test                            # all 777, with the databases
testenv/dbctl.sh down
```

`tests/test_adapter_contract.py` is one contract suite run against every
configured adapter, so adding an adapter means filling in a dialect and running
it rather than writing a new test file.

The same gates run in CI on every push and pull request, the live-database job
included.

## Diagnosing a failure

```bash
migr8 migrate --log-file /var/log/migr8/run.jsonl   # JSONL event log
migr8 migrate --json | jq '{outcome, failed_migration, phase}'
```

Every run has a correlation id, printed on failure and present on every log line.
The log records each phase with timings and ends with the outcome, the failing
migration and the phase.

A failure is reported by exception type and engine error code — `ORA-00001`,
`SQLSTATE 23505` — with the operation, phase and identity around it, on stderr,
in `--json`, in the event log and under `--verbose` alike. The driver's own
message is not reproduced, because it quotes the values that produced the error.
Two exclusions are stated and there are no others: `ctx.log()` fields are the
author's choice and the author's responsibility, and connection, session and
privilege errors raised before any migration runs quote the server, because
there the server's message is the diagnostic.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Completed and applicable validation passed. |
| 1 | Usage, configuration, unsupported capability, connection or binding error. |
| 2 | Manifest/history/source validation failed, including recovery admission. |
| 3 | Ordinary migration failure: atomic work rolled back, or restartable remains ACTIVE. |
| 4 | Operation outcome unknown after communication failure; rerun to reconcile. |
| 5 | Migration lock not acquired within policy. |
| 6 | Namespace not initialized; read-only commands only. |
| 7 | Metadata damaged or incompatible with the supported layout. |
| 8 | Detected transaction-contract violation; durable effects may need remediation. |

## Layout

```text
src/migr8/
  manifest.py fingerprint.py paths.py staging.py   capture and source integrity
  sqltext.py                                       lexical scanning only, no policy
  model.py statevalidate.py                        durable state and pure validation
  engine.py context.py loader.py latch.py          orchestration and the author facade
  diagnostics.py                                   correlation id and event log
  cli.py readonly.py reporting.py                  three commands and their output
  adapters/base.py                                 the contract, the session rules, the operation guard
  adapters/{oracle,postgres,sqlite}.py             dialect and engine-specific behaviour
docs/  examples/  testenv/  deploy/  tests/
migr8                                              thin entry point for a checkout
```

`adapters` imports from the core, and `engine`, `context` and `checks` import
`adapters.base` for the `Adapter` contract. No core module imports a concrete
driver: `oracle`, `postgres` and `sqlite` are reached only through
`adapters.create`, and the engine contains no engine-specific SQL.

## Licensing

AGPL-3.0-only ([LICENSE](LICENSE)), with commercial licenses available
separately ([COMMERCIAL.md](COMMERCIAL.md)). The model and the dependency audit
behind it are in [LICENSING.md](LICENSING.md).
