# migr8 design and implementation review — 2026-10-10

**Verdict: not releasable until F1 and F7 are fixed.** On PostgreSQL a batch whose author caught a statement error is recorded SUCCESS after the server discarded it. On SQLite a batch whose transaction ended early commits its checkpoint without its data, and the rerun records SUCCESS. F3 and F2 belong in the same release. Every gate passes and covers none of these.

Reviewed revision `06b3a36179fa7657528c1884e3d76ce6daa1c910` on 2026-10-10 with the checkout's `.venv`, Python 3.14.0. This review takes the astra review of the same date as a list of claims, re-derives each from the code and a reproduction, and adds what it missed. Scope: `docs/SPEC.md`, `docs/MANUAL.md`, `docs/ARCHITECTURE.md`, all of `src/migr8`, the three adapters, tests and the live test rig. No implementation file changed. The staged deletion of `astra-4-opus.md` is untouched. Probe scripts and outputs are under the session scratchpad, named per finding. An Opus 5.5 reviewer and a Codex read-only pass challenged the draft; where either changed a ruling, the finding says so.

## Where this review differs from the astra review

| Astra item | Astra | This review | Reason |
|---|---|---|---|
| F1 PostgreSQL commit of an aborted transaction | P1 | P1, restartable batches only | The atomic path exits 3 because its identity probe raises inside the aborted transaction. |
| F2 restartable SQL blocks skip admission | P1, reserved names | P2, wider root cause | The bypass skips every admission check for `BEGIN`/`DECLARE` files on all three adapters. |
| F3 batches lack the identity tripwire | P1 | P2 | No false SUCCESS; the migration stays ACTIVE and restartable code must converge. Both challenge reviewers argued P2. |
| F4 Oracle validity checks swallow transport errors | P2 | P3 | The query is read-only and runs after the latch check. Exit code, cleanup and diagnostic are wrong; no durable state is in doubt. |
| F5 PostgreSQL password concatenation | P2 | P2 | The dropped `database.user` is the worse half. |
| F6 status refused on a pending compile error | P2 | P2 | Confirmed. SPEC 11.2 requires pending units alongside a validation failure. |
| Recovery command drops config and manifest paths | Operator item | P3 | Correct in the default layout; a third copy is at `statevalidate.py:267`. |
| Shared transaction-scope and admission helpers | Recommended | Recommended, narrower cut | See the simplification section. |

F7 and N1 to N13 are new.

## Findings

### F1 — P1: a PostgreSQL batch whose author caught a statement error is committed as rolled back and reported clean

**Evidence.** `src/migr8/adapters/postgres.py:337-338` runs `COMMIT` and ignores its result. `src/migr8/context.py:289` commits on clean batch exit through `base.py:263` with no transaction-state check before or after. `engine.py:474` then finds no open transaction, because the connection is idle after the commit-turned-rollback.

**Reproduction** (`tx/f1_pg.py`, PostgreSQL 17.5, schema `f1_probe_<pid>`, dropped afterwards). A restartable Python unit inserts one row inside `ctx.transaction()`, catches a `UniqueViolation` on a second insert, and returns.

```text
raw: status before COMMIT: INERROR
raw: COMMIT raised nothing; statusmessage = 'ROLLBACK' result status = COMMAND_OK
[restartable] exit_code = 0
[restartable] history: [... (3, 'swallow', 'restartable', 'SUCCESS', 1)]
[restartable] orders: [(0,)]          <- the batch's INSERT id=1 is gone
[atomic] exit_code = 3
```

psycopg does not raise; it returns `COMMAND_OK` with status message `ROLLBACK`. The atomic variant exits 3 because `_check_transaction_identity` (`engine.py:363`) queries inside the aborted transaction and the server rejects it.

**Other adapters.** SQLite rolls back only the failed statement for an ordinary constraint error, so the batch keeps its rows (`tx/f1_sqlite.py`); the rollback-on-conflict forms are F7. Oracle has statement-level rollback and is unaffected by reading (SPEC 5.2).

**Against SPEC.** Not a documented limit. SPEC 5.2 says checks must be defined "through actual driver and database state" and names PostgreSQL's aborted-transaction flag. SPEC 7.1 defines the batch transition as "batch data and any progress updates in the same transaction". The run records SUCCESS and never revisits the migration, so the loss is permanent. Authors do have admitted ways to handle an expected conflict inside a batch: `INSERT ... ON CONFLICT DO NOTHING` and a `DO` block with an `EXCEPTION` clause (`postgres.py:70`, `base.py:1020-1025`). Plain catch-and-continue is the form the engine must reject rather than record.

