# migr8 - Specification v6.1

The maintained specification, kept in step with the implementation in
`src/migr8/`. No database version is certified by this document alone.

Acceptance evidence, including which gates are open, is in
[`ACCEPTANCE.md`](ACCEPTANCE.md).

The primary target is Oracle. PostgreSQL is the second database adapter and a useful comparison target. SQLite is an explicitly limited local probe adapter. Their different roles are specified in §13.

MUST and MUST NOT are requirements; SHOULD permits a documented reason to choose otherwise; MAY is optional. The runner is the migration process. The author writes trusted migration code. The operator chooses the target database and runs the tool.

## 1. Purpose and scope

`migr8` manages one ordered migration sequence for one database migration-history namespace. In the initial Oracle implementation, a namespace is one target schema with fixed metadata object names. One namespace has one migration stream.

The database contains an immutable successful prefix and may contain one active restartable migration immediately after it. A source-controlled manifest defines order. A migration has a permanent identity, a source unit, a language, an execution mode, and any declared final validity requirements.

The command surface is `migrate`, `validate`, and `status`. An explicit `migrate --recover ID` option admits changed source for the matching active restartable migration.

In scope:

- SQL and Python migrations.
- Atomic and restartable execution.
- Source integrity, ordered history, exclusive execution, and roll-forward recovery.
- A small transactional progress store for restartable Python migrations.
- Database-specific adapters and real-database acceptance tests.

Out of scope:

- Automatic undo, reverse migrations, baselining existing schemas, history repair, `clean`, forced unlock, skipping migrations, and out-of-order execution.
- Repeatable migrations, schema diffing, drift detection, and deployment coordination.
- Shell migrations, SQL*Plus interpretation, cross-system transactions, and a generic workflow engine.
- Automatic database-object recompilation, automatic reconnect/replay, and Transaction Guard integration in the MVP.
- A promise that the same migration source is portable between database engines.

The name `migr8` describes a small tool. A thin entry point and a small Python package are acceptable; there is no requirement to put every implementation concern in one physical file.

## 2. Guarantees and limits

### 2.1 Durable state

| State | Durable representation | Meaning |
|---|---|---|
| PENDING | No history row | This identity has not claimed a position or succeeded locally. |
| ACTIVE | One ACTIVE history row | Restartable work has claimed the next position and is unfinished. |
| SUCCESS | One SUCCESS history row | The completion transaction is durable. |

There is no durable FAILED or UNKNOWN state. A compliant atomic migration whose transaction is rolled back leaves no history row. An interrupted restartable migration retains its active row. An unknown outcome describes the runner's knowledge, not a new database state.

A SUCCESS row does not imply that the runner received its commit acknowledgement: the transaction may have committed before communication failed.

### 2.2 Invariants

1. Successful history is exactly the manifest prefix by position, identity, language, mode, and fingerprint. Positions are consecutive integers starting at 1.
2. SUCCESS rows are immutable. The runner never updates or deletes them.
3. There is at most one ACTIVE row. Its position is the successful-prefix length plus 1, its identity matches that manifest entry, and its stored and requested modes are both restartable.
4. An active identity and its position cannot change. Its source definition may change only through the recovery admission rules in §10.
5. Every engine-owned durable transition has defined contents and an explicit recovery rule (§7). Atomic completion includes migration work; restartable completion includes progress deletion. Initialization uses its own completion marker.
6. Oracle and PostgreSQL execution use one physical database session holding the namespace lock for all migration execution and metadata writes. SQLite's local probe exception is explicit in §13.3.
7. All migration-local files executed or read during an attempt come from the same staged unit whose fingerprint was admitted for that attempt.
8. No later migration executes until the preceding migration is durably successful. A failure stops the run.

### 2.3 Limits of the guarantees

History validation does not establish the actual schema or data state. It cannot detect arbitrary manual changes to application tables, or establish that every statement the author intended was executed.

Atomicity covers the supported transactional schema/data effects and the history row. It does not cover external services, independent connections, autonomous transactions, session state, or non-rollbackable allocation counters such as sequences. Ordinary sequence gaps are not evidence of a failed atomicity guarantee. Code must not rely on rolling those counters back.

The engine does not isolate backfills from application writers. Authors must state and satisfy their assumptions about concurrent inserts, updates, deletes, and application compatibility.

Python and database routines are trusted code, not sandboxed. Engine safeguards detect some mistakes; they cannot prove arbitrary indirect effects safe. Contract violations may require manual database remediation.

## 3. Manifest and migration units

### 3.1 Manifest format

One UTF-8 TOML manifest defines the sequence. Its default filename is `manifest.toml`.

```toml
manifest_version = 1

[[migration]]
id = "orders-table"
path = "orders-table"
language = "python"
mode = "restartable"
entry = "migration.py"
require_valid = []

[[migration]]
id = "orders-package"
path = "orders-package"
language = "sql"
mode = "restartable"
entry = "up.sql"
require_valid = [
  { name = "PKG_ORDERS", type = "PACKAGE" },
  { name = "PKG_ORDERS", type = "PACKAGE BODY" },
]
```

Array order is execution order. No sorting by filenames, numbers, or timestamps occurs. Display numbers may be derived from position but are not identities.

Required migration fields are `id`, `path`, `language`, `mode`, and `entry`. `require_valid` defaults to an empty list. Unknown keys, unsupported manifest versions, invalid field types, duplicate ids, and duplicate resolved unit paths are errors.

`id` matches `^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$` and is case-sensitive. It is assigned once and must never be reused for a different migration. Recovery source edits preserve that identity.

`language` is `sql` or `python`; `mode` is `atomic` or `restartable`. Both are explicit. Adapters reject unsupported combinations and never silently change modes.

`path` is a directory relative to the manifest directory. `entry` is a regular file relative to the unit. Absolute paths, traversal, symlinked roots, and paths escaping the manifest tree are rejected. Units cannot overlap or contain one another. The unit's external location is not part of its fingerprint.

All unit-local executable code, helper modules, SQL files, and data files belong inside the unit. Directory-only units are the initial format; a SQL-only unit can contain just its entry file.

### 3.2 Required objects

`require_valid` is a static part of the manifest for both SQL and Python migrations. It is loaded before migration code and included in the fingerprint. There is no runtime `ctx.require_valid()` API in the MVP.

Each entry specifies `name` and `type`. The owner is the configured target schema, not an independently selectable schema. Entries are a set: duplicate pairs are errors, and canonical ordering is defined in §4.

For the Oracle MVP, supported types are `PROCEDURE`, `FUNCTION`, `PACKAGE`, `PACKAGE BODY`, `TYPE`, `TYPE BODY`, `TRIGGER`, and `VIEW`. Names use unquoted uppercase Oracle identifiers in the initial implementation. Quoted mixed-case identifiers, editions, and additional object types require explicit adapter support before admission.

Every compiled object whose usability is necessary for the migration's intended final state must be declared. Authors remain responsible for declarations made necessary by dynamically generated DDL or indirect database calls. The engine does not infer completeness from the first SQL token.

The PostgreSQL and SQLite probe adapters initially reject nonempty Oracle-style required-object lists. Their migration fixtures may use language-native assertions instead. Future adapter-specific validity contracts must remain explicit and fingerprinted.

## 4. Fingerprints and staged execution

### 4.1 Covered definition

The fingerprint covers `language`, `mode`, `entry`, the canonical `require_valid` set, and every regular file's unit-relative path and exact bytes.

