# Architecture review

Reviewed against five goals: production reliability, minimalism, a simple API,
cheap maintenance, and root-cause diagnosability. Findings are ranked by what
they would cost in production. Each one says what was decided, not just what was
observed.

Everything marked **fixed** is in the code and covered by a test. Everything
marked **kept** or **open** is a deliberate position with a reason.

## Summary

| # | Finding | Severity | Decision |
|---|---|---|---|
| 1 | Interruption during a commit produced a traceback, not exit 4 | **high** | fixed |
| 2 | `SIGTERM` killed the process with no unwinding at all | **high** | fixed |
| 3 | No correlation id, no event log, no machine-readable outcome | **high** | fixed |
| 4 | The rules that matter were implemented three times, once per adapter | **high** | fixed |
| 5 | Oracle accepted a nested `begin()` | medium | fixed |
| 6 | An unexpected defect escaped as a traceback with exit 1 and no guidance | medium | fixed |
| 7 | No adapter conformance suite; a fourth adapter meant a fourth test file | medium | fixed |
| 8 | Seven unused symbols, including engine-specific token sets in the core lexer | low | fixed |
| 9 | No statement or call timeout | medium | kept, documented |
| 10 | `status` needs three round trips on Oracle where one would do | low | kept |
| 11 | The five reporting methods could collapse into one descriptor | low | open |
| 12 | Oracle's `has_open_transaction()` costs a round trip and is called often | low | kept |

A second pass after the rename to `migr8` reviewed the whole tree again:

| # | Finding | Severity | Decision |
|---|---|---|---|
| 13 | Reserved-object refusal matched substrings, not identifiers | **high** | fixed |
| 14 | Placeholder translation ran before the timestamp expression was safe | medium | fixed |
| 15 | Metadata initialization and inspection were implemented three times | medium | fixed |
| 16 | `readonly` imported `engine`, and through it the Python unit loader | low | fixed |
| 17 | Eleven unreferenced members and two redundant wrappers | low | fixed |
| 18 | The PostgreSQL adapter built SQL two different ways | low | fixed |

Counts after the work: **460 tests**, all passing, across pure, SQLite probe,
PostgreSQL 17.5 and Oracle Free 23.9. The adapter contract went from 35 abstract
members to **27**, and about 630 lines of duplicated adapter logic became ~380
lines of shared policy. The second pass removed a further 178 source lines while
adding the tests that pin findings 13 and 14.

---

## 1. Interruption during a commit produced a traceback — fixed

`Engine.run()` caught `UnknownOutcomeError` and `Migr8Error`. A
`KeyboardInterrupt` is neither, so Ctrl-C escaped `run()` entirely, skipping
`discard()` and `close()`. If the interrupt landed while a commit was in flight,
the tool exited with a traceback and exit code 1 — telling the operator *nothing*
about the one situation where the durable outcome is genuinely unknown.

`durable_commit` now catches `BaseException`. An interruption during a commit is
latched as an unknown outcome exactly like a lost acknowledgement, because it is
the same thing: the request may already be durable. Only a positively identified
definite failure escapes as itself. `Engine.run()` gained a `BaseException` arm
that honours an existing latch, and otherwise rolls back and reports an ordinary
interrupted failure with ACTIVE retained.

Covered by `test_interruption_during_a_commit_is_an_unknown_outcome` and the
parametrised signal test.

## 2. `SIGTERM` killed the process with no unwinding — fixed

Python's default `SIGTERM` handling terminates immediately. A supervisor or
orchestrator stopping a migration job would abandon the connection mid-operation
with nothing written anywhere. The CLI now installs a handler that raises
`KeyboardInterrupt` for `SIGTERM` and `SIGHUP`, so a supervised stop takes the
same classified path as Ctrl-C.

This matters more in the containerised shape than on a terminal: `container stop`
and every scheduler send `SIGTERM` first.

## 3. No correlation id, no event log, no machine-readable outcome — fixed

The engine generated a `run_id`, used it in author log lines, and then never
showed it to anyone. Failures surfaced as one prose sentence on stderr. There was
no way to answer "what happened, in what order, and how long did each step take"
after the fact, which is most of root-cause analysis.

Added `diagnostics.py`, about 90 lines:

* `--log-file PATH` (or `MIGR8_LOG_FILE`) appends one JSON object per line,
  flushed immediately, so a kill leaves the log usable to the last event.
  Phases recorded: run start, preflight, connect, lock, plan, per-migration
  start/done with timings, admission, and a terminal `run_end` carrying the
  outcome, exit code, failing migration and phase.
* `migrate --json` prints the `RunReport` as the outcome record, including
  `failed_migration`, `phase`, `recovery_command`, `duration_seconds` and
  `run_id`.
* The run id is printed on every failure and included in `status` and `validate`
  output, so a report and its log line up.