**Fix.** In `_do_commit`, raise a definite rejection when `self._conn.info.transaction_status` is `INERROR` before sending `COMMIT`; the error type must be one the classifier treats as definite, or it becomes an unknown outcome. `_commit_batch` already turns a definite rejection into exit 3 with ACTIVE retained and `_post_return_cleanup` rolls back. Treating a `ROLLBACK` status message after `COMMIT` as a rejection is a second line. Keep the lost-reply branch unchanged. PostgreSQL returns the `ROLLBACK` tag only from the aborted state, and every abort reaches the client as an error before `COMMIT`, so the pre-check is sufficient. A deferred-constraint failure at `COMMIT` raises SQLSTATE class 23 and stays a definite rejection.

**Acceptance.** Live PostgreSQL: a batch that swallows `UniqueViolation` exits 3, history stays ACTIVE, the table is unchanged. A deferred foreign-key violation at commit exits 3 the same way. Adapter test: `commit()` on an `INERROR` transaction raises. Existing unknown-commit and successful-batch tests still pass.

### F7 — P1: a SQLite batch whose transaction ended early commits its checkpoint without its data, and the rerun records SUCCESS

**Evidence.** SQLite ends the whole transaction on `INSERT OR ROLLBACK`, a `PRIMARY KEY ON CONFLICT ROLLBACK` column, a `RAISE(ROLLBACK)` trigger, and by reading on automatic rollback after `SQLITE_FULL`, `SQLITE_IOERR`, `SQLITE_NOMEM` or `SQLITE_BUSY`. The author catches the `IntegrityError`, which SPEC 5.2 permits. Later batch statements and `progress.set` then autocommit, because `progress_set` consults the adapter's own in-batch flag (`base.py:846`) rather than the connection's `in_transaction` (`sqlite.py:853`). The batch `COMMIT` fails with "no transaction is active", which `sqlite.py:797-802` reports as `CommitOutcomeUnknown`, exit 4. `MANUAL.md:434` tells the operator to rerun.

**Reproduction** (`rd/sqlite_split.py`, output `rd/sqlite_split.out`, both trigger forms):

```text
[or-rollback] run1 exit=4 phase=restartable_batch msg='unknown outcome for restartable_batch commit: ... CommitOutcomeUnknown [SQLITE_ERROR] ...'
   history= [..., ('swallow', 'ACTIVE', 1)] progress= [('swallow', 'batch1', 'done')] orders= [(0,), (2,)]
[or-rollback] run2 exit=0
   history= [..., ('swallow', 'SUCCESS', 2)] progress= [] orders= [(0,), (2,)]   <- row 1 lost for good
```

The migration reads its own checkpoint on rerun, skips the batch, and completes.

**Against SPEC.** SPEC 7.1 requires batch data and progress in one transaction; SPEC 14.2 item 5 lists data and checkpoint coupling as a required scenario. SPEC 13.3 names SQLite's native `in_transaction` state as part of the adapter's enforcement boundary, and the adapter does not consult it inside a batch.

**Fix.** While a batch is open, refuse every facade call and the commit when the connection's `in_transaction` is false, and latch exit 8 with the remediation warning. The F3 check at batch exit comes too late here because the checkpoint has already committed. Reclassify "no transaction is active" at commit as a definite rejection, since it reports a known state rather than a lost reply.

**Acceptance.** Both reproduced variants exit 8 with ACTIVE retained and no progress row written. A rerun re-enters the batch and inserts the missing row. The existing SQLite fault tests and the bounded-filesystem case still pass.

### F3 — P2: a hidden commit inside an Oracle batch is not detected

**Evidence.** `context.py:258-273` begins the batch and Oracle's `_do_begin` (`oracle.py:678-680`) is a no-op. `context.py:275-289` commits or rolls back with no identity check. The atomic path establishes the identity at `engine.py:355` and compares at `engine.py:402-417`. Oracle admits `BEGIN` and `DECLARE` inside a batch (`oracle.py:76-89`, `base.py:1020-1025`). SPEC 5.2 applies the atomic transaction-control rules, including "no hidden transaction boundaries", inside a batch.

**Reproduction** (`tx/f3_oracle.py`, Oracle 23.9 Thin, table `F3_PROBE_<pid>`, dropped afterwards; the real `OracleAdapter`, `build_context` and `_BatchContext`, batch context only, no engine run and no `m8_*` access).