It excludes the separately compared id, the unit's external path, and manifest position. External dependency versions and database/runtime state are not captured unless their descriptors are files inside the unit. A fingerprint proves source integrity, not semantic determinism.

### 4.2 Canonical encoding

The first implemented fingerprint format is `fp1`. Earlier specification drafts are not deployed history formats. Once this encoding is implemented and used, it is immutable. Any later encoding receives a new prefix; unsupported prefixes fail validation without rewriting history.

```text
field(name, value) = u32be(len(UTF8(name))) || UTF8(name)
                  || u64be(len(value))     || value

required = u64be(number_of_required_objects)
         || for each required object, sorted by (UTF8(type), UTF8(name)):
              field("type", UTF8(type)) || field("name", UTF8(name))

canonical = bytes("migr8-fingerprint/1\n", ASCII)
          || field("language", UTF8(language))
          || field("mode", UTF8(mode))
          || field("entry", UTF8(entry))
          || field("require_valid", required)
          || field("count", u64be(number_of_files))
          || for each file, sorted by UTF8(relative_path):
               field("path", UTF8(relative_path))
            || field("data", exact_file_bytes)

fingerprint = "fp1:" + lowercase_hex(SHA256(canonical))
```

Omitted and explicitly empty `require_valid` lists encode identically. Merely reordering that set does not change the fingerprint; adding, removing, or changing an entry does.

Paths use `/`, have no empty, `.` or `..` components, and contain no backslashes, control characters, or leading/trailing spaces. Paths must already be Unicode NFC. Reject case-folding collisions within the unit; portable ASCII filenames are recommended. Sorting is by UTF-8 bytes, never locale collation.

Files are read in binary with no newline or whitespace normalization. The migrations tree must use a consistent Git line-ending policy, for example a root `.gitattributes` containing `* -text` or `* text eol=lf`.

Only regular files and directories are accepted. No ignore list applies. Symlinks and special files are rejected. `__pycache__`, `*.pyc`, `*.pyo`, `.pytest_cache`, and `.mypy_cache` within units are errors. `*.pyo` is included for the same reason as `*.pyc`. Empty units and missing/non-file entries are errors. Set `sys.dont_write_bytecode = True` before importing migrations.

### 4.3 Staging

`migrate` loads the manifest into an immutable in-memory definition, stages all its units into a private per-run directory, and fingerprints the staged copies before executing any migration. Copying must reject symlinks and special files rather than following them. The operator must keep the input artifact stable during capture; staging is not an atomic filesystem snapshot of a concurrently edited repository.

Make staged files read-only as a guardrail. This is not a security boundary against trusted Python. Authors must not modify the staged tree. Execute entry files, relative imports, and `ctx.sql()` reads exclusively from staged units. Working-tree changes after staging must not change the attempt.

`validate` and `status` hash files directly without staging, importing code, or executing SQL from migration files. Their result describes the inspected artifact, which must remain stable for the command's duration.

Clean up staging on normal exit and handled failure. A crash may leave a temporary directory; never reuse it for another run.

## 5. Execution contracts

### 5.1 Atomic

The engine establishes a transaction before importing or invoking the migration. All supported work and one SUCCESS history insertion commit together. If execution or required checks fail, roll back. If commit acknowledgement is lost, follow §7; do not infer rollback.

In the Oracle adapter, atomic migration work permits transactional DML, queries, and compliant non-committing PL/SQL. DDL is prohibited. In PostgreSQL and SQLite, supported transactional DDL may also be atomic; the adapter must explicitly admit it.

Migration code cannot commit, roll back, create transaction boundaries, change session settings, use autonomous transactions, or call routines that violate the contract. A transaction method is not exposed in the atomic context. External connections and durable external effects remain outside the guarantee and must not be used as part of the migration's claimed atomic result.

Oracle's transaction-identity tripwire establishes and captures the local transaction id using an engine-owned PL/SQL call with an output bind:

```sql
BEGIN
  :xid := SYS.DBMS_TRANSACTION.LOCAL_TRANSACTION_ID(TRUE);
END;
```

Immediately before inserting SUCCESS, read the id without creating a transaction. A missing or changed id is a contract violation: roll back what remains, insert no success row, stop with the distinct violation exit code, and report that durable effects may require manual remediation. Autonomous effects are invisible to this tripwire. A later run cannot infer from absent history that a previously reported contract violation was repaired; automatic recovery guarantees apply only to compliant code.

Other adapters must either provide an appropriate guard or state their supported enforcement boundary. SQLite's native transaction state and PostgreSQL's driver/server transaction facilities are adapter concerns; do not copy Oracle SQL into the core.

The other two adapters' boundaries are:

- **PostgreSQL** has a real transaction-identity tripwire. `pg_current_xact_id()` assigns and returns the identity; `pg_current_xact_id_if_assigned()` reads it without assigning one. A commit inside the migration leaves a transaction with no assigned identity, which the check detects. PostgreSQL additionally refuses `COMMIT` inside a `DO` block running in an explicit transaction, so most violations never reach the tripwire at all.
- **The SQLite probe** has no server transaction identity. Its stated enforcement boundary is facade statement admission plus SQLite's native `in_transaction` state and an adapter-owned transaction epoch, incremented on every begin, commit, and rollback the adapter performs. A commit reachable only through the facade therefore changes the epoch and is detected; effects reachable some other way are not claimed to be detectable.

### 5.2 Restartable

Before importing or invoking migration code, the engine commits an ACTIVE marker or the next-attempt update. Acknowledged admission is required before invoking code. The migration is always invoked from its entry point on retry.

It must converge from every durable state any admitted version of that identity could have produced, including completed work followed by missing completion metadata. It may inspect state, issue supported DDL, and commit DML in explicit batches.

For Python batching, `ctx.transaction()` owns the batch transaction and commits on clean exit. Within that block, the atomic transaction-control rules apply: no DDL, manual commit, rollback, or hidden transaction boundaries. Direct DML outside a batch block is rejected when the adapter can classify it before execution. Procedural blocks outside batch contexts may own transactions in restartable mode; they are trusted to commit all intended work before returning.

`ctx.ddl()` is available only in restartable mode, outside any batch context and with no open transaction. It executes a single supported DDL statement and never uses pre-DDL implicit commits to flush unrelated pending work.

After code returns, require no open transaction and a usable session. Do not silently commit forgotten work. Roll back remaining uncommitted work, retain ACTIVE, and fail if the return state is invalid.

Oracle has no general PostgreSQL-style aborted-transaction flag after an ordinary statement error. Define checks through actual driver and database state. Authors may deliberately handle expected errors; the engine must not claim to detect every swallowed error or prove semantic completion.

Once final validity checks pass, update the matching ACTIVE row to SUCCESS, set completion time, and delete its progress rows in one transaction. Confirm exactly one matching ACTIVE row was updated. No later work begins until that completion is acknowledged or reconciled by a subsequent run.

### 5.3 Statement safeguards and script semantics

The adapter owns SQL execution. A `.sql` file contains one database statement or one procedural block. The MVP has no multi-statement splitter or SQL*Plus interpreter. Python can sequence multiple local SQL files.

Use an Oracle-aware lexical scanner for leading tokens and terminators. It must preserve literal contents and recognize line comments, non-nested block comments, escaped single quotes, quoted identifiers, and supported Oracle alternative/national string forms. If a form cannot be classified safely, reject it with a specific unsupported-syntax error.

