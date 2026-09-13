# migr8

An experimental, inspectable database migration engine. Oracle is the primary
target, PostgreSQL is the second adapter, and SQLite is an explicitly limited
local probe.

**Status: experimental.** See [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) for what
has actually been tested and which acceptance gates are still open. Fast tests
passing is not a production-readiness claim.

| Document | What it is for |
|---|---|
| [`docs/MANUAL.md`](docs/MANUAL.md) | **Start here.** Install, configure, write a migration, run it, recover, diagnose. |
| [`docs/SPEC.md`](docs/SPEC.md) | The maintained specification: the protocol and its guarantees. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Architecture review: ranked findings and the decision taken on each. |
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | What was tested, against which versions, and which gates are open. |
| [`deploy/apple-container/`](deploy/apple-container/) | Running the migration job as a container under Apple's `container` runtime. |

## What it does

One ordered TOML manifest defines one migration stream for one database
namespace. The database holds an immutable successful prefix and at most one
active restartable migration immediately after it. Migrations are SQL or Python,
atomic or restartable, with permanent identities and fingerprinted source.

Command surface:

```text
migr8 migrate  [--config PATH] [--manifest PATH] [--recover ID]
migr8 validate [--config PATH] [--manifest PATH] [--json]
migr8 status   [--config PATH] [--manifest PATH] [--json]
```

There is no undo, no `clean`, no baseline, no history repair and no forced
unlock. Those omissions are deliberate.

## Quick start against the SQLite probe

```bash
uv sync --all-extras
uv run migr8 --help
cd examples/sqlite-probe
uv run migr8 status   --config migr8.toml --manifest manifest.toml
uv run migr8 migrate  --config migr8.toml --manifest manifest.toml
uv run migr8 validate --config migr8.toml --manifest manifest.toml
```

## Tests

```bash
uv run pytest -m "not oracle and not postgres"   # 315 tests, no services needed
testenv/dbctl.sh up                              # disposable Oracle + PostgreSQL
testenv/dbctl.sh test                            # all 460, with the databases
testenv/dbctl.sh down
```

`tests/test_adapter_contract.py` is one contract suite run against every
configured adapter, so adding an adapter means filling in a dialect and running
it rather than writing a new test file.

## Diagnosing a failure

```bash
migr8 migrate --log-file /var/log/migr8/run.jsonl   # JSONL event log
migr8 migrate --json | jq '{outcome, failed_migration, phase}'
```

Every run has a correlation id, printed on failure and present on every log line.
The log records each phase with timings and ends with the outcome, the failing
migration and the phase. Credentials and bind values are never written.

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
  adapters/base.py                                 the contract plus every shared rule
  adapters/{oracle,postgres,sqlite_probe}.py       dialect and engine-specific behaviour
docs/  examples/  testenv/  deploy/  tests/
migr8                                              thin entry point for a checkout
```

The dependency direction is one-way: adapters import from the core, never the
reverse, and the engine contains no engine-specific SQL.