```text
A: batch exited cleanly; latch: OPEN
A: xid before hidden commit 9.9.2171 | right after None | at batch end 3.32.2260
B: batch raised RuntimeError('author failure after the hidden commit') ; latch: OPEN
B: rows: [1, 2, 3, 11, 12]
C: established 9.30.2169 current None -> mismatch: True
C: open txn after empty established batch: False
```

In case B, row 11 was written before the committing block and row 12 inside it; both survive the batch's rollback. Row 13, written after the block, was rolled back. The author sees an ordinary exit 3 and nothing says part of the batch is durable.

**Against SPEC.** Exit 8 is defined for atomic mode only in `MANUAL.md:459`, and SPEC 14.2 lists the tripwire under atomic scenarios. No SPEC sentence states the restartable omission as a limit; the test list leaves it out. `SPEC.md:666` gives one reason for not reading the identity in a batch: the read-only form cannot tell "inside a batch" before the first write. The establishing form creates the id up front, as the atomic path does, and case C shows an empty established batch leaves nothing open.

**Severity.** No false SUCCESS results: the migration stays ACTIVE and re-enters, and restartable code must converge from any durable state (SPEC 5.2). The hidden commit breaks the author's side of the contract, and SPEC 2.3 limits detection of trusted code's indirect effects. The realistic honest mistake is a legacy procedure that commits internally, called from a batch; the engine then neither detects it nor warns, and the checkpoint coupling the author relied on is gone. That is a missing safeguard with a documented workaround, which is P2. The astra review and this review's own first pass rated it P1; both challenge reviewers argued P2, and the reasoning above is why P2 stands.

**Fix.** Establish the identity in `__enter__` after `begin()`. In `__exit__`, unless the latch holds an unknown outcome, compare the identity on both the clean and the exception path before committing or rolling back. On mismatch, latch the violation, roll back the remainder, exit 8, and keep the remediation warning; the check cannot undo the earlier commit. Apply the same hook on PostgreSQL and SQLite; both expose identity hooks (`ARCHITECTURE.md:93`), and on PostgreSQL the read also fails inside an aborted transaction, which gives F1 a second guard. On PostgreSQL the rollback after a latched violation currently re-raises through the guarded probe (`postgres.py:341`, `base.py:210-211`) and is left to `close()`; the fix must route that rollback past the latch check. Keep the F1 and F7 adapter fixes regardless. Reword the exit-8 message at `engine.py:411` and `MANUAL.md:459`, which say "atomic migration".

**Acceptance (live Oracle).** A batch running `BEGIN INSERT ...; COMMIT; END;` exits 8 with history ACTIVE and no later batch. The same block followed by an exception still exits 8, not 3. A read-only batch, a progress-only batch, an empty batch and the existing restartable suite exit 0. A lost reply on the identity probe exits 4.

### F2 — P2: restartable `BEGIN`/`DECLARE` entry files skip admission entirely on all three adapters

**Evidence.** `checks.py:43-46` sends atomic files through `admit_statement` and restartable non-block files through `admit_ddl`; a restartable `PLSQL_BLOCK` gets no admission call. `engine.py:534-540` repeats this at execution. `sqltext.py:347-349` classifies any file whose first word is `DECLARE` or `BEGIN` as a block regardless of adapter, while PostgreSQL and SQLite list `BEGIN` as forbidden and have `allows_plsql=False` (`postgres.py:78,98`; `sqlite.py:103,117`). The bypass therefore skips the capability check and the forbidden-token check as well as the reserved-name check at `base.py:1061-1075`.

**Reproduction** (`adm/f2_admission.py`, offline, no connection).

```text
[oracle]   restartable 'BEGIN DELETE FROM m8_history; COMMIT; END;'  preflight PASS   facade REFUSED (reserved name)
[postgres] restartable 'BEGIN; DELETE FROM m8_history; COMMIT;'       preflight PASS   facade REFUSED (no PL/SQL support)
[sqlite]   restartable 'BEGIN;'                                        preflight PASS   facade REFUSED
[*]        atomic      'BEGIN DELETE FROM m8_history; END;'            preflight REFUSED on all three
```

No test covers a restartable SQL-entry block; `tests/integration/test_postgres.py:529` covers the atomic case only.

**Consequences by adapter.** On SQLite, `sqlite3` refuses text with more than one statement before running anything, so only a lone `BEGIN;` gets through and `engine.py:474` rolls it back. On PostgreSQL, `postgres.py:591-592` executes the text with no parameters on an autocommit connection (`postgres.py:202`), and psycopg 3.3.5 sends a parameterless query over the simple protocol, which executes several statements in one call (`psycopg/_cursor_base.py:456-459`). By reading, a restartable file `BEGIN; UPDATE ...; COMMIT;` passes preflight and runs every statement, against SPEC 5.3's one-statement rule and its transaction-control ban. The live probe (`adm/f2_pg_live.py`) was NOT RUN: the session's permission classifier denied it. Such a file does what its author meant, so this stays P2; it becomes P1 only with a reserved-name reference, which the Oracle reasoning below calls implausible.

