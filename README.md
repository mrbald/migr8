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
| [`deploy/container-job/`](deploy/container-job/) | Running the migration job as a container image against the test databases. |

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
testenv/dbctl.sh test                            # all 779, with the databases
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

## Release

Releases are tag-driven. A push of a tag that matches `v*` starts
[`.github/workflows/release.yml`](.github/workflows/release.yml). The workflow
runs the whole CI workflow as its first job, the live-database suite included.
It checks that the tag is annotated and names `TOOL_VERSION` from
`src/migr8/version.py`, builds the sdist and the wheel, and exports the runtime
dependency set of the `oracle` and `postgres` extras from `uv.lock` with hashes.
The base profile has no runtime dependencies. It then smoke-tests the installed
wheel with [`scripts/release_smoke.sh`](scripts/release_smoke.sh): without an
extra against the SQLite example, and with each extra against a live Oracle or
PostgreSQL server in a schema of its own. It publishes only the files the smoke
installed. Publication uses trusted publishing (OIDC) in the `pypi`
environment; the repository holds no PyPI token. The environment has no
required reviewer, so a tag push publishes without a further click. The GitHub release that follows carries the sdist, the wheel,
`runtime-oracle.txt`, `runtime-postgres.txt`, `versions.txt` (the servers the
smoke ran against) and `SHA256SUMS`. The workflow's header comment lists its
jobs and records the one-time trusted-publisher setup on pypi.org.

`src/migr8/version.py` is the only version source. `pyproject.toml` reads the
package version from `TOOL_VERSION`, and every durable history row records it.

A minor bump (0.1.0 to 0.2.0) carries behaviour: anything an operator or a
migration author can observe changing. A patch bump (0.1.0 to 0.1.1) carries
documentation or a correction with no behaviour change.

The annotated tag's message is the release note. Its first line is the
summary. Its body answers, in a fixed order, what an operator checks before
upgrading; the template is under "Make a release".

### Make a release

Start from a clean working tree with `main` pushed. Run the checks in
[`AGENTS.md`](AGENTS.md#checks-before-a-change-is-done), the live-database
suite included.

Set the new `TOOL_VERSION` in `src/migr8/version.py` and run `uv lock`.
`uv.lock` records no version for migr8, because the version is dynamic, so
`uv lock` changes it only if the dependency resolution moved; such a change is
part of the release and the tag message names it. Build locally and compare the
wheel's file list with the previous release's (`uv build`, then `unzip -Z1` on
both): the difference must be what the tag message is about to say. Commit and
push:

```sh
git commit src/migr8/version.py uv.lock -m "Set the version to X.Y.Z"
git push origin main
```

Rehearse the release on that commit before you tag it. A manual run of the
workflow from a branch runs `gates`, `build` and `smoke` and publishes nothing,
whatever its `publish` input says. Publication needs a tag: a tag push, or a
manual run from a tag with `publish` set.

```sh
gh workflow run release.yml --ref main
gh run list --workflow release.yml --limit 1
```

The tag checks in `build` run whenever the ref is a tag, on a push or a manual
run.

The smoke test also runs locally, on macOS or Linux, against a local build.
`dist/` must hold only this build's two files:

```sh
rm -rf dist
uv build --sdist && uv build --wheel
exports="$(mktemp -d)"
uv export --frozen --no-dev --no-emit-project --extra oracle -o "$exports/runtime-oracle.txt"
uv export --frozen --no-dev --no-emit-project --extra postgres -o "$exports/runtime-postgres.txt"
PYTHON="$(uv python find 3.12)" bash scripts/release_smoke.sh dist "$exports"
```

Without its connection variables, each live run prints a `SKIP:` line and the
rest of the smoke runs. To include them, start the rig with
`testenv/dbctl.sh up`, create empty smoke schemas, and set the variables before
the last command above. Each live run needs a freshly created schema: the
script fails on a namespace that is already initialized, so provision again
before every run. Provisioning drops and recreates `MIGR8_SMOKE`,
`MIGR8_SMOKE_RUNNER`, `MIGR8_SMOKE_OWNER` and the `migr8_smoke` schema, and
honours an inherited `ORACLE_DSN` or `PG_CONNINFO`, so run it in a shell that
sets neither:

```sh
ORACLE_TEST_USER=MIGR8_SMOKE .venv/bin/python testenv/provision_oracle.py
MIGR8_PG_SCHEMA=migr8_smoke .venv/bin/python testenv/provision_postgres.py
export MIGR8_SMOKE_ORACLE_DSN=localhost:15210/FREEPDB1 \
  MIGR8_SMOKE_ORACLE_USER=MIGR8_SMOKE MIGR8_SMOKE_ORACLE_PASSWORD=migr8_ora_test
export MIGR8_SMOKE_PG_DSN="host=127.0.0.1 port=15433 dbname=migr8_test" \
  MIGR8_SMOKE_PG_USER=migr8_test MIGR8_SMOKE_PG_PASSWORD=migr8_pg_test \
  MIGR8_SMOKE_PG_SCHEMA=migr8_smoke
```

Then tag that commit and push the tag. The first `-m` is the summary. The
others are the release note's body, one paragraph per question, in this order:
the metadata layout version (`layout_version` in `m8_meta`), the version of
[`docs/SPEC.md`](docs/SPEC.md), exit codes that changed meaning, inputs that
preflight now refuses and admitted before, what the adapters now detect or
refuse that they did not before, and Python modules that joined or left the
package. The last paragraph
describes the content since the previous release, A.B.C:

```sh
git tag -a vX.Y.Z -m "X.Y.Z: one-line summary" \
  -m "Metadata layout: unchanged at 1 (or from 1 to 2)." \
  -m "SPEC: unchanged at v6.2 (or from v6.2 to v6.3)." \
  -m "Exit codes: none changed meaning (or which code now means what)." \
  -m "Preflight: refuses nothing it admitted before (or what it now refuses)." \
  -m "Adapters: detect and refuse what they did before (or what changed)." \
  -m "Modules: none joined or left the package (or which)." \
  -m "Content since A.B.C: what changed, in prose."
git push origin vX.Y.Z
```

The tag must point at the commit that carries the same version in
`src/migr8/version.py`. The `build` job refuses a tag that disagrees, and a
lightweight one.

Last, check that the `release` workflow succeeded, and read the project page at
https://pypi.org/project/migr8/ and the GitHub release page for the tag.

A local `uv build` writes into the ignored `dist/` directory. It tests the
build only. The workflow is the one publication path.

PyPI refuses a second upload of a version that exists. Do not move a tag after
a successful publish. Release the next patch version instead.

## Licensing

AGPL-3.0-only ([LICENSE](LICENSE)), with commercial licenses available
separately ([COMMERCIAL.md](COMMERCIAL.md)). The model and the dependency audit
behind it are in [LICENSING.md](LICENSING.md).