The scanner also recognizes PostgreSQL dollar quoting, `$$ ... $$` and `$tag$ ... $tag$`. Without it the PostgreSQL adapter could not accept a `DO` block at all, because the semicolons inside the body would read as statement separators. Oracle input is unaffected: Oracle never opens a token with `$`, which appears only inside identifiers such as `V$SESSION` and is still scanned as one word. An opening tag with no matching closing tag is an unterminated literal and is rejected rather than guessed at.

For an Oracle non-PL/SQL statement, remove at most one trailing SQL terminator outside comments/literals. Preserve PL/SQL's final semicolon. A final standalone `/` outside literals/comments may be accepted solely as an end-of-file convenience; it is not a statement separator.

Two further rules make "not a separator" concrete. After the optional trailing terminator is removed, a remaining bare `;` outside comments and literals in a non-PL/SQL statement means the file holds more than one statement, and is rejected. A standalone `/`, meaning one alone on its line, anywhere other than at the very end is rejected as a separator attempt rather than split on. Division never occupies a line by itself, so valid arithmetic is unaffected. `CREATE LIBRARY` is not a PL/SQL body and must not be classified as one merely because it creates an object. Use exact tested grammar for identifying stored PL/SQL definitions.

Apply the same normalization to SQL entries and SQL text passed through the Python facade, including `ctx.sql()` results. Python parameters use the native adapter's driver binding convention. SQL entry files have no parameter binding or substitution.

Oracle atomic first-token admission permits `SELECT`, `WITH`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `LOCK`, `DECLARE`, and `BEGIN`; other leading tokens fail before submission. This is an honest-mistake guard, not proof of transitive effects. Oracle restartable direct DDL uses an explicit adapter allow-list. Direct transaction-control and session-setting statements are prohibited in the facade. Engine-owned SQL uses a separate internal path.

Inspect significant tokens at statement boundaries, not line prefixes. A multiline `UPDATE` with a line beginning `SET`, and a PL/SQL `EXIT WHEN`, are valid. An unsupported top-level SQL*Plus command receives an unsupported-command error; never send commands prohibited by the migration-mode checks merely to improve a diagnostic.

Never use `DBMS_SQL.PARSE` as a read-only DDL preflight: it can execute the DDL. Backend syntax validation happens during execution. Preflight guarantees cover structural checks and supported lexical rules, not arbitrary SQL semantics.

## 6. Final validity checks

For the Oracle required-object set, completion checking is read-only. There is no automatic compilation and no touched-object or baseline-difference acceptance rule.

For every declared `(target_schema, name, type)`:

1. Resolve the exact object through the adapter's documented dictionary views.
2. Fail if it is missing, has the wrong type, is ambiguous in the supported namespace, or cannot be inspected with the current privileges.
3. Require `STATUS = 'VALID'` and no matching `ALL_ERRORS` row with `ATTRIBUTE = 'ERROR'`.
4. Report compiler warnings separately without failing solely because of warnings.

Inspect all requirements after execution, using a consistent read where supported. All must pass on every attempt, including attempts that skipped already-completed creation work. An empty set is allowed where no compiled-object requirement applies. It is the author's responsibility to declare the complete required set.

Authors explicitly compile objects when necessary, with object-specific Oracle syntax and an appropriate restartable migration. For example, compiling a package body uses `ALTER PACKAGE ... COMPILE BODY REUSE SETTINGS`; there is no universal `ALTER <dictionary object_type> ...` template.

Existence alone also does not establish intended columns, indexes, constraints, or backfill completion. Migration code must check relevant definitions and state, including constraint validation or index usability where required. These checks are not a generic schema-diff feature.

## 7. Durable transitions and unknown outcomes

### 7.1 Engine-owned transitions

| Transition | Transaction contents | Recovery after lost acknowledgement |
|---|---|---|
| Metadata object created | One metadata object's creation | Inspect which objects exist and complete the permitted prefix under Section 8.4. |
| Initialization complete | The `m8_meta` singleton row, after metadata objects have been created and verified | Inspect marker, object definitions, and existing rows under the lock. |
| Atomic completion | Supported migration work plus one SUCCESS row | SUCCESS present: validate and skip. Absent after exclusive reacquisition: retry compliant work. |
| Restartable admission | ACTIVE creation, or its attempt/accepted-definition update | No code runs until acknowledged. A later run inspects ACTIVE and admits a new attempt. |
| Restartable batch | Batch data and any progress updates in the same transaction | Retain ACTIVE; migration inspects data/checkpoint and converges. |
| Restartable completion | ACTIVE to SUCCESS, completion time, progress deletion | SUCCESS: skip. ACTIVE: re-enter code, including the completed-work case. |

Oracle metadata-object creation and migration DDL can commit independently. They are governed by recoverable initialization and restartable execution, respectively, not disguised as history-only transactions.

The two initialization transitions are named separately because they recover differently and because an acceptance harness must be able to target each one. Object creation is `metadata_object_created`; the marker is `initialization_complete`. On Oracle each object's `CREATE` commits implicitly, so only the marker carries an engine-issued commit. On PostgreSQL and the SQLite probe each object is created in its own explicit transaction.

### 7.2 Failure handling

An acknowledged synchronous commit is durable within the supported database durability configuration. A communication failure during commit, DDL, or another commit-capable operation is an unknown outcome unless the adapter can prove otherwise.

On unknown outcome: stop migration execution immediately, do not retry the operation, do not reconnect and continue, and do not attempt additional SQL cleanup. Close/discard the connection and report the operation, migration, and phase with exit code 4. A new invocation must acquire the namespace lock and reconcile durable state.

A known server rejection of a statement is distinct from communication loss. Under compliant atomic execution, errors before submission of the completion commit can be rolled back. Under restartable execution, a definite statement error may still follow earlier durable effects or Oracle's pre-DDL commit. Retain ACTIVE regardless.

Do not label an operation non-committing solely because its first token passed a lexical allow-list; routines and indirect effects require the trusted-author contract. Driver error allow-lists must be narrow and justified by driver behavior. Unclassified communication failures are unknown.

A driver-side refusal raised *before* anything is submitted is as definite as a server rejection: no durable effect is possible. The implementation therefore treats an outcome as definite in exactly these cases, and everything else, including an unrecognized exception or an unlisted driver code, as unknown:

- **Oracle:** an `ORA-` error number returned by the server that is not in the adapter's transport-failure list, or a driver code in the adapter's enumerated client-side list. An unlisted `DPY-` code stays unknown on purpose.
- **PostgreSQL:** a SQLSTATE outside class `08`, the connection-exception class. A `psycopg` error with no SQLSTATE is unknown unless it is a `ProgrammingError` or `NotSupportedError`, which are raised client-side.
- **The SQLite probe:** every `sqlite3.Error` is definite, because the library runs in-process and there is no transport to lose. The probe therefore produces no unknown outcomes and is not evidence for this contract.

An unknown batch outcome may be caught by user code accidentally. The facade must latch the run as unusable and refuse further calls or successful completion. Batch context cleanup must not issue a rollback after an unknown commit outcome. This prevents a caught exception from allowing execution to continue.

A dead client may leave a live server session executing its submitted call. A fresh runner proceeds only after acquiring the lock, not after observing that the previous operating-system process has disappeared.

An interruption counts as a communication failure when it lands inside a commit-capable call. Ctrl-C, `SIGTERM` and `SIGHUP` during a commit leave exactly the same doubt as a lost acknowledgement: the request may already be durable. The implementation therefore latches the run as unknown, discards the connection without further SQL, and exits 4. An interruption *outside* such a call is an ordinary failure: uncommitted work is rolled back, a restartable migration stays ACTIVE, and the exit code is 3.

