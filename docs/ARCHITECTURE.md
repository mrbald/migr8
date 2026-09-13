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
  base          the contract plus every shared rule: the operation guard,
                history transitions, metadata initialization and inspection,
                the progress store, statement admission, engine transactions
  metadata      the logical layout, state classification, definition comparison
  oracle        primary target: DBMS_LOCK, transaction identity, ALL_* validity
  postgres      second adapter: advisory lock, transactional DDL, real xid guard
  sqlite        local file database: file lock, stated enforcement boundary
```

The dependency direction is one-way, but not the way a one-line summary suggests.
`adapters` imports from the core (`config`, `errors`, `latch`, `manifest`,
`model`, `sqltext`), and `engine`, `context` and `checks` import
`adapters.base` for the `Adapter` contract, `Boundary` and `OutcomeClass`. No
core module imports a concrete driver: `oracle`, `postgres` and `sqlite`
are reached only through `adapters.create`, and the engine contains no
engine-specific SQL. `checks` exists so the read-only commands can preflight
without importing `engine`, which would pull in the Python unit loader;
`readonly`'s contract is that it imports nothing able to import migration code.
That is also what lets `validate --offline` run the whole plan lint, Python
compilation included, in a process that has connected to nothing: `compile()`
parses the source without importing it, so the module that checks migration code
still never loads it.

`statevalidate` performs no I/O at all. It is handed a consistent snapshot and a
fingerprinted capture and either returns a `Plan` or raises, which is what makes
the state machine testable without a database and is why it is inside the 100%
branch-coverage gate.

## Rule versus dialect

The adapter layer's dividing line is between a *rule*, which the specification
states and which must exist exactly once, and a *dialect*, which is how one
engine spells it.

`Adapter` owns the rules about the database session: which columns an attempt or
completion update may touch and which are permanent, that an affected-row count
other than one is metadata damage, the progress store's shape and its
batch-transaction precondition, which statement contexts exist and which leading
tokens each admits, how a failed call is classified, which database calls go
through the operation guard, metadata initialization and inspection, and the
engine-owned SQL path that is kept separate from the migration facade. `update_active_attempt` has
exactly one `SET` list in the project, so "position, identity, mode, start time
and first fingerprint are never touched by recovery" is enforced once rather than
asserted three times.

Rules also live outside the adapter, where they belong to a different subject.
`statevalidate` owns the history and plan rules and decides which failures are
exit 7 and which are exit 2. `engine` owns the state machine and the order of
durable transitions, including which terminal path an outcome takes. `manifest`,
`paths`, `staging` and `fingerprint` own the artifact rules. `sqltext` owns the
lexical rules. Each of those exists once too.

An adapter declares a dialect and little else:

```python
paramstyle = "named"  # shared SQL is written with :name and translated
now_expression = "SYSTIMESTAMP"
supports_on_conflict = False  # selects MERGE instead of ON CONFLICT
identifier_case = "upper"  # Oracle folds, so quoting must follow
```

plus a `StatementPolicy` of token sets and two hooks for the engine-owned SQL
path. `Adapter` has 26 abstract members.

These members stay per-adapter:

| Member | Why it cannot be shared |
|---|---|
| `_objects_present`, `_definition_problems` | Oracle reads `ALL_TAB_COLUMNS`, `ALL_CONSTRAINTS`, `ALL_IND_EXPRESSIONS`; PostgreSQL reads `pg_attribute`, `pg_constraint`, `pg_index`; SQLite reads `PRAGMA table_info`. The columns, the vocabulary and the state each reports differ. |
| `_create_metadata_object` | Oracle's `CREATE` commits by itself; PostgreSQL and SQLite wrap each one in a transaction and an explicit commit. |
| `execute_ddl` | Same reason: Oracle submits and returns, the others begin, run and commit. |
| `executemany` | Oracle passes `batcherrors=False, arraydmlrowcounts=False`; psycopg needs a cursor context manager. |
| `classify_exception`, `error_code` | `ORA-`/`DPY-` numbers, SQLSTATEs and `sqlite3` result codes are different namespaces. |
| `_establish_transaction_identity`, `_read_transaction_identity` | `DBMS_TRANSACTION.LOCAL_TRANSACTION_ID`, `pg_current_xact_id*` and an adapter-owned epoch. |
| `_has_open_transaction`, `_read_snapshot` | Oracle asks `DBMS_TRANSACTION` and reads the whole snapshot in one statement; PostgreSQL reads the driver's transaction status and opens a repeatable-read block; SQLite reads `in_transaction`. |
| `acquire_lock`, `release_lock`, `lock_binding` | `DBMS_LOCK`, advisory locks, a lock file. |
| `_run`, `_metadata_execute`, `_metadata_query`, `connect`, `close`, `discard` | Driver API. |
| `statement_policy`, `capabilities` | Token sets and what the adapter claims to enforce. |

`initialize()` and `inspect_metadata()` are shared: the order objects are created
in, what counts as a permitted incomplete initialization, and the rule that
nothing is recreated are specification rules, not dialect.

`_objects_present()` has a stated contract — it returns logical names — so Oracle
folds its dictionary's upper case back rather than making every caller remember
to. Each adapter reads its own dictionary, but how the answers are *compared* is
shared: `metadata.check_problems` holds the rule that a table's check
constraints are matched against the complete supported set, exactly and in both
directions. Substring containment was not enough — appending `OR 1=1` to every
required condition leaves each fragment present while the constraint enforces
nothing — so each expected condition is the canonical form of what that server
actually stores, recorded from the supported release rather than guessed, and a
condition that is missing, altered or added is damage. A foreign key's target is
compared schema-qualified and in key order: a same-named history table in another
schema is a different table.

## The operation boundary

Every database call a run makes goes through `Adapter.guarded()`, which checks
the latch before submitting and classifies exactly one failure afterwards. The
entry points it covers are the point: migration SQL and the Python facade, but
also engine-owned metadata reads and writes, namespace inspection, object
creation, the snapshot read, `begin`, the transaction-state probe, the
transaction identity and the final-validity check.

That breadth is the rule. A lost reply during the transaction-state probe leaves
the same doubt as one during a commit, and Oracle answers that probe with a real
round trip, so reading it as "no transaction is open" and carrying on would be a
guess. The required behaviour for a failed database call must not depend on which
public method reached it, so the raw driver methods stay behind the guarded entry
points: `_metadata_execute`, `_do_begin`, `_has_open_transaction` and each
adapter's dictionary queries are private, and the public method above each one
supplies the guard.

Two kinds of call stay outside. Setup — `connect`, session configuration,
`acquire_lock` — runs before the engine owns a run and has its own exit codes.
Teardown — `close`, `discard`, `rollback` — must not turn a failure while closing
into an unknown migration outcome, so those ask the driver directly.

## The adapter contract suite

`tests/test_adapter_contract.py` is one suite parametrised over every configured
adapter: initialization to a verified layout, engine-transaction state,
transaction identity, the full admission/attempt/completion lifecycle, the
permanence of `first_fingerprint` and `started_at` across a recovery, bind
narrowing, statement admission in every context, reserved-object refusal, the DDL
allow-list, required-object consistency, and error classification. It also
asserts that every name in `adapters.SUPPORTED` appears in the parametrisation,
so a new adapter cannot skip it.

The suite is parametrised over `adapters.SUPPORTED`, so a new adapter is covered
by the existing cases as soon as it is registered.

## The author-facing API

Ten members, and the shape expresses the rules:

```python
ctx.migration_id  ctx.attempt  ctx.execute  ctx.executemany  ctx.query
ctx.sql  ctx.log                                    # every mode
ctx.transaction  ctx.ddl  ctx.progress              # restartable only
```

`AtomicContext` exposes `migration_id`, `attempt`, `execute`, `executemany`,
`query`, `sql` and `log`. `RestartableContext` adds `transaction`, `ddl` and
`progress`. They are separate classes, so the three restartable members are
absent from an atomic context rather than present and raising.

`ctx.transaction()` returns an explicit context-manager object rather than a
generator, so a batch entered and never exited is detected instead of being
closed non-deterministically by garbage collection. No driver handle, cursor or
commit method is exposed. There is no `execute_returning()` for Oracle DML output
binds: the specification says such a member arrives with a concrete requirement
and a test.

Statement admission through this facade is an honest-mistake guard, not a
sandbox. Migration code is trusted, and the specification says so.

## Diagnostics

`diagnostics.py` gives every run a correlation id, writes one JSON object per
line to `--log-file` flushed immediately, and ends with a terminal record
carrying the outcome, exit code, failing migration and phase. `migrate --json`
prints the same outcome record on stdout. `ctx.log()` writes into the same log,
so author notes interleave with the engine phases around them.

Three constraints hold it in scope. Nothing is written without `--log-file`, so
default behaviour is unchanged. No database object is involved: an audit table
would change the metadata layout and enlarge the scope the specification fixes.
And the output policy is the one described below.

**What a failure is allowed to say.** A driver message quotes the data that
produced it — PostgreSQL echoes the literal in `invalid input syntax for type
integer: "..."` — so the runner does not reproduce it. A reported failure names
the exception type, the engine error code (`ORA-00001`, `SQLSTATE 23505`,
`SQLITE_CONSTRAINT`), the operation, the phase, the migration identity and the
run id. That is the rule on stderr, in `--json`, in the event log and under
`--verbose` alike; the driver's own text stays in the exception chain and is
logged only at `DEBUG`, which no CLI flag enables. A field-name denylist
additionally drops `password`, `secret`, `dsn`, `credential` and `params` from
event fields.

Two things are stated exclusions, and there are no others. `ctx.log(...)` writes
whatever fields the author passes, which is the author's responsibility.
And connection, session-configuration and privilege errors raised before any
migration runs quote the server's message, because that message is the
diagnostic and carries no migration data; the connection string itself is not
included.

The rule applies to every path that writes, not only to `Engine.run()`.
`describe_safely` in `errors` is what the CLI's own handlers and the event log
use, because they have no adapter to ask for an engine error code. It names a
`Migr8Error` by its report — engine-authored text, written for an operator — and
anything else by module and type. The corollary is a constraint on error
construction: a `Migr8Error` whose message was built from a driver's text would
pass straight through, so engine-authored messages and driver text are kept
apart at the point the error is raised, not at the point it is rendered.

**One owner decides what a command returns.** `main()` fixes the exit code once,
and diagnostics cannot change it afterwards. Opening the event log happens before
any database work, so a log path that cannot be written fails with exit 1 rather
than escaping the handlers that exist to produce an exit code; closing it is
contained, so a failing close cannot replace an outcome the database already made
durable and the operator has already been shown. A handled failure is rendered
the same way whether or not the engine was reached, `--json` included, and always
with the run id. An unexpected exception maps to exit 1 and says what it can
honestly say: that the fallback handler cannot tell what the database did,
because such a failure can also arise while reporting a run that completed, and
that `status` is what answers the question.

## Standing positions

**No client-side statement or call timeout.** Nothing bounds how long a single
migration statement may run, so a statement that blocks forever holds the
namespace lock until an operator intervenes at the database. This is the
specification's position: a client-side timeout would create a new
unknown-outcome surface, because a timeout firing during a commit is
indistinguishable from a lost acknowledgement. Bound long statements with
database-side policy — `DDL_LOCK_TIMEOUT` and resource manager on Oracle,
`statement_timeout` on PostgreSQL.

**Oracle `status` inspects the whole layout before reading the snapshot.**
`inspect_metadata` queries `ALL_OBJECTS` once, then `ALL_TAB_COLUMNS` and
`ALL_CONSTRAINTS` once per metadata table, then `ALL_INDEXES`,
`ALL_IND_COLUMNS` and `ALL_IND_EXPRESSIONS` for the one-ACTIVE index, then the
marker and the row counts. That validation is what distinguishes "uninitialized"
from "damaged", which is load-bearing. No timing for it has been measured.

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