**Against SPEC.** Admission is an honest-mistake guard (SPEC 2.3, 5.3; `MANUAL.md:322-327`). On Oracle an accidental write to `m8_*` inside a block is implausible, which alone would be P3. On PostgreSQL a hand-written `BEGIN; ...; COMMIT;` script is an ordinary mistake, and `MANUAL.md:324` promises the tool stops a reference to `m8_history`, `m8_progress` or `m8_meta`.

**Fix.** Route restartable blocks through `admit_statement(mode=RESTARTABLE, in_batch=False)` in preflight and execution, with the decision in `checks.py` so `engine.py` can import it and `readonly.py` need not import the engine. On Oracle this rejects only blocks that use an `m8_*` name as a word, matching `ctx.execute`. On PostgreSQL and SQLite it rejects every `BEGIN`/`DECLARE` file, none of which is legitimate there. Two consequences need a ruling. Preflight covers SUCCESS units (SPEC 11.1), so a PostgreSQL deployment that already applied such a file would fail every command, with no path except reverting the tool, since SPEC 10.1 never admits an edit to a SUCCESS migration; SPEC 11.1 accepts failing loudly, and the release note must say so. Separately, a restartable PostgreSQL `DO $$...$$;` or `CALL p();` file is classified as SQL, sent to `admit_ddl` and refused today (`adm/f2_do_output.txt`) although SPEC 5.2 allows procedural blocks in restartable mode; branching on `first_token in policy.procedural` closes that gap and is a design decision.

**Acceptance.** Preflight refuses each red case above on each adapter without a connection; the engine refuses them without executing; the Oracle MANUAL example block (`MANUAL.md:243-253`) still passes; a PostgreSQL `DO` block in restartable mode passes if the wider fix is chosen.

### F5 — P2: PostgreSQL ignores `database.user` and splices the password into the DSN string

**Evidence.** `postgres.py:195-198` builds `f"{conninfo} password={password}"` and calls `psycopg.connect(conninfo, autocommit=True)`. `Config.user` is read nowhere outside Oracle (`config.py:84-86`). The error text at `postgres.py:208` still tells operators to check `database.user`. Oracle passes `user`, `password` and `dsn` as keyword arguments (`oracle.py:421-428`). `examples/postgres/migr8.toml:5-6` and both test fixtures (`tests/test_adapter_contract.py:92,108`, `tests/integration/conftest.py:230,264`) put the user in the DSN as well, which hides the defect.

**Reproduction** (`adp/f5.py`, intercepted `psycopg.connect`, parsed with `conninfo_to_dict`, synthetic credentials).

```text
1 plain DSN + database.user='migrator'  -> parsed: no user key
2 password with a space                  -> ProgrammingError: missing "=" after "horse"
3 password 'x host=other.invalid'        -> host: other.invalid
4 URI DSN + MIGR8_PASSWORD               -> ProgrammingError: unexpected spaces found
6 password a\b                           -> one backslash lost
```

**Severity.** Valid passwords fail or change; URI DSNs cannot be used with `MIGR8_PASSWORD`; a configured user is dropped and libpq falls back to `PGUSER` or the OS user. By reading, under peer authentication the migration then runs as another role and owns the objects it creates. The docs promise nothing about PostgreSQL `user`; the MANUAL has no PostgreSQL example.

**Fix.** Call `psycopg.connect(dsn, autocommit=True, **kw)` with `user` and `password` as keywords when set (`adp/f5_fix.py` shows `make_conninfo` handling all five inputs). Precedence, which SPEC does not define and the MANUAL must: if `database.user` and a DSN user key differ, refuse with a configuration error; if equal, accept, so the shipped example keeps working; otherwise whichever is set wins. The password comes from `MIGR8_PASSWORD` only; refuse a DSN that carries `password`, which `config.py:3` states and the code does not enforce. With nothing set, leave libpq's `.pgpass` and `PGPASSWORD` fallbacks alone and document them.

**Acceptance.** Offline unit test intercepting `psycopg.connect` for the five cases plus the user conflict, asserting exact keyword arguments without logging credentials. An integration fixture whose DSN carries no `user=`. A PostgreSQL configuration example in the MANUAL.