`SIGTERM` must be handled explicitly for this to hold, because the default disposition ends the process with no unwinding at all. A runner that can be stopped by a supervisor or an orchestrator must install a handler, or a stop during a commit abandons the session with nothing recorded.

## 8. Locking, metadata, and initialization

### 8.1 Lock contract

All cooperating runners for a namespace must contend on the same lock. The lock is acquired before any metadata mutation or migration code and held across all commits until the run ends.

For Oracle use `DBMS_LOCK.REQUEST` with exclusive mode and `release_on_commit => FALSE`, through the real package or a documented wrapper. An explicitly configured integer in `0..1073741823` is recorded in `m8_meta`. All subsequent runs verify that binding before migration work. Wrong bindings fail; the runner does not switch locks mid-run.

The recorded binding is the lock id alone, rendered `dbms_lock:<id>` for Oracle and `advisory:<id>` for PostgreSQL. `DBMS_LOCK` user locks are global by id, so a documented wrapper package is an access path rather than a different lock namespace. Recording the package name would make a wrapper change look like a binding mismatch while two runners were in fact contending on the same lock. The provider is recorded separately in `lock_provider`, and the configured package name is still validated as an identifier before being used to build SQL.

Return codes: 0 means acquired; 1 timeout and 2 deadlock mean exit 5; 3 parameter error and 5 illegal handle mean configuration failure; 4 already owned is an internal lifecycle error. Acquire once per run. Release once on normal exit, and close the physical session. Never release a lock through another connection after losing the owning session.

The wrapper contract is `REQUEST(id, lockmode, timeout, release_on_commit)` and `RELEASE(id)` with equivalent semantics. Required grants must be established before running migrations; the engine must not fall back to an ineffective lock.

Lock timeout is an operator policy, independent from database statement duration. Diagnostics must distinguish contention from a previous server session that has not ended. Do not promise an upper bound based solely on dead-connection detection settings. There is no lease, heartbeat, lock-breaking command, or active-marker-based concurrency lock.

### 8.2 Logical metadata schema

Adapters implement the following fixed logical objects. Physical SQL types are adapter-owned and must be checked by integration tests. Store no passwords or full connection strings.

`m8_history`, one row per migration:

| Field | Meaning |
|---|---|
| `seq` | Positive consecutive manifest position; unique. |
| `migration_id` | Permanent case-sensitive id; primary key. |
| `fingerprint` | Accepted definition for the latest admitted attempt, final after success. |
| `first_fingerprint` | Definition at first admission; retained after recovery edits. |
| `language`, `mode` | Accepted current language and immutable execution mode. |
| `status` | ACTIVE or SUCCESS. |
| `attempt` | Positive admission count for restartable; NULL for atomic. |
| `started_at`, `last_attempt_at`, `finished_at` | Database timestamps; completion time only for SUCCESS. |
| `runner_host`, `runner_user`, `runner_pid`, `db_session` | Optional latest-attempt diagnostics. |
| `tool_version` | Version responsible for the latest transition. |

Enforce primary-key and position uniqueness, valid state/mode/language values, positive positions, and at most one ACTIVE row. ACTIVE must imply restartable. Enforce appropriate nullability for timestamps and counters. An Oracle function-based unique index on `CASE WHEN status = 'ACTIVE' THEN 1 END` is an appropriate one-active constraint.

Two physical details follow from the target engines.

`MODE` is an Oracle reserved word. The Oracle column is created and referenced as `"MODE"`, because an unquoted `mode` raises ORA-03050, and its bind placeholder is named `exec_mode`, because a `:mode` placeholder raises ORA-01745. It still appears as `MODE` in the data dictionary, so definition validation is unaffected.

PostgreSQL uses a partial unique index, `UNIQUE (status) WHERE status = 'ACTIVE'`, the native equivalent of Oracle's function-based index. Both are tested to be enforced by the database, not only by the runner.

`m8_progress`: `(migration_id, prog_key)` primary key, `prog_value`, and database `updated_at`. It references an existing history id. Engine validation rejects progress attached to SUCCESS, unrelated ids, or any state other than the current ACTIVE migration. Its mutating API is available only inside restartable batch transactions.

`m8_meta`: a single fixed-key row containing `layout_version = 1`, adapter identity, lock binding, target namespace, and database initialization time. A fixed singleton key plus a check constraint enforces one row. Namespace bindings must use normalized database identifiers, not caller spelling that can refer to the same schema differently.

Current and first fingerprints are not a full attempt audit. If source changes A to B and later back to A, equality of the first and last fingerprints does not prove that no recovery edit occurred. Logs may retain intermediate fingerprints; no correctness rule depends on them.

### 8.3 Definition and row validation

Validate metadata object types, columns, relevant lengths/precision/scale, nullability, constraints and their participating columns/expressions, enabled/validated state, and the one-active index's uniqueness and usability. Apply binary/case-sensitive identity semantics. Compare normalized semantic definitions rather than database-generated constraint names alone.

For Oracle, use `ALL_*` views filtered by exact target owner, even when the connection user differs. Probe inaccessible objects as an error, not as absent. Reserve metadata names and prohibit migration code from modifying them outside the supplied progress API.

After reading a consistent snapshot, validate consecutive positions, exact successful prefix, stored language/mode consistency, active position/id/mode, fingerprint formats, row-state consistency, and progress ownership. A database primary key does not by itself make an identity immutable; immutability is also a runner update rule.

These checks split across two exit codes, because they mean different things to an operator.

Exit 7, metadata damaged: the rows are internally inconsistent, or use an unsupported layout or fingerprint format. No manifest could make them valid. Examples are a position gap, two ACTIVE rows, an ACTIVE row recorded as atomic, an ACTIVE row with no attempt count, a SUCCESS row with no completion time, an atomic SUCCESS row carrying an attempt count, a stored fingerprint in an unknown format, and progress attached to a SUCCESS row or to an unrelated identity.

Exit 2, validation failed: the rows are self-consistent but disagree with the manifest or its source. Examples are a SUCCESS row whose fingerprint differs from the current source, an identity or position that does not match, a stored language or mode differing from the manifest, history longer than the manifest, and an ACTIVE migration the manifest now declares atomic.

The split is implemented in a pure function over an already-read snapshot, so it is testable without a database.

### 8.4 Initialization

Under the lock, inspect before creating:

- If the `m8_meta` completion row exists, every expected object must exist and match. Namespace, lock, adapter, and layout bindings must match. Missing objects or conflicting state are damage; recreate nothing.
- If the marker is absent, automatic completion is permitted only where all existing metadata objects have compatible definitions and existing history/progress tables are empty. Populated history or progress without a marker is damage.
- In a permitted incomplete initialization, create missing objects in fixed order, verify the complete layout, then insert and synchronously commit the singleton marker last.
- Any race, unexpected object, conflicting definition, privilege failure, or ambiguous inspection stops initialization. Never overwrite or silently alter an object to pass validation.

The scheme cannot distinguish a never-initialized database from one whose entire migration metadata was removed. Total metadata loss is outside automatic recovery; use controlled restoration and operational safeguards. Absence of metadata must not be presented as proof that application objects have never been migrated.

`validate` and `status` execute no initialization. They report uninitialized compatible empty metadata separately from damaged or incompatible metadata.