* `ctx.log()` now writes into the same log, so author notes interleave with the
  engine phases around them.

Three constraints were deliberate. Nothing is written without `--log-file`, so
the default behaviour is unchanged. A field-name denylist keeps passwords, DSNs
and bind values out of the log. And no database object was added: an audit table
would change the metadata layout and grow the scope the specification fixes.

## 4. The rules that matter were implemented three times — fixed

This was the maintenance finding. Adapters were 43% of the code, and the
duplication was not in the dialect, it was in the *policy*:

| Group | Oracle | PostgreSQL | SQLite | Total |
|---|---|---|---|---|
| History transitions | 76 | 83 | 76 | 235 |
| Statement admission | 55 | 55 | 48 | 158 |
| Progress store | 22 | 20 | 16 | 58 |

Each adapter independently decided which columns a recovery may touch, whether to
check the affected-row count, and which leading tokens a batch admits. Three
places to get the same invariant right is three places to get it wrong, and
nothing detected disagreement.

These now live in `Adapter`, driven by a small declared dialect:

```python
paramstyle = "named"          # shared SQL is written with :name and translated
now_expression = "SYSTIMESTAMP"
supports_on_conflict = False  # selects MERGE instead of ON CONFLICT
identifier_case = "upper"     # Oracle folds, so quoting must follow
```

plus a `StatementPolicy` of five token sets, and two methods for the engine-owned
SQL path. `update_active_attempt` now has exactly one SET list in the whole
project, so "position, identity, mode, start time and first fingerprint are never
touched by recovery" is enforced once rather than asserted three times.

Two real bugs surfaced while doing it, which is the point:

* `admit_ddl` initially rejected stored PL/SQL definitions because they are not
  `StatementKind.SQL`. They *are* DDL; only anonymous blocks are not.
* python-oracledb rejects a bind value with no matching placeholder where sqlite3
  and psycopg ignore it. Shared SQL now narrows its binds to the placeholders
  actually present, and still raises on a missing one so a typo fails loudly.

What was **not** merged: metadata inspection and the physical DDL. Oracle's
`ALL_TAB_COLUMNS`, PostgreSQL's `pg_attribute` and SQLite's `PRAGMA table_info`
are genuinely different, and forcing them into one shape would obscure all three.
That still holds for the dictionary queries themselves. It did not hold for the
sequence around them, which finding 15 later moved into the base.

Net effect on size is roughly flat, about 7,250 lines. The win is structural: the
number of places one rule can be wrong went from three to one.

## 5. Oracle accepted a nested `begin()` — fixed

Found by the new conformance suite on its first run. `begin()` guarded on
`has_open_transaction()`, which on Oracle is false until the first write, because
Oracle assigns a local transaction id lazily. A second `begin()` was therefore
accepted and two nested scopes would have silently shared one transaction.

`begin()` now guards on both the engine-transaction flag and the database's view
of uncommitted work. This is the same root cause as an earlier bug where Oracle's
progress precondition was inferred from `LOCAL_TRANSACTION_ID`; the lesson is
recorded in the specification.

## 6. An unexpected defect escaped as a traceback — fixed

`main()` caught only `Migr8Error`. Any defect in the tool itself produced a
traceback and whatever exit code Python chose. A migration tool that crashes must
still tell the operator what is safe to do next. `main()` now maps an unexpected
exception to exit 1 with the run id and an explicit instruction to run `status`
to confirm the database state, and the engine converts an unexpected fault during
a run into an ordinary rolled-back failure.

## 7. No adapter conformance suite — fixed

Adapters were tested through the engine, in one test file per engine. Adding an
adapter meant writing a fourth file and hoping it covered the same ground.

`tests/test_adapter_contract.py` is one suite parametrised over every configured
adapter, covering initialization, engine-transaction state,
transaction identity, the full admission/attempt/completion lifecycle, the
permanence of `first_fingerprint` and `started_at` across a recovery, bind
narrowing, statement admission in every context, reserved-object refusal, and
error classification. It also asserts that every name in `adapters.SUPPORTED`
appears in the suite, so a new adapter cannot skip it.

## 8. Seven unused symbols — fixed

Removed `tool_identity`, `hooks.unregister`, Oracle's `_HISTORY_SELECT` and
`_is_session_statement`, and `RunReport.skipped`. Three engine-specific token
sets (`ORACLE_ATOMIC_FIRST_TOKENS`, `TRANSACTION_CONTROL_TOKENS`,
`SESSION_SETTING_TOKENS`) were deleted from the core lexer: they were dead, and
they were a layering violation — the lexer scans, adapters decide. The reserved
metadata names are now read from one constant rather than retyped in each
adapter.

## 9. No statement or call timeout — kept, documented

Nothing bounds how long a single migration statement may run. A migration that
blocks forever holds the namespace lock forever, and the only recovery is
operator intervention at the database.