### F6 — P2: `status` returns only the preflight error when a pending unit does not compile

**Evidence.** `readonly.py:245` runs `preflight(capture, adapter)` before `adapter.connect()` at line 248; the exception reaches the CLI failure renderer and no history is read.

**Reproduction** (`adm/f6_status.sh`, SQLite). Two applied units plus a pending Python unit with a syntax error:

```text
status --json -> exit 2, outcome validation_failed,
  message "migration 'broken-py' does not compile: migration.py:1:17: expected ':' [phase=preflight ...]"
  (no history, no pending list, no namespace state)
control without the broken unit -> full history, success_count 2, exit 0
```

**Against SPEC.** SPEC 11.2: status "reports pending units even when it can also identify a validation failure. Its exit code reflects that failure". Compile-before-connect is required for `migrate` and `validate` (SPEC 11.1 item 1, 14.2 item 11), not for `status`. `MANUAL.md:432` tells the operator on exit 2 to read `status`, which returns the same error, so the advice loops.

**Fix.** Scoped to `status` only, since `_run` is shared and `validate` must keep its before-connection order. Keep the preflight error's own exit code (an unsupported-capability error exits 1, not 2). Define precedence when a preflight error coincides with exit 6, 7, a binding error or a recovery condition, under SPEC 11.3's "one place decides". Report both problems; the existing `Report.problem` string can carry both, or the JSON schema can grow a list. Compilation imports nothing, so the read-only rule holds. Manifest-load and capture errors would still block `status`, which is acceptable.

**Acceptance.** The reproduced case exits 2 and includes the applied rows and the invalid pending entry. An ACTIVE row followed by an invalid pending unit reports both. `validate` behaviour is unchanged.

### F4 — P3: Oracle validity checks turn a transport failure into an ordinary validity failure

**Evidence.** `oracle.py:1111` catches every `oracledb.Error` from the required-object query and returns a failure; `oracle.py:1174` does the same for the compiler-message lookup and blames privileges. The operation guard (`base.py:1128-1133`, `base.py:216-231`) never sees an exception, contrary to its own docstring at `base.py:198-199`. `_require_valid` (`engine.py:488-497`) turns the failure into exit 3, and `_terminate` then closes the connection normally, which submits a `LOCAL_TRANSACTION_ID` probe on the dead session. No test covers either path.

**Reproduction** (`adp/f4.py`, fake connection, driver-shaped `ORA-03113`):

```text
A: required-object query  -> ValidityResult failure, latch open, close() submits the transaction-id probe
B: ALL_ERRORS lookup, INVALID object -> "(compiler messages are not readable with the current privileges)"
C: ALL_ERRORS lookup, warnings only  -> validity passes
F: same error propagated            -> UnknownOutcomeError, exit 4, phase final_validity
```

In case C, by reading, the next guarded call is the transaction-state probe inside `complete_active_row`, which latches exit 4 under phase `restartable_completion`.

**Severity.** No durable state is in doubt: the query is read-only, runs after author code returned and the latch was checked, and ACTIVE is retained. The exit code is 3 where `inspect_metadata` would give 4 for the same error, cleanup SQL is attempted after a communication failure contrary to `base.py:198-199`, the diagnostic blames privileges, and case C misattributes the phase. The only route to a wrong durable state is Thick-mode failover, which is N7.

**Fix.** In both handlers, re-raise unless `classify_exception(exc)` is a server rejection: a non-zero `ORA-` code outside the transport list, or a listed client-side `DPY-` code. Make the fallback text name the `ORA-` code and claim a privilege problem only for `ORA-00942` and `ORA-01031`. This fix inherits the classifier gap in N3.

**Acceptance.** Unit test with a fake cursor injecting `ORA-03113` into each query: `UnknownOutcomeError`, latch `unknown_outcome`, no further `execute`. `ORA-00942` and `ORA-01031` controls still return failure or fallback text. Engine-level test asserting exit 4 and a discarded connection.

### N1 — P2: an interrupt during cleanup overwrites the engine's exit code, including a latched exit 4

**Evidence.** `cli.py:295-305` handles `KeyboardInterrupt` with "interrupted before any migration work began" and returns 3. `cli.py:130-139` turns SIGTERM and SIGHUP into `KeyboardInterrupt`. Nothing shields `_terminate` (`engine.py:162-183`, including `adapter.discard()`), the close after a clean run (`engine.py:157-158`) or staging cleanup (`cli.py:164-168`). On the server adapters `close()` makes about two round trips (`oracle.py:557-559`, `postgres.py:266-268`), which is a real window.