### 8.5 Attempts

The first restartable admission inserts ACTIVE with attempt 1, both fingerprints set to the admitted value, and start/last-attempt times. Later admission updates only the matching ACTIVE row, increments the counter, updates latest-attempt diagnostics and any permitted changed definition, and commits before import/invocation.

Confirm an affected-row count of exactly one for attempt and completion updates. Mode, position, id, initial timestamp, and first fingerprint never change during recovery. Updating language is allowed only as part of an explicitly admitted source recovery.

The counter counts durable admissions, not proven executions: a crash may happen after admission but before code runs. It is diagnostic and must never decide whether work is skipped. Atomic attempts are not counted because failed ones leave no durable row.

## 9. Python migration interface

### 9.1 Loading

The entry module exposes a synchronous callable `migrate(ctx) -> None`. It may contain additional helpers; there is no requirement that it contain exactly one callable in total.

Register a private package for the staged unit, using an injective name such as `_migr8_unit_` plus hex-encoded UTF-8 id bytes. Relative sibling imports resolve inside that staged unit. Do not add units to global `sys.path`. Use a fresh module namespace for each run and remove its loaded modules when finished if the runner can be invoked repeatedly in one Python process.

Import only after atomic transaction establishment or acknowledged restartable admission. `validate`, `status`, fingerprinting, and planning do not import migration code. Import-time durable external effects, background mutation tasks, and changes to the staged unit violate the author contract.

The runtime and dependency environment must be pinned for repeatable deployment. Stdlib behavior follows the pinned Python runtime. External dependencies should be version-pinned, and migration-specific helpers should normally be inside the unit. The fingerprint does not imply that external libraries or database routines were hashed.

### 9.2 Context API

| Member | Contract |
|---|---|
| `ctx.migration_id` | Current immutable identity. |
| `ctx.attempt` | Committed admission count for restartable; None for atomic. |
| `ctx.execute(sql, params=None)` | One statement; affected-row count for supported DML. |
| `ctx.executemany(sql, parameter_sets)` | Supported DML over parameter sets; total affected-row count. Per-row error continuation is disabled in the MVP. |
| `ctx.query(sql, params=None)` | One query; a list of tuples in selected-column order. |
| `ctx.sql(relative_path)` | Read text from a regular SQL file within the staged unit; reject absolute/traversing/escaping paths. |
| `ctx.log(message, **fields)` | Structured log correlated with this run and migration; exclude secrets. |
| `ctx.transaction()` | Restartable only; non-nested batch context yielding the same facade, commits on clean exit, rolls back on ordinary failure. It returns an explicit context-manager object rather than a generator-based one, so a batch entered and never exited is deterministically detectable instead of being silently rolled back by garbage collection. |
| `ctx.ddl(sql)` | Restartable only; one admitted DDL statement outside a batch and with no open transaction. |
| `ctx.progress.get(key, default=None)` | Restartable only; stored string or the supplied default when absent. |
| `ctx.progress.set(key, value)` | Restartable only, inside its current batch transaction; never commits independently. |

Progress keys are nonempty strings of at most 128 ASCII characters. Values are nonempty strings with at most 4000 UTF-8 bytes and must be representable in the target database character set. Empty strings are rejected explicitly so Oracle's empty-string/NULL behavior does not change the API. Values, defaults, and conversions are not implicit typed serialization.

The driver connection, cursors, and commit methods are not intentionally exposed. This is API discipline, not sandboxing. Out binds, streaming cursors, async execution, nested transactions, and generic stored-procedure adapters are deferred until required by a concrete migration. Bounded `query` batches are sufficient for the initial backfill.

When an ordinary database error is caught by author code, the author must still satisfy final-state and transaction contracts. An engine-latched unknown outcome or contract violation cannot be cleared by catching its exception.

### 9.3 Backfill example

The following Oracle example assumes a stable set of positive ids, no competing changes to the selected rows, and that assigning `EU` is the intended transformation. Adapt those assumptions explicitly for real application traffic. The migration manifest uses `language = "python"`, `mode = "restartable"`, and an empty required-object list.

```python
def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                """SELECT id FROM orders
                   WHERE id > :after AND region IS NULL
                   ORDER BY id FETCH FIRST :batch_size ROWS ONLY""",
                {"after": last, "batch_size": 1000},
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            changed = tx.executemany(
                """UPDATE orders SET region = :region
                   WHERE id = :id AND region IS NULL""",
                [{"id": key, "region": "EU"} for key in ids],
            )
            if changed != len(ids):
                raise RuntimeError("batch membership changed")
            last = max(ids)
            ctx.progress.set("last_id", str(last))
        ctx.log("batch committed", last_id=last, rows=len(ids))
```

The checkpoint comes from the exact selected-and-updated batch and commits with it. The example does not claim that a later insert below the checkpoint will be covered. An idempotent predicate or an explicit application-write strategy is still necessary when those assumptions do not hold.

Do not implement DML RETURNING as a query result by assumption. Oracle returns these values through output binds. If a later migration needs them, add a small explicit, tested output-bind API and update this specification.

## 10. Recovery and publication

### 10.1 Active recovery admission

An unchanged ACTIVE definition can be retried by plain `migrate`. If its fingerprint differs, plain `migrate` stops with exit 2 and reports the recorded and requested fingerprints plus the exact recovery command.

`migrate --recover ID` is valid only for the existing active identity. Missing ACTIVE state, the wrong id, a changed position, changed restartable mode, or any mismatch in the successful prefix causes failure before metadata mutation. The flag does not admit edits to successful migrations or unrelated validation failures.

For admitted recovery, preserve id, position, mode, first fingerprint, and original start time. Atomically update current fingerprint, current language, attempt count, and latest-attempt diagnostics. SQL-to-Python recovery is allowed; switching to atomic is not. The staged definition and static required set govern the new attempt.

Recovery edits must converge from durable states produced by every earlier admitted source version, not just the first one. Existing progress rows remain and must be understood or safely adapted by the amended migration inside its transaction contracts. A progress format change is part of recovery logic.

The flag is an explicit operator action, not proof that the operator has reviewed the diff. There is no required fingerprint-prefix argument and no claim that first/latest fingerprints are a full audit journal.

### 10.2 Shared environments

A migration successful in any shared environment is published and must be treated as immutable in source control. The runner enforces immutability only against the current database. It has no publication registry or knowledge of other environments.

Teams should choose a visible publication event, usually release-artifact publication or merge to a deployment branch. A published migration's recovery in another environment must respect the already-published source. Options include fixing an external condition, or controlled application-schema/data remediation under the same migration lock so the original source can converge. Successful history and active identity remain intact. Restore from a suitable backup if necessary.

An unpublished active migration can be amended and admitted with `--recover`. If it has already succeeded elsewhere, admitting an edit locally creates incompatible fingerprints across environments; the tool cannot resolve that conflict automatically.

There is no supported abandonment, marker deletion, manual success insertion, or history-rewriting procedure. Administrative intervention in metadata is outside this protocol and must not be described as ordinary recovery.

## 11. Commands, configuration, and reporting

### 11.1 migrate

```text
migr8 migrate [--config PATH] [--manifest PATH] [--recover ID]
```