This is the specification's explicit position: the initial runner need not impose
a driver call timeout, and a deployment may choose database-side statement or
resource policies instead. Adding a client-side timeout would also create a new
unknown-outcome surface — a timeout firing during a commit is indistinguishable
from a lost acknowledgement — which is a real design question rather than a
missing feature.

Kept as-is. Operators should set database-side limits, and the supported route is
`DDL_LOCK_TIMEOUT` on Oracle plus resource manager or `statement_timeout`
policies. Recorded in the remaining-risks section of the acceptance report.

## 10. Oracle `status` needs three round trips — kept

`inspect_metadata` probes objects, then columns, then constraints and indexes,
before `read_snapshot` runs its single consistent statement. For a command meant
to be fast during a long migration this is more chatty than it needs to be.

Measured cost is a few milliseconds against a local database, and the validation
it performs is the thing that distinguishes "uninitialized" from "damaged", which
is load-bearing. Not worth trading clarity for.

## 11. Five reporting methods could be one descriptor — open

`capabilities`, `server_description`, `normalized_namespace`, `lock_binding` and
`session_identity` are five abstract methods that a new adapter must fill in
separately. They could be one `describe() -> AdapterDescription`, taking the
abstract count from 27 to 23 and making the contract easier to read.

Not done. The capability notes are live strings that change after `connect()`
(the Oracle `COMMIT_WAIT` note and the PostgreSQL `synchronous_commit` read-back
are computed during session setup), so a frozen descriptor would need a build
step anyway, and the acceptance report and two tests read `capabilities().notes`
directly. The gain is cosmetic and the churn is not. Recorded as available.

## 12. Oracle's transaction-state probe costs a round trip — kept

`has_open_transaction()` executes a PL/SQL block on every call, and the engine
calls it at each `ctx.ddl()` and after every migration returns. Caching it would
be wrong: the whole point is to ask the database rather than trust bookkeeping,
which is precisely the mistake finding 5 came from. The cost is one round trip at
a handful of boundaries per migration.

## 13. Reserved-object refusal matched substrings — fixed

`_reject_reserved` upper-cased the whole statement and asked whether each
reserved name appeared anywhere in it. That refuses far more than it should, and
the rename made it sharper, because `m8` reads like the end of an ordinary word.
All four of these were rejected:

```
CREATE TABLE custom8_history (id INTEGER)
SELECT * FROM platform8_progress
INSERT INTO orders (note) VALUES ('see m8_meta for details')
-- m8_history is the engine table; do not touch
SELECT 1
```

It fails in the safe direction, but it fails on legitimate work: a schema with a
table called `custom8_history` could not be migrated at all, and the message
would blame the author for something they had not done.

The lexer already separates identifiers from comments and literals. `Statement`
now carries `names`, the upper-cased set of its word and quoted-identifier
tokens, built from the token list `normalize()` was already walking, and the
refusal matches against that. Schema-qualified and quoted references still match,
because `.` is not an identifier character to the scanner. The contract suite
asserts both directions, on all three adapters.

## 14. Placeholders were translated after `{now}` — fixed

`_render` substituted the engine's timestamp expression first and only then
rewrote `:name` into `%(name)s` for a pyformat engine. Its sibling `_binds` reads
placeholder names from the *unrendered* template, and its docstring says why;
`_render` did not follow the same rule. A timestamp expression carrying a
PostgreSQL cast corrupted silently:

```
now()::timestamptz   ->   now():%(timestamptz)s
```

Not live, because the one pyformat adapter uses `clock_timestamp()`. That is the
kind of defect worth fixing before it is live: the next adapter, or a change to
this one, would have produced malformed engine-owned SQL with no failing test in
between. The two statements are now in the other order, and a unit test pins it
with a colon-bearing expression no shipped adapter declares.

## 15. Metadata initialization and inspection existed three times — fixed

Finding 4 consolidated the history transitions, the progress store and statement
admission, and deliberately left metadata inspection alone because the dictionary
queries are genuinely different. That was right about the queries and wrong about
the sequence around them. `initialize()` was 31 to 34 lines in each adapter and
differed only in which DDL text to run and whether each create needed its own
commit; the marker insert, which is a durable transition with a real invariant,
existed three times, and two copies spelled the same value differently.

`Adapter` now owns `initialize()` and `inspect_metadata()`, plus `_read_meta()`
and `_count()`, which turned out to need no engine-specific SQL at all once they
were written through the shared renderer. What stayed adapter-owned is what is
actually different: `_objects_present()`, `_definition_problems()` and one new
`_create_metadata_object()` hook, where Oracle submits DDL that commits itself
and the other two wrap it in an engine transaction.