**Reproduction** (`hunt/probe_interrupts.py`, SQLite, `KeyboardInterrupt` injected at fixed points rather than by signal timing). A first interrupt inside a durable COMMIT latches exit 4; a second during `discard()` yields exit 3 with the "before any migration work began" message while history shows the migration as SUCCESS. A SIGTERM-shaped interrupt during staging cleanup after a clean run gives the same exit 3 and message with both migrations SUCCESS.

**Against SPEC.** SPEC 11.3: one place decides the exit code, and the latched failure is what the run reports. An operator reading exit 3 and that message will assume nothing happened.

**Fix.** Once `Engine.run` has produced a report, keep its exit code through teardown and rendering, for SIGINT, SIGTERM and SIGHUP alike; switch a `close()` that is interrupted to `discard()`; and claim "before any migration work began" only when `Engine.run` was never entered. Do not mask signals: no call timeout is set (SPEC 12), so a close on a black-holed link blocks until the TCP timeout, and a masked process would then ignore the operator until a SIGKILL that leaves no report.

### N2 — P3: the reserved-name guard misses quoted and string-embedded names

**Evidence.** `base.py:1069-1074` checks `statement.names`, which `sqltext.py:445-448` builds from words and double-quoted identifiers only. SQLite accepts a single-quoted table name, and a PostgreSQL `DO $$ ... $$` body is one string token.

**Reproduction** (`hunt/probe_admission.py` case D, SQLite). An atomic unit containing `DELETE FROM 'm8_history' WHERE seq = 2;` exits 0; the next `validate` exits 7 with "positions are not consecutive". A SUCCESS row is gone, against invariant 2. The PostgreSQL `DO` variant is from reading only. P3 by the same reasoning as F2 on Oracle: an implausible honest mistake.

**Fix.** Treat a string token as a name only where an identifier belongs (SQLite), and re-tokenize `DO` bodies before the reserved-name check. Folding every string token into `names` would refuse `VALUES ('m8_history cleanup')`, which `base.py:1064-1067` deliberately allows. Land with F2.

### N3 — P2: the three adapters disagree on which commit failures are unknown

**Evidence.** The Oracle transport list (`oracle.py:125-139`) lists `12571` but not `12570`, and omits `12547`, `12609`, `12637`, `00603`, `03156` and `25408`; any unlisted `ORA-` code is a server rejection (`oracle.py:1235-1241`). The driver maps nineteen `ORA-` codes to `DPY-4011` with `is_session_dead=True` that the list lacks (`adp/f4_xref.out`: 22, 31, 45, 378, 600, 602, 603, 609, 1041, 1043, 2396, 3122, 12153, 12547, 12570, 12583, 27146, 28511, 56600). PostgreSQL (`postgres.py:648-654`) treats every SQLSTATE outside class 08 and `40003` as definite, including a `58030` PANIC from a failed WAL fsync during COMMIT, while SQLite (`sqlite.py:797-802`) treats the equivalent storage failure at commit as unknown.

**Failure (reading only).** A batch COMMIT whose reply is lost with `ORA-12570` reports "batch commit was rejected by the server" (`context.py:320-325`): an ordinary failure that author code may catch and retry, applying the batch twice. An atomic migration exits 3 as rolled back where it should exit 4.

**Fix.** Treat `DPY-4011` or `is_session_dead` as a communication failure on Oracle; for commit-capable calls, treat PostgreSQL classes 57, 58 and XX as unknown. Amend SPEC 7.2 to match. Add a test that diffs the adapter list against the driver's table.

### N4 — P2: unit paths differing only in case alias one directory on macOS and the unit runs twice

**Evidence.** `paths.py:116-131` compares `Path` objects; `resolve()` keeps the given case on a case-insensitive filesystem. **Reproduction** (`hunt/probe_case_units.py`, this Mac): entries `path="u1"` and `path="U1"` resolve to the same directory, the run exits 0, the table has two rows and both ids are SUCCESS. By reading, the same manifest fails on Linux. SPEC 3.1 requires duplicate resolved unit paths to be an error. **Fix.** Compare `(st_dev, st_ino)` of unit directories, or reject casefold collisions.

### N5 — P2: driver errors during setup and lock acquisition reach the fallback handler