1. Load and structurally validate configuration and manifest. Validate supported modes, required-object declarations, paths, and lexical rules that can be checked without executing code.
2. Capture the manifest, stage all units, and fingerprint them. No migration code runs.
3. Connect, establish adapter session settings, and acquire the namespace lock.
4. Inspect or complete allowed initialization. Verify namespace and lock binding.
5. Read consistent metadata and validate the successful prefix, active state, progress, and fingerprints. Resolve recovery admission without changing an invalid state to make it pass.
6. Execute the pending suffix in order, starting with ACTIVE if present, using the protocols in §5 and §7.
7. On ordinary completion/failure, release resources and remove staging. On unknown outcome, discard the connection without further SQL.

All structural and fingerprint errors must be found before migration execution. SQL syntax/semantic errors requiring execution, unavailable external dependencies, and migration-specific final-state failures may still occur later. Do not claim that preflight establishes every pending migration will succeed.

Preflight covers every unit in the manifest, not only the pending suffix, because step 1 happens before any history is read. One consequence is recorded deliberately: if a future change to the lexical scanner rejected a form an already-successful migration uses, `migrate` would fail with exit 2 even though the database state is sound. A published migration's source cannot change, so only a tool change can cause this, and failing loudly is preferred to skipping checks on the part of the manifest that defines the successful prefix.

A waiting runner may acquire the lock after another runner has finished, validate, find no pending work, and exit successfully. This is correct, not a lock-test failure.

### 11.2 validate and status

```text
migr8 validate [--config PATH] [--manifest PATH] [--json]
migr8 status   [--config PATH] [--manifest PATH] [--json]
```

Both are read-only application operations: no metadata creation, staging, imports, migration SQL, recompilation, or migration lock. Use a short consistent metadata read; a single query where feasible, otherwise a read-only/snapshot transaction implemented by the adapter. Do not hold it open while waiting for user input.

For Oracle the single query is not merely preferred, it is required. A `SET TRANSACTION READ ONLY` snapshot that reads a table whose definition changed in the same second raises ORA-01466. That happens on every run which has just created metadata or executed migration DDL, which is exactly the long migration `status` exists to observe. The Oracle adapter therefore reads history, progress, and the marker in one `UNION ALL` statement, which is read-consistent in Oracle without a transaction. PostgreSQL uses `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` and the SQLite probe uses `BEGIN DEFERRED`; neither has that restriction.

An uninitialized compatible namespace is reported distinctly. Missing objects after completed initialization, incompatible definitions, orphaned rows, or invalid history are damage/validation failures. Missing access privileges are not evidence of uninitialized state.

`validate` checks the manifest and history contracts. An active fingerprint mismatch is a recovery-required condition and returns exit 2. It is not permission to modify the active marker.

`status` reports id, position, state, mode, language, admitted/current fingerprint comparison, first/latest fingerprint comparison, available timestamps, admission count, and optional latest-attempt diagnostics. It reports pending units even when it can also identify a validation failure. Its exit code reflects that failure rather than silently returning a clean result.

Status remains useful during a long migration. Oracle and PostgreSQL should return committed metadata promptly without waiting for the migration lock. Server availability and ordinary database resource contention still apply; this is not an absolute no-blocking guarantee.

Session-liveness diagnostics are optional and separate from the consistent history snapshot. A session match requires a full usable identity, not a reusable SID alone; include Oracle instance/SID/serial information where available. Without adequate privileges, report unknown. Session existence is not proof that the migration is currently executing or holding the lock.

### 11.3 Exit codes

| Code | Meaning |
|---|---|
| 0 | Command completed and applicable validation passed. |
| 1 | Usage, configuration, unsupported capability, connection setup, or namespace/lock binding error. |
| 2 | Manifest/history/source validation failed, including recovery admission requirements. |
| 3 | Ordinary migration failure: compliant atomic work rolled back, or restartable remains ACTIVE. |
| 4 | Operation outcome unknown after communication failure; rerun must reacquire and reconcile. |
| 5 | Migration lock not acquired within policy. |
| 6 | Namespace not initialized; read-only commands only. |
| 7 | Metadata damaged or incompatible with the supported layout. |
| 8 | Detected transaction-contract violation; durable effects may require human remediation. |

Do not silently retry failed migrations within one run. Exit 4 permits a fresh invocation under the recovery protocol; it does not authorize repeating the last statement. Failure diagnostics name the phase and identity without exposing credentials or sensitive bind values.

### 11.4 Configuration

An Oracle configuration can have this shape:

```toml
[database]
adapter = "oracle"
dsn = "localhost:1521/FREEPDB1"
user = "MIGR8_TEST"
target_schema = "MIGR8_TEST"

[oracle]
ddl_lock_timeout_seconds = 30

[lock]
provider = "dbms_lock"
package = "SYS.DBMS_LOCK"
id = 4711
timeout_seconds = 60
```

Credentials come from `MIGR8_PASSWORD`, supported wallet/external authentication, or an explicitly supplied secret provider. Do not store passwords in the committed configuration or logs. An ephemeral local test fixture may use dedicated test-only secrets supplied through ignored environment files.

Resolve config/manifest paths once relative to explicit CLI paths or documented defaults. Validate identifier fields before constructing SQL. Use native bind parameters for data and proper adapter identifier quoting for identifiers; string interpolation of unchecked identifiers is not acceptable.

There is no user setting to weaken required commit durability. Optional driver features and platform support must be stated in the actual implementation's compatibility report.

### 11.5 Run diagnostics

Three facilities, none of which changes the protocol or the database layout:

- **A run correlation id.** Generated per invocation, printed on every failure, and
  included in `status` and `validate` output.
- **An optional append-only event log.** `--log-file PATH`, or `MIGR8_LOG_FILE`,
  appends one JSON object per line, flushed as written, so an interruption still
  leaves the log usable to the last event. Recorded phases are run start,
  preflight, connection, lock acquisition, the plan, per-migration start and
  completion with timings, each admission, and a terminal record carrying the
  outcome, exit code, failing identity and phase. `ctx.log()` writes into the same
  stream. Credentials, connection strings and bind values are excluded by field
  name.
- **A machine-readable outcome.** `--json` emits the run report, including the
  failing identity, the phase, the recovery command where one applies, and the
  duration.

Three constraints are part of the requirement. Nothing is written unless a log
file is configured, so default behaviour is unchanged. No database object is
added: an audit table would change the metadata layout and enlarge this
specification's scope. And the log is diagnostic only -- no correctness rule may
depend on it, exactly as Section 8.2 says of intermediate fingerprints.

## 12. Oracle adapter requirements

The MVP uses `python-oracledb`, initially Thin mode where compatible with the tested server and authentication configuration. Thick mode and additional authentication combinations are supported only after appropriate tests. Record actual Python, driver, database release/update, operating system, and architecture in test evidence.

Use one physical connection for the run with `autocommit = False`. Establish `COMMIT_WAIT = FORCE_WAIT` before acquiring the migration lock or writing metadata. If the setting fails, fail setup. Read it back when the required view privilege is available; lack of optional read-back must be reported and must not be described as a successful verification.

Oracle exposes no session-level read-back for `COMMIT_WAIT`. `V$PARAMETER` reports the instance value, which a session setting overrides. The adapter therefore reports the setting as NOT VERIFIED at session level in its capability notes, records the instance value when `V$PARAMETER` is readable, and says so when it is not. The `ALTER SESSION` itself is still required to succeed, and `COMMIT_LOGGING = IMMEDIATE` is set alongside it. PostgreSQL does support a real session read-back, and its adapter requires `SHOW synchronous_commit` to return `on` after setting it.

Prohibit migration changes to required session settings. Disable transparent reconnect and replay for this runner. Transaction Guard is compatible with Oracle's continuity features, but integrating either is outside this MVP's recovery path.