`_objects_present()` now has a stated contract: it returns logical names, so
Oracle folds its dictionary's upper case back rather than making every caller
remember to. That removed the `.upper()` calls scattered through four Oracle
methods.

The gate for this was the live suites, not the fast ones: the interrupted
initialization scenarios parametrise over each independently durable object and
over both sides of the marker.

## 16. The read-only path imported the engine — fixed

`readonly.py` imported `preflight` and `verify_bindings` from `engine.py`. Both
are free functions with no relationship to the `Engine` class, but the import
meant the module whose contract is that it imports no migration code pulled in
the Python unit loader transitively. Both now live in `checks.py`, which
`engine` and `readonly` each import. The module graph stays acyclic and the
layering now matches what the docstring claims.

`run_status` and `run_validate` were also merged. They were 25 and 29 lines and
differed in three places: the command name, whether the active migration's
session is probed, and one extra sentence on the recovery message.

## 17. Eleven unreferenced members and two redundant wrappers — fixed

Verified unreferenced across source, tests, examples and the test environment:
`Snapshot.active`, `Snapshot.by_id`, `Capture.by_id`, `Manifest.by_id`,
`Manifest.at_position`, `hooks.active`, `Statement.is_plsql`, `Report.notes`,
`LoadedUnit.package`, `LoadedUnit.entry_module`, and `RequiredObject`'s unused
`order=True`. Three attributes were written and never read: `_batch_open` on two
adapters, and `_entered`/`_finished` on the batch context.

The two wrappers were `_parse_timestamp` in the Oracle adapter and `_passthrough`
in the PostgreSQL one: the same function, and both already implemented by
`md.parse_iso_timestamp`. The Oracle adapter called both forms in different
methods, which is how that kind of thing gets noticed.

Also removed: the re-export blocks in `engine.py`, `readonly.py` and
`adapters/metadata.py`, which imported names solely to list them in `__all__` and
created dependencies that looked real, and ten empty section headers left by the
earlier consolidation.

`Report.notes` was rendered but never populated, so `status --json` and
`validate --json` no longer carry an always-empty `notes` key.

## 18. The PostgreSQL adapter built SQL two ways — fixed

`_t()` composed object names through `psycopg.sql.Identifier` while the base
rendered quoted, schema-qualified strings; `_t`'s own docstring flagged the risk
of the two paths naming different objects. Every object involved is a
compile-time constant plus one schema name already validated as a bare lower-case
identifier, so the composition layer was buying nothing the base did not provide.
It is gone, along with the `psycopg.sql` import.

`execute` and `query` are now shared too, over one `_run()` hook returning the
driver cursor, as is the transactional `execute_ddl`. `executemany` stays
per-adapter: Oracle passes `batcherrors=False`, PostgreSQL needs a cursor context
manager, and forcing those into one shape would obscure all three, which is the
judgement finding 4 already made about dictionary queries.

One round trip went with it. `update_active_attempt` selected the new attempt
number after incrementing it, although the caller holds the namespace lock and
read the previous value under it. The engine now derives it, and a probe test
asserts that the attempt the author sees through `ctx.attempt` is the attempt
recorded in history, across three attempts.

---

## What the structure looks like now

```text
core, 2,500 code lines, no database dependency
  manifest  fingerprint  paths  staging      capture and source integrity
  sqltext                                    lexical scanning only; no policy
  model  statevalidate                       durable state; pure validation
  checks                                     preflight and binding verification
  engine  context  loader  latch             orchestration and the author facade
  diagnostics                                correlation id and event log
  cli  readonly  reporting                   three commands and their output

adapters, 2,200 code lines
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
the Python unit loader.

## The author-facing API

Reviewed and left alone. It is ten members, and the shape already expresses the
rules:

```python
ctx.migration_id  ctx.attempt  ctx.execute  ctx.executemany  ctx.query
ctx.sql  ctx.log                                    # every mode
ctx.transaction  ctx.ddl  ctx.progress              # restartable only
```

`transaction`, `ddl` and `progress` genuinely do not exist on an atomic context
— they are separate classes, not methods that raise — so an author cannot reach
for them. `ctx.transaction()` returns an explicit context-manager object rather
than a generator, so a batch entered and never exited is detected instead of
being quietly rolled back by garbage collection. No driver handle, cursor or
commit method is exposed.

One thing was considered and rejected: a `ctx.execute_returning()` for Oracle DML
output binds. No migration needs it yet, and the specification is explicit that
it should arrive with a concrete requirement and a test rather than in advance.

## Still not production-ready

The gates in [`ACCEPTANCE.md`](ACCEPTANCE.md) are unchanged by this review. The
dominant one remains Oracle 19c: **NOT RUN**. Thick-mode drivers, wallet
authentication, Windows, performance and an independent security review are also
NOT RUN. This review improved reliability, diagnosability and maintainability; it
did not change what has been tested against.
