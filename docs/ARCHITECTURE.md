# Architecture

How the code is arranged, what each layer owns, and the design positions that
stand. The protocol itself is [`SPEC.md`](SPEC.md); what has been tested is
[`ACCEPTANCE.md`](ACCEPTANCE.md).

## Layering

```text
core, ~3,000 code lines, no database dependency
  manifest  fingerprint  paths  staging      capture and source integrity
  sqltext                                    lexical scanning only; no policy
  model  statevalidate                       durable state; pure validation
  checks                                     preflight and binding verification
  engine  context  loader  latch             orchestration and the author facade
  diagnostics                                correlation id and event log
  cli  readonly  reporting                   three commands and their output

adapters, ~3,000 code lines
  base          the contract plus every shared rule: history transitions,
                metadata initialization and inspection, the progress store,
                statement admission, engine transactions, execution
  metadata      the logical layout, state classification, definition comparison
  oracle        primary target: DBMS_LOCK, transaction identity, ALL_* validity
  postgres      second adapter: advisory lock, transactional DDL, real xid guard
  sqlite_probe  local probe: file lock, stated enforcement boundary, no claims
```

The dependency direction is one-way: `adapters` imports from the core, never the
reverse, and the engine contains no engine-specific SQL. `checks` exists so the
read-only commands can preflight without importing `engine`, which would pull in
the Python unit loader; `readonly`'s contract is that it imports nothing able to
import migration code.

`statevalidate` performs no I/O at all. It is handed a consistent snapshot and a
fingerprinted capture and either returns a `Plan` or raises, which is what makes
the state machine testable without a database and is why it is inside the 100%
branch-coverage gate.

## Rule versus dialect

The adapter layer's dividing line is between a *rule*, which the specification
states and which must exist exactly once, and a *dialect*, which is how one
engine spells it.

Every rule lives in `Adapter`: which columns an attempt or completion update may
touch and which are permanent, that an affected-row count other than one is
metadata damage, the progress store's shape and its batch-transaction
precondition, which statement contexts exist and which leading tokens each
admits, metadata initialization and inspection, and the engine-owned SQL path
that is kept separate from the migration facade. `update_active_attempt` has
exactly one `SET` list in the project, so "position, identity, mode, start time
and first fingerprint are never touched by recovery" is enforced once rather than
asserted three times.

An adapter declares a dialect and little else:

```python
paramstyle = "named"          # shared SQL is written with :name and translated
now_expression = "SYSTIMESTAMP"
supports_on_conflict = False  # selects MERGE instead of ON CONFLICT
identifier_case = "upper"     # Oracle folds, so quoting must follow
```

plus a `StatementPolicy` of token sets and two hooks for the engine-owned SQL
path. `Adapter` has 26 abstract members.

What stays per-adapter is what is genuinely different rather than differently
spelled. Metadata *inspection* is the clearest case: Oracle's `ALL_TAB_COLUMNS`,
PostgreSQL's `pg_attribute` and SQLite's `PRAGMA table_info` differ in substance,
and forcing them into one shape would obscure all three. `initialize()` and
`inspect_metadata()` are shared; `_objects_present()`, `_definition_problems()`
and `_create_metadata_object()` are not. `executemany` is per-adapter for the
same reason: Oracle passes `batcherrors=False` and PostgreSQL needs a cursor
context manager.

`_objects_present()` has a stated contract — it returns logical names — so Oracle
folds its dictionary's upper case back rather than making every caller remember
to.

## The adapter contract suite

`tests/test_adapter_contract.py` is one suite parametrised over every configured
adapter: initialization to a verified layout, engine-transaction state,
transaction identity, the full admission/attempt/completion lifecycle, the
permanence of `first_fingerprint` and `started_at` across a recovery, bind
narrowing, statement admission in every context, reserved-object refusal, the DDL
allow-list, required-object consistency, and error classification. It also
asserts that every name in `adapters.SUPPORTED` appears in the parametrisation,
so a new adapter cannot skip it.

This is the structural answer to a fourth adapter: fill in a dialect and run the
existing suite, rather than write a fourth test file and hope it covers the same
ground.

## The author-facing API

Ten members, and the shape expresses the rules:

```python
ctx.migration_id  ctx.attempt  ctx.execute  ctx.executemany  ctx.query
ctx.sql  ctx.log                                    # every mode
ctx.transaction  ctx.ddl  ctx.progress              # restartable only
```

`transaction`, `ddl` and `progress` genuinely do not exist on an atomic context —
they are separate classes, not methods that raise — so an author cannot reach for
them. `ctx.transaction()` returns an explicit context-manager object rather than a
generator, so a batch entered and never exited is detected instead of being
quietly rolled back by garbage collection. No driver handle, cursor or commit
method is exposed. There is no `execute_returning()` for Oracle DML output binds:
the specification says such a member arrives with a concrete requirement and a
test, not in advance.

Statement admission through this facade is an honest-mistake guard, not a
sandbox. Migration code is trusted, and the specification says so.

## Diagnostics

`diagnostics.py` gives every run a correlation id, writes one JSON object per
line to `--log-file` flushed immediately, and ends with a terminal record
carrying the outcome, exit code, failing migration and phase. `migrate --json`
prints the same outcome record on stdout. `ctx.log()` writes into the same log,
so author notes interleave with the engine phases around them.

Three constraints hold it in scope. Nothing is written without `--log-file`, so
default behaviour is unchanged. A field-name denylist keeps passwords, DSNs and
bind values out of the log. And no database object is involved: an audit table
would change the metadata layout and enlarge the scope the specification fixes.

`main()` maps an unexpected exception to exit 1 with the run id and an explicit
instruction to run `status`, and the engine turns an unexpected fault during a run
into an ordinary rolled-back failure. A migration tool that crashes still has to
tell the operator what is safe to do next.

## Standing positions

These are decisions, not omissions.

**No client-side statement or call timeout.** Nothing bounds how long a single
migration statement may run, so a statement that blocks forever holds the
namespace lock until an operator intervenes at the database. This is the
specification's position: a client-side timeout would create a new
unknown-outcome surface, because a timeout firing during a commit is
indistinguishable from a lost acknowledgement. Bound long statements with
database-side policy — `DDL_LOCK_TIMEOUT` and resource manager on Oracle,
`statement_timeout` on PostgreSQL.

**Oracle `status` costs three round trips before the snapshot read.**
`inspect_metadata` probes objects, then columns, then constraints and indexes.
That validation is what distinguishes "uninitialized" from "damaged", which is
load-bearing, and the measured cost against a local database is a few
milliseconds.

**`has_open_transaction()` asks the database every time.** It executes a PL/SQL
block at each `ctx.ddl()` and after every migration returns. Caching it would be
wrong: the point is to ask the database rather than trust bookkeeping. Oracle
assigns a local transaction id only on first write, so "is there uncommitted work"
and "am I inside an engine-opened transaction" are different questions, and the
adapter tracks the second explicitly rather than inferring it from the first.

**Five reporting methods could be one descriptor — open.** `capabilities`,
`server_description`, `normalized_namespace`, `lock_binding` and
`session_identity` are five abstract members a new adapter fills in separately,
and one `describe() -> AdapterDescription` would take the abstract count from 26
to 22. It is not done because the capability notes are live strings computed
during `connect()` (Oracle's `COMMIT_WAIT` note, PostgreSQL's
`synchronous_commit` read-back), so a frozen descriptor would need a build step
anyway, and the acceptance report and two tests read `capabilities().notes`
directly. The gain is cosmetic; recorded as available.

## Where the gates are

`.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, `mypy` over
`src` and `testenv`, the service-free tier under branch coverage, and then, in a
separate job, the whole suite against real Oracle and PostgreSQL servers brought
up by `testenv/dbctl.sh` — the same script the acceptance report tells a reader to
run. Both jobs refuse to pass while testing less than they claim: one asserts the
live tests still exist, the other that none of them skipped.

The coverage gate is scoped to `errors`, `fingerprint`, `latch`, `model` and
`statevalidate` at 100% of branches, and `[tool.coverage.report]` in
`pyproject.toml` argues that scope. The adapters, engine and CLI are excluded
deliberately: their branches are reached through real databases and real
subprocesses, so an in-process percentage there would measure the harness rather
than the tests.