The initial runner need not impose a driver call timeout on migration statements. A deployment may choose database statement/resource policies; any resulting error must still follow the actual operation-outcome rules. Test harnesses must arrange bounded transport termination rather than relying on an infinite blackhole or a production timeout setting.

Set `DDL_LOCK_TIMEOUT` from configuration. Set `CURRENT_SCHEMA` if the target differs from the connect user. This does not grant privileges or change which owner `USER_*` views describe. Use exact-owner `ALL_*` probes and verify necessary access during setup.

The initial adapter may restrict target schemas and declared object names to unquoted uppercase identifiers to keep namespace resolution precise. State this restriction in errors and documentation. Metadata should be schema-qualified internally so application code cannot redirect engine writes by changing name resolution.

Oracle DDL is restartable, including single-statement DDL. A plain CREATE that errors when repeated is not automatically made restartable by its mode label. Authors must supply a convergent PL/SQL block or Python logic, or use a database operation whose repeated behavior satisfies the migration contract.

Bulk DML must default to raising on error rather than silently collecting partial row errors. Engine batch rollback and progress coupling must be verified against Oracle. Partial effects inside permitted restartable procedural calls remain author-owned recovery states.

One more Oracle detail shapes the adapter. Oracle assigns a local transaction id only once a write happens, so `DBMS_TRANSACTION.LOCAL_TRANSACTION_ID` cannot answer "am I inside an engine-opened batch": the first write in a batch is often the progress checkpoint itself. The adapter tracks batch state explicitly and keeps `LOCAL_TRANSACTION_ID` for what it does answer correctly, namely whether uncommitted work exists, which is what `ctx.ddl()`'s precondition and the post-return state check need.

## 13. Probe adapters and practical test databases

### 13.1 Oracle: primary acceptance target

Recommended local development path: run Oracle Database Free in a disposable container using the maintained `gvenzl/oracle-free` images. The project provides ARM64 images from the 23.5 generation onward, suitable for an Apple Silicon development host. Select a currently available compatible tag and pin its digest in the test infrastructure; record the actual server banner, since image tags and product names evolve.

Docker Desktop or another working local container runtime is sufficient if it supports the image. Check the daemon, architecture, disk space, and configured memory before downloading or starting the Oracle image. Bind published database ports to loopback. Use an isolated Compose project, dedicated test credentials, dedicated volumes or disposable storage, and a health check before running tests.

Create a disposable migration user/schema with the required metadata/migration privileges and a `DBMS_LOCK` grant or test wrapper. If exercising separate connect-user/target-schema behavior, use a second controlled fixture for those grants. Run setup as an administrator only against the explicitly identified disposable test instance. Do not reuse a production service or credentials as a fallback.

Oracle Free exercises the implementation on that tested release. It does not certify Oracle 19c. Before claiming 19c support, run the release-gate suite against a real available 19.x installation and record its exact update level. If that environment is unavailable, say NOT RUN and constrain the published support claim.