**Evidence.** `sqlite.py:485-486` re-raises the raw driver error on contention in `_establish_settings`; PostgreSQL `acquire_lock` (`postgres.py:294`) and the session-setup queries are raw driver calls; these land in `engine.py:211-220`. **Reproduction** (`hunt/probe_busy_connect.py`, another process holding EXCLUSIVE): `migrate` exits 3 with "the run failed unexpectedly (sqlite3.OperationalError [SQLITE_BUSY]); uncommitted work was rolled back", and `status` exits 1 through the fallback handler. By reading, a lost connection during lock acquisition exits 3 on PostgreSQL and 1 on Oracle, where `oracle.py:599-604` adds a misleading "Required grants must be established". **Fix.** Wrap setup and lock-acquisition driver errors in `UsageError`, or `LockNotAcquiredError` for contention, inside each adapter.

### N6 — P2: a non-UTF-8 SQL file exits 3 or 1 instead of 2

**Evidence.** `checks.py:41` and `engine.py:528` call `read_text(encoding="utf-8")` unguarded; `readonly.py:222` catches only `Migr8Error`. **Reproduction** (`hunt/probe_admission.py` case C, bytes ending `caf\xe9`): `migrate` exits 3 with "the run failed unexpectedly (builtins.UnicodeDecodeError); uncommitted work was rolled back", `validate --offline` exits 1. SPEC 11.1 puts structural errors at exit 2 before any connection. **Fix.** Decode in preflight and raise the source-error type on `UnicodeDecodeError`.

### N7 — P2: Oracle Thick mode does not refuse transparent failover

**Evidence.** The Thick-mode check at `oracle.py:438-454` verifies the driver mode and nothing about failover; `oracle.py:428` says transparent reconnect and replay are not used and that Thin mode implements neither. `docs/ORACLE-CONNECTIONS.md:24,76` documents Thick mode and failover descriptions in `tnsnames.ora`. **Failure (reading only).** With `allow_thick_mode` and a service that has `FAILOVER_MODE`, an instance failover between migrations is transparent: the next `begin()` runs on a session that holds no `DBMS_LOCK` and has lost `COMMIT_WAIT` and `CURRENT_SCHEMA`, while a second runner can take the lock. This violates SPEC 12 and invariant 6. **Fix.** In Thick mode read `v$session.failover_type` after connecting and refuse anything but NONE; optionally confirm lock ownership before each durable commit.

### N8 — P3: two facade admission gaps on SQLite and PostgreSQL

`base.py:1029` admits `WITH` outside a batch, but `WITH ... DELETE` is DML and autocommits (`hunt/probe_admission.py` case E: exit 0, zero rows left). `base.py:1020` admits CREATE, ALTER, DROP and PostgreSQL TRUNCATE inside `ctx.transaction()` (case F). SPEC is ambiguous on the second: 5.1 allows transactional DDL in atomic mode on these adapters and 5.2 applies "the atomic rules" inside a batch while also saying "no DDL". **Fix.** Settle the SPEC sentence first; then a separate in-batch token set, and reject a top-level DML verb after a CTE outside a batch.

### N9 — P3: anchored regexes accept a trailing newline

By reading and a pure-Python probe: `ID_RE`, `FINGERPRINT_RE` and the Oracle and PostgreSQL identifier patterns use `^...$` with `.match`, so `"a\n"` is a valid id and a fingerprint with a trailing newline is "supported". Ids `a` and `a\n` coexist and print alike; a corrupted stored fingerprint reports exit 2 "changed" instead of exit 7. **Fix.** `re.fullmatch`.

### N10 — P3: the PostgreSQL search path lets the target schema shadow `pg_catalog`

By reading: `postgres.py:233` sets `search_path = "<schema>", pg_catalog`. A function in the target schema named `pg_try_advisory_lock`, `pg_current_xact_id_if_assigned` or `clock_timestamp` shadows the engine's unqualified calls. **Fix.** Put `pg_catalog` first or schema-qualify engine calls.

### N11 — P3: the recovery command omits the selected config and manifest

`engine.py:178`, `readonly.py:260` and the error text at `statevalidate.py:267` emit `migr8 migrate --recover ID`. `cli.py:116-127` resolves `--config` and `--manifest` against the working directory with default names, so the command is correct from the project directory. By reading, with a different `./migr8.toml` whose target has an ACTIVE row of the same id, it admits the amended source against the wrong database. `MANUAL.md:470-471` calls it "the exact command". **Fix.** Build it at the CLI boundary with shell-quoted resolved paths when they were given explicitly.

### N12 — P3: an ACTIVE row's stored language is not validated against the manifest

`statevalidate.py:213` checks the language of successful rows; the ACTIVE branch at `statevalidate.py:230-244` checks id, mode and fingerprint only. A row labelled `sql` whose fingerprint matches the current Python unit passes, and `base.py:752` then overwrites the language during a plain `migrate`, which SPEC 8.5 allows only under admitted recovery. The fingerprint covers the language (SPEC 4.1), so only an edited row reaches this state. Raised by the Codex pass. **Fix.** Add the language comparison to the ACTIVE branch with exit 2.

### N13 — P3: the PostgreSQL snapshot sends COMMIT after a failed read

`postgres.py:532-560` opens a read-only transaction and commits it in `finally`. A transport failure in the history read latches unknown, and the `finally` still sends `COMMIT` on the dead connection, contrary to `base.py:198-199`; the second error also replaces the first. No durable state is involved. Raised by the Codex pass. **Fix.** Skip the `finally` commit when the latch is set, or discard instead.

## Simplification

1. **One batch-scope guard shared by atomic and restartable paths.** Establish, verify and latch in one helper used by `engine.py` for atomic work and by `_BatchContext` for batches, parameterised by what commits at the end. F1, F7 and F3 are three consequences of the batch path owning a weaker copy. Keep atomic history insertion and restartable progress semantics explicit.
2. **One SQL-entry admission decision** in `checks.py`, returning the permitted execution category, consumed by preflight and `_invoke_sql()`. F2 is the divergence; N2 belongs in the same change.
3. **One outcome-classifier table per adapter, cross-checked against the driver.** N3 and the F4 cross-reference show the Oracle list drifting from what `oracledb` marks as session-dead.
4. **Keep** the pure state validator, explicit adapter registry, mode-specific contexts and shared metadata transitions. There is no demonstrated need for a package split, plugin discovery, or a generic catalog query layer. The astra suggestion to share result plumbing between `RunReport`, `reporting.Report` and `_render_failure` is reasonable but third in value behind the two guards; the F6 fix forces part of it.

## Operator perspective

- **Exit codes an operator can act on.** N1, N5 and N6 surface as exit 3 "rolled back" or exit 1 for conditions that are not migration failures. After F1 and F7, these are the next thing an operator notices.
- **PostgreSQL needs a complete MANUAL example** with `database.user`, `MIGR8_PASSWORD` and a URI DSN, once F5 is fixed.
- **Keep destructive convenience commands out.** Agreed with the astra review: undo, forced unlock, automatic history repair and schema adoption are separate protocols. Nothing here needs them.

## Verification and limits

All results obtained on 2026-10-10 from this checkout with its `.venv`, Python 3.14.0, Docker context `podman`, containers `migr8-oracle` (loopback 15210, Oracle 23.9 Thin) and `migr8-postgres` (loopback 15433, PostgreSQL 17.5). No container was provisioned or restarted.

| Gate | Result |
|---|---|
| `ruff check .` | PASS, exit 0 |
| `ruff format --check .` | PASS, exit 0, 80 files (the astra review reports 79 on the same revision; not reconciled) |
| `mypy src` | PASS, exit 0, 28 files |
| `pytest -m 'not oracle and not postgres' -q` | 616 passed, 1 skipped, 162 deselected, exit 0 |
| `testenv/dbctl.sh test -q -rs` | 776 passed, 3 skipped, exit 0 |
| Oracle TLS cases | NOT RUN, 2 skipped: `MIGR8_ORACLE_TLS_ADMIN` unset |
| SQLite bounded-filesystem case | NOT RUN, 1 skipped: `MIGR8_SMALL_FS` unset |
| F2 PostgreSQL multi-statement execution | NOT RUN, permission denied in this session; script at `adm/f2_pg_live.py` |
| F3 end-to-end engine run after a hidden commit | NOT RUN; batch context only |
| F7 automatic-rollback triggers (FULL, IOERR, NOMEM, BUSY) | NOT RUN; the two conflict forms were run |
| N3, N7, N9, N10, N12, N13 and the "by reading" claims above | Reading only, no fault injected |
| Fresh wheel install, Linux, coverage | NOT RUN |

Live probes: F1 ran a full migration through the engine in its own PostgreSQL schema; F3 drove the real adapter and batch context against one Oracle table; both cleaned up. F4 and F5 used a fake connection and intercepted construction. F6, F7, N1, N2, N4, N5, N6 and N8 ran on SQLite in the scratchpad.

**Implementation order.** F1 and F7, then F3, as the shared batch guard. F2 and N2 as the shared admission decision. N3 with the F4 handler change. F5. N1. F6 with the report change. N4, N5, N6. The remainder as time allows. Add each finding's regression before rerunning the gates; the gates pass today and cover none of these.