Source: [Oracle Free image project](https://github.com/gvenzl/oci-oracle-free).

### 13.2 PostgreSQL: second adapter and independent database behavior

Use the official PostgreSQL image in the same isolated Compose project, with its own credentials, port, health check, and storage. Pin the selected supported release and image digest. Choose the correct volume mount for that image version; do not assume its data-directory convention is identical across major releases.

Use `psycopg` and one physical session with a session-level advisory lock held across commits. Require synchronous local commit durability and explicit engine transactions. Session-level advisory locks, unlike transaction-level ones, survive transaction boundaries. Disable transparent reconnection. Use a dedicated schema/namespace and validate the lock binding.

Allow supported transactional DDL in atomic migrations. Operations that cannot execute within a transaction block, such as concurrent index creation, belong to restartable migrations with adapter-native checks. Do not silently change a declared mode, or emulate Oracle's implicit DDL commits on PostgreSQL.

An autocommit driver connection between explicit PostgreSQL transactions is an adapter choice, not a violation of Oracle's separate session requirements. The facade still controls which work may execute outside transaction contexts.

PostgreSQL tests validate the state machine and PostgreSQL behavior. They do not substitute for Oracle's DDL, PL/SQL, lock, or commit-outcome tests.

Sources: [Official PostgreSQL image](https://hub.docker.com/_/postgres), [PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS).

### 13.3 SQLite: explicitly limited local probe

Use Python's built-in `sqlite3` and a temporary file per test. This needs no database service and is useful for fingerprints, state validation, CLI behavior, atomic work/history coupling, restartable batching, and progress recovery.

The adapter name is `sqlite-probe`. It is test/development support, not an Oracle emulator or a production-compatibility claim. Use real SQLite transaction behavior. Do not invent implicit DDL commits to make Oracle tests pass.

To keep exclusion across batch commits, the local POSIX probe may use a process-held advisory file lock associated with the canonical database path. This is an explicit exception to the production adapters' database-backed lock requirement. Hold it for the entire run and never unlink/recreate a live lock file. Only cooperating local processes using the same canonical database file/path convention are supported; network filesystems, aliases/hard-link access, shared in-memory databases, and Windows locking are outside the first probe profile.

The SQLite connection and lock are owned by the same synchronous process; no background database execution may outlive it. `status` does not take the migration file lock. Use committed reads, a short busy timeout, and optionally WAL for read/write coexistence. WAL setup is a controlled probe-initialization operation, not a side effect of `status` or `validate`.

Do not use `sqlite3.executescript()` as a generic atomic SQL executor; use explicit transaction handling and one-statement execution with tested driver semantics. Record Python and SQLite versions because Python driver transaction defaults vary by configuration/version.

SQLite success is fast probe evidence. Live Oracle and PostgreSQL suites remain separately reported gates.

Source: [SQLite isolation documentation](https://www.sqlite.org/isolation.html).

## 14. Acceptance evidence

### 14.1 Evidence levels

Maintain a checked-in test matrix and an execution report with actual commands, fixture identifiers, versions/digests, outcomes, and skipped/unavailable gates. Do not convert NOT RUN into PASS, or use mocks to substantiate database transaction semantics.

| Level | Purpose | Required evidence |
|---|---|---|
| Pure/unit | Manifest validation, canonical fingerprints, paths, lexer, state validation | Deterministic fixtures, including malformed and colliding definitions. |
| SQLite probe | Fast real transactional and CLI feedback | Actual SQLite files and processes; identified probe limitations. |
| PostgreSQL integration | Session locking and PostgreSQL execution contracts | Real pinned server, driver, concurrent sessions and process failures. |
| Oracle integration | Primary execution and recovery contract | Real Oracle server, metadata/DDL/PLSQL behavior, lock and commit-failure evidence. |
| Oracle 19c release gate | Claimed 19c compatibility | Full applicable suite on recorded real 19.x update. |

### 14.2 Required scenario groups

1. **Manifest and fingerprint:** deterministic golden encoding; changed entry/mode/language/required set; identity comparison; location independence; duplicated ids/units/requirements; symlinks; invalid paths; bytecode; missing entry; required-set reordering; changed successful source; staged source unaffected by later working-tree edits; distinct Python ids `a-b` and `a_b` load distinct helpers.
2. **History validation:** valid empty/prefix/full states; gaps and wrong positions; more than one ACTIVE; ACTIVE not next; ACTIVE marked atomic; incorrect stored language/mode; orphaned progress; modified SUCCESS; unsupported fingerprint/layout formats; required-field corruption.
3. **Initialization:** interruption after each independently durable object creation and before/after the final marker; compatible completion; missing history/progress/index after completed initialization; populated history without marker; incompatible or disabled constraints; lack of inspection privileges; lock/config binding mismatch.
4. **Atomic:** normal DML; query-only success; statement error rollback; error inserting successful history; forbidden DDL; implicit/explicit boundary violation tripwire; no further work after a detected violation; success work/history commit together; lost completion acknowledgement in both proven branches.
5. **Restartable:** failure after each DDL/batch boundary; completed work before SUCCESS; no-op rerun of completed work; open transaction on return; progress/data commit together; checkpoint derived from processed keys; rejection of progress writes outside batches; no continuation after a caught unknown-outcome exception.
6. **Required objects:** missing, wrong-type, ambiguous, invalid, or inaccessible declarations fail; warning-only valid objects pass; failed compilation has the same verdict on retries including skipped recreation; adding/removing a requirement changes the fingerprint; unrelated invalid objects and compilation settings remain untouched by the engine.
7. **Recovery admission:** unchanged retry; changed ACTIVE without flag; wrong/no active id; changed mode rejected even with flag; SQL-to-Python recovery records consistent metadata; original position/first fingerprint preserved; recovery handles states/checkpoints from more than one earlier source version; SUCCESS edits never admitted.
8. **Concurrency:** deterministic lock contention with zero timeout; a waiter that subsequently succeeds; lock held across DDL and batch commits; dead client with live server session; lock released only after the old session ends; read-only status during a long migration.
9. **Oracle and SQL details:** correct PL/SQL transaction-id invocation; multiline UPDATE/SET and EXIT WHEN; literals/comments containing slashes and semicolons; correct stored-PLSQL terminators; SQL*Plus unsupported without rejecting valid internal tokens; direct DDL restrictions; synchronous commit session setup; native driver parameter handling; realistic bounded backfill example.
10. **Inspection:** `validate` and `status` perform no initialization, code import, migration execution, compilation, or SQLite WAL changes; consistent history snapshots; precise uninitialized/damaged distinction; honest optional diagnostics.

### 14.3 Deterministic commit-acknowledgement failures

A proxy with independently controlled request and response directions is useful, but blackholing traffic alone does not establish a branch or make the runner exit. Synchronize at the engine-owned operation boundary with a test hook. A signal emitted earlier by migration code is not sufficient if further driver calls precede COMMIT.

For **request not delivered**, hold the runner immediately before commit, arrange for the commit request not to reach the server, then terminate the transport. After the original server session has ended and exclusive access is obtainable, assert from an independent session that the transaction's work/history is absent.

For **commit durable, response not received**, allow the commit request through and withhold its response. Use an observer connection to confirm the expected durable state. Then terminate the transport so the waiting runner receives an error. Assert exit 4, no in-run continuation, and correct fresh-run reconciliation. Observers are test infrastructure, not extra migration execution connections.

Apply the two branches separately to initialization completion, ACTIVE admission, atomic completion, a restartable batch, and restartable completion. A test accepting either durable outcome without establishing which branch it induced is insufficient.

Use hooks or a test wrapper that actually obtains a successful server commit and then suppresses acknowledgement only where its evidentiary scope is stated. Keep transport-level failures separate from wrapper simulations. Test hooks must not be accidentally activatable by normal deployment configuration.

## 15. Implementation boundaries

Keep a small separation between manifest/fingerprint/staging, pure state validation, execution orchestration, database adapters, and CLI/reporting. No plugin discovery framework, ORM, distributed job system, or universal SQL AST is required.

The separation runs *through* the adapter layer as well, and the line is between rule and dialect. Every rule this specification states belongs in the shared adapter base and must exist exactly once:

- which columns an attempt or completion update may touch, and which are permanent;
- that an affected-row count other than one is metadata damage;
- the progress store's shape and its batch-transaction precondition;
- which statement contexts exist, and that a context admits a declared token set.

An adapter supplies only what differs: placeholder style, the engine's timestamp
expression, identifier folding and quoting, the upsert form, the token sets
themselves, and a path for engine-owned SQL separate from the migration facade.
Metadata *inspection* stays per-adapter, because dictionary views differ in
substance rather than in spelling, and forcing them together would obscure all of
them.

Two rules follow from this. Engine transaction state is tracked by the adapter
base rather than inferred from the database, because Oracle assigns a local
transaction id only on first write, so "is there uncommitted work" and "am I
inside an engine-opened transaction" are different questions with different
answers. And one conformance suite runs against every adapter: without it, three
implementations of one rule drift silently, and adding a fourth adapter means
writing a fourth set of tests that may not cover the same ground.

For each adapter, implemented capabilities and tested capabilities are reported
separately. An unavailable database environment is a blocked acceptance gate, not
a reason to certify the engine from the SQLite probe.

## 16. Primary technical references

These references support database facts. The protocol and scope choices above are design decisions and still require implementation evidence.

- Oracle implicit and explicit commits: [COMMIT](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/COMMIT.html).
- Synchronous commit policy: [COMMIT_WAIT](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/COMMIT_WAIT.html).
- Session lock semantics: [DBMS_LOCK](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/DBMS_LOCK.html).
- Transaction identifier creation: [DBMS_TRANSACTION](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/DBMS_TRANSACTION.html).
- Oracle 19c Boolean restrictions: [BOOLEAN data type](https://docs.oracle.com/en/database/oracle/oracle-database/19/lnpls/boolean-data-type.html).
- DDL execution during parse: [DBMS_SQL](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/DBMS_SQL.html).
- Compiler errors and warnings: [ALL_ERRORS](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/ALL_ERRORS.html).
- Driver DML output binding: [python-oracledb binds](https://python-oracledb.readthedocs.io/en/stable/user_guide/bind.html#dml-returning-bind-variables).
- Explicit PostgreSQL driver transactions: [Psycopg transaction management](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).

## 17. Deliberate omissions

Each of these would weaken something the specification protects, and none of
them is in the tool:

- **Relaxing preflight to the pending suffix only.** It would hide a tool
  regression affecting the part of the manifest that defines the successful
  prefix.
- **Degrading Oracle's consistent read to three unsynchronised queries when
  ORA-01466 appears.** Silently reducing consistency to avoid an error is worse
  than the error; the single-statement read removes the problem instead.
- **Classifying every `DPY-` driver code as definite.** Some of them are genuine
  connection losses. The list is enumerated and anything unlisted stays unknown.
- **Making `UnknownOutcomeError` a `BaseException` so author code cannot catch
  it.** The specification's mechanism is the run latch, and the latch is what is
  tested. Catching the exception must not let the run continue, and it does not.
- **Adding an undo, repair, abandonment, or forced-unlock path** for any of the
  failure states the tests produce. Every one of them is resolved by roll-forward
  or by controlled operator action outside this protocol.
- **A client-side statement or call timeout.** Section 12 already declines one for
  the initial runner, and adding it would create a new unknown-outcome surface: a
  timeout firing during a commit is indistinguishable from a lost
  acknowledgement. Bounding long statements stays a database-side policy.
- **An audit table recording every attempt.** It would answer useful questions,
  and it would also change the metadata layout, add a durable object to recover,
  and invite correctness rules to depend on it. The event log answers the same
  questions from outside the database.
- **A fourth command for diagnosis.** The command surface is three commands by
  specification. `status --json` plus the event log covers it without widening the
  protocol.
