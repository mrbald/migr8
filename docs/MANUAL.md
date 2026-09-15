# User manual

`migr8` applies one ordered sequence of migrations to one database namespace.
It has three commands and no undo.

[`ACCEPTANCE.md`](ACCEPTANCE.md) records what has been tested and against which
versions. The specification is [`SPEC.md`](SPEC.md); this manual is how to use
the thing. [`ORACLE-CONNECTIONS.md`](ORACLE-CONNECTIONS.md) is the background for
the Oracle connection settings: driver modes, TNS aliases, proxy authentication
and wallets.

**Contents** — [Install](#install) · [Configure](#configure) ·
[Write a migration](#write-a-migration) · [Run it](#run-it) ·
[Read the status](#read-the-status) · [When something fails](#when-something-fails) ·
[Recovery](#recovery) · [Diagnose](#diagnose-a-failure) ·
[Teams](#working-in-a-team) · [Deployment](#deployment) · [Runbooks](#runbooks) ·
[Reference](#reference)

---

## Install

Requires Python 3.12 or later on a POSIX host. One wheel carries every adapter;
the extras decide which driver is installed with it.

**On a server.** Install into a virtual environment of its own, from your index
or proxy, and pin the version:

```bash
export PIP_INDEX_URL=https://proxy.internal/simple     # your index or proxy
python3 -m venv /opt/migr8            # 3.12 or later
/opt/migr8/bin/pip install 'migr8[oracle]==<approved-version>'   # Oracle
/opt/migr8/bin/pip install 'migr8==<approved-version>'           # SQLite only
/opt/migr8/bin/migr8 --version                                   # self-check
/opt/migr8/bin/migr8 validate --offline                          # self-check, with a plan
```

Give the environment to an account that owns it and let the account that runs
migrations read and execute it. The runner writes nothing there.

`postgres` is the third extra and pulls `psycopg`. The SQLite adapter needs no
driver: it uses the standard library. Nothing else is required at run time — no
checkout, no `uv`, no compiler, no test framework, no linter, and no driver for
a database you do not use.

Pinning `migr8` alone does not pin what the driver resolves to, so install from
a dependency set with versions and hashes. Each release publishes one per
profile, `runtime-base.txt` and `runtime-oracle.txt`, built from the same
`pyproject.toml` the wheel was:

```bash
/opt/migr8/bin/pip install --require-hashes -r runtime-oracle.txt
/opt/migr8/bin/pip install --no-deps 'migr8[oracle]==<approved-version>'
```

Prefer a wheel. If the index has no wheel for the host architecture, `pip` will
try to build the driver from source and need a toolchain; treat that as a
failure of the index rather than a reason to install build tools on the server.

What the runner needs beyond the package: a writable temporary directory (it
stages every migration there before executing anything), a directory for the
event log if you enable one, the configuration and manifest, and the database
password in `MIGR8_PASSWORD`. It never writes inside its own installation.

What the DBA provides, separately and before the first run: the database itself,
the migration identity, the target schema, its quota, the privileges to create
the metadata and migration objects, and — on Oracle — `EXECUTE` on the lock
package plus the agreed lock id. `testenv/` provisions disposable *test*
databases only; its README excludes shared and production targets, and nothing
in it belongs in a production installer.

**In a checkout**, for development:

```bash
uv sync --all-extras
uv run migr8 --version
```

## Configure

Two files. A **configuration** says which database; a **manifest** says which
migrations, in what order. Neither ever contains a password.

```toml
# migr8.toml
[database]
adapter = "oracle"                     # oracle | postgres | sqlite
dsn = "db.internal:1521/ORDERS"
user = "ORDERS_MIGRATOR"
target_schema = "ORDERS"               # defaults to the connect user

[oracle]
ddl_lock_timeout_seconds = 30

[lock]
provider = "dbms_lock"                 # advisory on PostgreSQL, file on SQLite
package = "SYS.DBMS_LOCK"
id = 4711                              # every cooperating runner must use this id
timeout_seconds = 60
```

The password comes from `MIGR8_PASSWORD`, nowhere else:

```bash
export MIGR8_PASSWORD="$(read-from-your-secret-store)"
```

On Oracle the `[oracle]` table also selects the driver mode and where the driver
reads its own configuration:

```toml
[oracle]
ddl_lock_timeout_seconds = 30
allow_thick_mode = false               # Thin is the default
client_lib_dir = "/opt/oracle/instantclient_23_9"   # Thick only
config_dir = "/etc/oracle"             # tnsnames.ora, sqlnet.ora, wallet
```

**Thin or thick.** Thin needs no Oracle client at all. Thick loads the Oracle
Client libraries, and the runner loads them itself when you ask for it, before it
connects; the mode is then read back from the connection, so a run configured for
one and given the other fails instead of quietly using the wrong stack. The
client resolves its own libraries through the dynamic loader, so the directory in
`client_lib_dir` must also be on the loader's path — `ldconfig`, or
`LD_LIBRARY_PATH` in whatever starts the runner. Loading is per process and
cannot be undone.

**`config_dir`** is `TNS_ADMIN` by another name, and it works in both modes. With
it, `database.dsn` may be a TNS alias from `tnsnames.ora` rather than
`host:port/service`. The namespace binding records the schema and the lock id,
not how you spelled the address, so the same namespace can be reached through an
alias in one environment and a host and port in another.

Three settings deserve thought:

**`lock.id`** is the namespace lock. Every runner that might touch this namespace
must use the same id, or they will not exclude each other. It is recorded on first
run and verified on every later run; changing it is a configuration error, not a
migration.

**`target_schema`** is where metadata and migration objects live. On Oracle it may
differ from the connect user; the engine sets `CURRENT_SCHEMA` and still qualifies
its own writes, so migration code cannot redirect them.

**`user`** on Oracle may also be a proxy connect string, which is how a deployment
separates the identity that proves who is running from the schema that owns the
objects:

| `database.user` | Authenticates as | Runs as | Password |
|---|---|---|---|
| `APP_DBA` | `APP_DBA` | `APP_DBA` | `MIGR8_PASSWORD` |
| `RUNNER[APP_DBA]` | `RUNNER` | `APP_DBA` | `MIGR8_PASSWORD`, the runner's |
| `[APP_DBA]` | a wallet | `APP_DBA` | none; needs `allow_thick_mode` |

The target in brackets becomes the session user, so `target_schema` defaults to it
and nothing sets `CURRENT_SCHEMA`. The database has to permit it:
`ALTER USER APP_DBA GRANT CONNECT THROUGH RUNNER`. Without a password the run
authenticates externally through a wallet, which the thin driver cannot do.

**`lock.timeout_seconds`** is how long to wait for another runner to finish. It is
an operator policy, unrelated to how long a statement may take.

On SQLite the configuration names the file and the durability it requires:

```toml
[database]
adapter = "sqlite"
path = "/srv/orders/orders.db"         # the canonical path every runner opens

[sqlite]
journal_mode = "wal"                   # or "delete"
synchronous = "full"                   # or "extra"
busy_timeout_ms = 5000

[lock]
provider = "file"                      # <database>.m8lock, held for the whole run
timeout_seconds = 60
```

The adapter sets these on the database and reads them back, and refuses to run if
one does not take effect. `synchronous` has no weaker setting: `off` and `normal`
do not survive the power loss the durable commits are defined against.

Full examples per engine: [`../examples/`](../examples/).

## Write a migration

A migration is a **directory**. Everything it executes or reads lives inside,
because the directory's bytes are what gets fingerprinted.

```text
migrations/
  010-create-orders/
    up.sql
  020-backfill-region/
    migration.py
    assumptions.py
```

Declare it in the manifest. Array order is execution order; the leading numbers
are for humans.

```toml
manifest_version = 1

[[migration]]
id = "create-orders"                   # permanent, never reused, never renamed
path = "migrations/010-create-orders"
language = "sql"                       # sql | python
mode = "restartable"                   # atomic | restartable
entry = "up.sql"

[[migration]]
id = "backfill-region"
path = "migrations/020-backfill-region"
language = "python"
mode = "restartable"
entry = "migration.py"
require_valid = [                      # Oracle only; checked read-only at the end
  { name = "PKG_ORDERS", type = "PACKAGE BODY" },
]
```

### Choosing the mode

This is the only real decision, and it is not reversible after the migration
succeeds anywhere.

| | **atomic** | **restartable** |
|---|---|---|
| Transaction | one; work and history commit together | many; you own the batching |
| On failure | nothing happened | partial work stays, migration stays ACTIVE |
| Oracle DDL | **not allowed** (Oracle DDL commits on its own) | allowed |
| PostgreSQL DDL | allowed (transactional) | allowed |
| Your obligation | do not commit, roll back, or change session state | be convergent from every state you can leave behind |

**Use atomic** when the whole change fits in one transaction: DML, backfills small
enough to do at once, PostgreSQL DDL.

**Use restartable** for anything else, and especially for all Oracle DDL. The
label does not make your code convergent — that is on you:

```sql
-- A plain CREATE TABLE fails on the second attempt. Make repetition safe:
DECLARE
  already_exists EXCEPTION;
  PRAGMA EXCEPTION_INIT(already_exists, -955);
BEGIN
  EXECUTE IMMEDIATE 'CREATE TABLE orders (id NUMBER(10) PRIMARY KEY)';
EXCEPTION
  WHEN already_exists THEN NULL;
END;
/
```

### SQL migrations

One statement or one procedural block per file. There is no statement splitter and
no SQL\*Plus interpreter, deliberately: splitting SQL correctly requires parsing
it, and a tool that guesses wrong executes half a migration.

Semicolons and slashes inside literals and comments are safe. A trailing `;` on a
plain statement is removed; a PL/SQL block keeps its final `;`; one trailing `/`
on its own line is accepted and discarded. A second bare `;` is an error.

### Python migrations

The entry module defines `migrate(ctx)`. Siblings are imported **relatively** —
the unit is its own private package and is never added to `sys.path`:

```python
from .assumptions import BATCH_SIZE


def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                """SELECT id FROM orders
                    WHERE id > :after AND region IS NULL
                    ORDER BY id FETCH FIRST :batch_size ROWS ONLY""",
                {"after": last, "batch_size": BATCH_SIZE},
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            changed = tx.executemany(
                "UPDATE orders SET region = :region WHERE id = :id AND region IS NULL",
                [{"id": key, "region": "EU"} for key in ids],
            )
            if changed != len(ids):
                raise RuntimeError("batch membership changed under the migration")
            last = max(ids)
            ctx.progress.set("last_id", str(last))  # commits with the batch
        ctx.log("batch committed", last_id=last, rows=len(ids))
```

The shape matters: select a bounded set of keys, update **those exact keys**, then
checkpoint them — all in one transaction. The checkpoint is then always true about
work that is durable. Note what this does *not* claim: a row inserted below the
checkpoint afterwards will not be revisited. If that can happen, use an idempotent
predicate or decide explicitly how application writes are handled.

### The context API

| Member | Available | Notes |
|---|---|---|
| `ctx.migration_id`, `ctx.attempt` | always | `attempt` is `None` for atomic |
| `ctx.execute(sql, params=None)` | always | affected-row count |
| `ctx.executemany(sql, sets)` | always | raises on the first error |
| `ctx.query(sql, params=None)` | always | list of tuples |
| `ctx.sql(relative_path)` | always | reads a fingerprinted file from the unit |
| `ctx.log(message, **fields)` | always | goes to the event log |
| `ctx.transaction()` | restartable | one batch; commits on clean exit |
| `ctx.ddl(sql)` | restartable | one DDL statement, outside any batch |
| `ctx.progress.get/set` | restartable | `set` only inside a batch |

Parameters use the driver's native style: `:name` on Oracle and SQLite, `%s` or
`%(name)s` on PostgreSQL.

What you cannot do, and why the tool stops you: commit or roll back yourself,
change session settings, open nested transactions, run DDL inside a batch, write a
checkpoint outside a batch, or touch `m8_history`, `m8_progress` or `m8_meta`. The
driver connection is not exposed. These are guards against honest mistakes, not a
sandbox — your code is trusted, and a routine with autonomous transactions can
still defeat them.

## Run it

```bash
migr8 migrate                      # apply everything pending
migr8 status                       # what is applied, what is not
migr8 validate                     # are source and history still consistent
migr8 validate --offline           # is the plan itself well formed, no database
```

Each takes `--config`, `--manifest`, `--log-file` and `--json`; `migrate` also
takes `--recover ID`, and `validate` takes `--offline` and `--baseline PATH`.

`migrate` does this, in order: load and validate both files; stage every unit into
a private directory and fingerprint the copies; connect, set up the session and
acquire the namespace lock; initialize or verify metadata; read history and
validate it; execute the pending suffix in order.

Staging means an edit to your working tree after `migrate` starts cannot change
what runs. Exactly one runner executes at a time, enforced by the database lock —
a second runner waits, and if it finds nothing left to do it exits 0, which is
correct and not an error.

### Checking a plan in CI, before any database

`migr8 validate --offline` connects to nothing, reads no password and touches no
namespace. It checks what the plan can be held to on its own: manifest order,
identities, paths and fingerprints; the language and mode combinations this
backend supports; required-object declarations; the statement rules for every SQL
unit; and that every Python source in every Python unit compiles. It imports
nothing, executes nothing and writes no bytecode. The report lists what it
checked, because exit 0 from it is a statement about the plan, not about your
database.

```bash
migr8 validate --offline --json > plan.json
```

Keep the approved `plan.json` as the published plan, and check the next one
against it:

```bash
migr8 validate --offline --baseline plan.json
```

Every entry the approved artifact records must still be at the same position with
the same id and fingerprint; anything after them is new work. A manifest on its
own cannot show that a published migration was edited — the unit and its
fingerprint change together — so this comparison is what holds published identity
and order in a pipeline. `--baseline` works with plain `validate` too, and is
checked before it connects.

Three gates, in order, and none replaces another:

| Gate | Command | Answers |
|---|---|---|
| Plan lint | `migr8 validate --offline --baseline plan.json` | Is the plan well formed, admissible, and unchanged where it was published? |
| Rehearsal | `migr8 migrate` against a disposable database of the same engine, from the starting state you expect | Does it actually run? |
| Target check | `migr8 validate` against the real namespace | Does this database agree with this plan? |

The lint cannot tell you that the SQL is valid on the server, that the privileges
are there, that the data is what the migration assumes, or that a restartable
migration converges. An uninitialized target returns exit 6 from `validate`,
which is the expected answer before the first install, not a failure to suppress.
Never blanket-ignore a validation failure: the engine's own check, under the
namespace lock, is the one that decides whether a migration runs.

## Read the status

```text
adapter:   oracle
server:    Oracle Database 23ai Free Release 23.0.0.0.0 ... Version 23.9.0.25.07
namespace: ORDERS
metadata:  complete
history:   4 successful, 1 pending, ACTIVE backfill-region

 pos  state    mode         lang    fp    id
   1  SUCCESS  restartable  sql     ok    create-orders
   2  SUCCESS  atomic       sql     ok    seed-orders
   3  SUCCESS  restartable  sql     ok    pkg-orders-body
   4  ACTIVE   restartable  python  DIFF  backfill-region
        attempt=2 started=... last_attempt=...
        recorded=fp1:9c2... current =fp1:4ab...

run:  6ab19a585c51442d86133df2b05f5435
exit: 2
```

The `fp` column is the one to read: `ok` means the source still matches what was
recorded, `DIFF` means it does not, `-` means nothing is recorded yet. A `DIFF` on
an ACTIVE row needs recovery. A `DIFF` on a SUCCESS row means published source
changed, which is a problem to fix in source control, not in the database.

`status` is read-only and takes no lock, so it is safe and useful while a long
migration is running. Add `--json` for scripts.

## When something fails

The exit code is the interface. Each one means a different action.

| Exit | Meaning | What to do |
|---|---|---|
| **0** | Done, validation passed | nothing |
| **1** | Usage, configuration, connection or lock-binding error | fix the config or the environment; nothing was attempted |
| **2** | Validation failed | read `status`. An ACTIVE `DIFF` needs `--recover`; a SUCCESS `DIFF` means restore the published source |
| **3** | Ordinary migration failure | atomic work rolled back, or restartable is still ACTIVE. Fix the cause and rerun |
| **4** | **Outcome unknown** | do not retry the statement. Just rerun `migrate`: it reacquires the lock and reconciles |
| **5** | Lock not acquired | another runner holds it, or a previous server session has not ended. Check `status`; wait or investigate |
| **6** | Not initialized | read-only commands only. `migrate` creates the metadata |
| **7** | Metadata damaged | **stop.** Nothing is recreated automatically. See below |
| **8** | Transaction contract violated | **stop.** Durable effects may need manual remediation |

### Exit 4 is normal, not alarming

It means a commit-capable call lost its answer: the network dropped, the session
was killed, or the process was interrupted mid-commit. The work may or may not be
durable, and the tool refuses to guess. It issues no cleanup SQL, discards the
connection, and stops.

The fix is always the same: **run `migrate` again.** It takes the lock, reads what
is actually durable, and continues from there. Do not re-run the individual
statement by hand.

### Exit 7 and 8 need a human

Exit 7 means the metadata is internally inconsistent or does not match the
supported layout — a position gap, two ACTIVE rows, an unknown fingerprint
format, orphaned progress. The tool will not repair it, because a repair that
guessed wrong would corrupt the record of what ran. Read the message, inspect
`m8_history`, and restore from backup if the metadata was lost.

Exit 8 means a migration broke its own atomic transaction, usually by committing
inside a PL/SQL block. No success row was written, but earlier durable effects may
exist. Inspect what the migration did, remediate, then fix the migration.

## Recovery

A restartable migration that fails stays ACTIVE with its checkpoint intact.

**If you change nothing**, rerun `migrate`. It admits a new attempt and re-enters
your code from the top; your checkpoint tells you where to resume.

**If you must amend the migration**, plain `migrate` refuses with exit 2 and prints
the exact command:

```bash
migr8 migrate --recover backfill-region
```

`--recover` admits amended source for the one ACTIVE migration. It preserves the
identity, position, mode, original start time and first fingerprint; it updates
the current fingerprint, the language and the attempt count. It will not change
the mode, will not touch a successful migration, and will not accept a different
id.

There has to be something to recover. Against a namespace with no completed
metadata the flag is refused with exit 2 before anything is created, so a
mistyped `--recover` on a fresh database leaves it exactly as it was. Run
`migrate` without the flag to initialize.

Your amended code must converge from **every** state any earlier admitted version
could have left, including its checkpoint format. Existing progress rows are still
there; reading the old key and writing a new one is part of recovery logic:

```python
def migrate(ctx):
    cursor = ctx.progress.get("cursor")
    legacy = ctx.progress.get("last_id")  # written by the previous version
    start = int(cursor.split(":", 1)[1]) if cursor else (int(legacy) if legacy else 0)
    ...
```

There is no undo, no repair, no abandonment and no forced unlock. Roll forward, or
restore from a backup.

## Diagnose a failure

Turn on the event log. One JSON object per line, flushed as it goes, so a kill
still leaves it usable:

```bash
migr8 migrate --log-file /var/log/migr8/run.jsonl
# or: export MIGR8_LOG_FILE=/var/log/migr8/run.jsonl
```

```json
{"ts":"2026-09-12T10:14:02.118+00:00","run":"6ab1...","event":"run_start","elapsed":0.0,"command":"migrate","adapter":"oracle","units":5}
{"ts":"...","run":"6ab1...","event":"lock_acquired","elapsed":0.41,"binding":"dbms_lock:4711"}
{"ts":"...","run":"6ab1...","event":"migration_start","elapsed":0.52,"migration":"backfill-region","position":5,"mode":"restartable"}
{"ts":"...","run":"6ab1...","event":"migration_log","elapsed":1.88,"migration":"backfill-region","message":"batch committed","last_id":1000}
{"ts":"...","run":"6ab1...","event":"run_end","elapsed":4.02,"outcome":"migration_failed","migration":"backfill-region","phase":"final_validity","detail":"..."}
```

Every line carries the same `run` id, which is printed on every failure. The
terminal `run_end` line gives you the outcome, the failing migration and the
phase.

`detail` names the failure by exception type and engine error code, such as
`oracledb.DatabaseError [ORA-00001]`. The driver's own message is not written to
the log, to `--json` or to stderr: it quotes the statement and the values that
produced the error. To see the server's text, reproduce the statement against the
database yourself. Event fields named `password`, `secret`, `dsn`, `credential`
or `params` are dropped whatever the caller passes; fields you pass to
`ctx.log()` are your responsibility.

For scripting, `--json` gives the outcome as one record:

```bash
migr8 migrate --json | jq '{outcome, failed_migration, phase, recovery_command}'
```

A practical order for any failure:

1. `migr8 status` — what does the database actually say?
2. The `run_end` line for that run id — which phase, which migration?
3. `migration_log` lines before it — how far did the author's code get?
4. Decide from the exit code table above.

## Working in a team

**Identities are permanent.** Once a migration has succeeded anywhere, its `id`
and its source bytes are fixed. Renaming the id, reordering the manifest, or
editing the file makes every other environment fail validation.

**Choose a publication point** — merging to the deployment branch, or publishing a
release artifact. Before that point a migration is still editable. After it, treat
it as immutable. The tool enforces this against the database in front of it; it has
no idea what other environments exist.

**If a published migration fails elsewhere**, do not amend it. Fix the external
condition, or remediate the application schema under the same migration lock so
the original source can converge.

**Line endings matter**, because bytes are fingerprinted. Commit a `.gitattributes`
that pins them:

```gitattributes
* text eol=lf
```

## Deployment

Run migrations as a one-shot job with the tool and the migrations in the **same
immutable artifact**. If the migration tree can change after the artifact is
built, the fingerprint is not protecting anything.

A worked example, using Apple's `container` runtime on macOS, is in
[`../deploy/apple-container/`](../deploy/apple-container/): the migrations are baked
into the image, only the generated config is mounted, the password arrives through
the environment, and the job runs as an unprivileged user.

Operational notes:

* Set `MIGR8_LOG_FILE` to a path you collect, and mount it.
* `SIGTERM` is handled: a supervised stop is classified against the durable state
  rather than abandoning the connection. Give the job time to exit.
* Alert on exit 4, 7 and 8. Exit 5 means contention, which may be normal.
* There is no client-side statement timeout. Bound long statements with database
  policy: `DDL_LOCK_TIMEOUT` and resource manager on Oracle, `statement_timeout`
  on PostgreSQL.
* Back up the migration metadata with the application data, and restore them
  together: they are one consistent pair, and history restored without the
  objects it describes is worse than neither. On PostgreSQL, `pg_dump -n <schema>`
  covers both; a namespace restored that way validates and takes the next
  migration. Total metadata loss is outside automatic recovery, by design.

### Running on SQLite

The supported profile is one database file on a local filesystem, opened by
cooperating processes under the same canonical path, on a POSIX host. Outside it:
network filesystems, a symlink or hard link that reaches the same database under
a different path, shared in-memory databases, and Windows.

* Every runner and every application process must name the **same canonical
  path**. The namespace lock is a POSIX advisory lock on `<database>.m8lock`
  beside it, and two paths to one file are two namespaces to this lock.
* The lock is held by the process for the whole run and released by the kernel
  when it ends, including when it is killed. A stale lock file is not a held
  lock, and it is never unlinked while a runner may hold it.
* `journal_mode` is `wal` or `delete`, and only `migrate` sets it; `status` and
  `validate` never change the file's mode. Switching mode needs exclusive access,
  so a run that cannot get it fails rather than proceeding in the other mode.
* The application can read during a migration in both modes, though a
  DELETE-mode commit blocks readers for the moment it takes. It cannot write
  while a batch is open: its writes wait on the migration's lock and fail when
  its own busy timeout expires. The reverse holds too — an application
  transaction holding the database fails the migration once `busy_timeout_ms`
  expires, writing no history, and the next run applies the same migration.
* **Back up with the database quiesced or through SQLite's backup API**, not by
  copying the file under a running writer. In WAL mode the `-wal` and `-shm`
  files are part of the database: a copy of the main file alone can be an old
  state. Restore the application data and `m8_*` metadata together — they are one
  consistent pair — and restore to the same canonical path the configuration
  names.

```bash
# a consistent copy, taken through SQLite itself
sqlite3 /srv/orders/orders.db ".backup '/backup/orders-$(date +%F).db'"
```

## Runbooks

Three procedures, each with the commands, the exits to expect, and what is true
when it is over. Rehearse them against a disposable database of the same engine
before using them on anything you care about.

### Initial schema creation

The database and its migration identity already exist; this creates the
application objects in an empty target namespace. **Owner: the operator running
the release, with the DBA available.**

1. Confirm the starting state: the target namespace has no application objects
   this plan creates, and `migr8 status` reports exit 6, "not initialized".
   Absence of metadata is not proof that nothing was ever migrated — on an
   existing schema, see below.
2. Confirm the prerequisites the DBA provides: identity, target schema, quota,
   privileges, and the lock package and id (Oracle).
3. Install the approved version into its own environment and record what was
   installed: `migr8 --version`, and the dependency set you installed from.
4. Review the configuration: adapter, DSN, target schema, `lock.id`, log path.
   `lock.id` is recorded on this first run and every later runner must match it.
5. Lint the plan: `migr8 validate --offline --baseline plan.json` — expect exit 0.
6. Apply: `MIGR8_PASSWORD=... migr8 migrate --log-file /var/log/migr8/run.jsonl`
   — expect exit 0 and one `applied <id>` line per migration.
7. Confirm: `migr8 status` reports every migration SUCCESS, no ACTIVE row, and
   `migr8 validate` exits 0. Run the application's own checks.

Afterwards: the namespace holds `m8_history`, `m8_progress` and `m8_meta`; the
binding of adapter, namespace and lock id is recorded and will be enforced; the
report, the exit code and the event log are retained with the release record.

**Adopting an existing schema** needs a decision before step 1, taken with the
DBA. There is no baseline and no history repair, so either the first migration is
written to converge on what is already there, or the schema is rebuilt from the
plan.

### Routine migration

New migrations, appended after a published prefix that is already applied.
**Owner: the operator running the release.**

1. The published prefix is unchanged: `migr8 validate --offline --baseline plan.json`
   passes in CI, on the same artifact you are about to deploy.
2. The artifact is the reviewed one: the tool and the migrations travel together
   and cannot change after the build.
3. Check the target: `migr8 validate` — exit 0. Exit 2 means source and history
   disagree; exit 6 means this namespace was never initialized and you are on the
   wrong target or on an initial install; exit 7 means damaged metadata, and
   nothing should run.
4. Decide the window: state whether the application may run during this release.
   The engine does not stop it, and a long backfill contends with it.
5. Apply, as exactly one supervised job: `migr8 migrate --log-file ...`. A second
   runner waits for the lock, and exits 0 having found nothing to do; that is
   correct, not a failure.
6. Retain the report, the exit code, the run id and the event log.
7. Confirm: `migr8 status` shows the new migrations SUCCESS and no ACTIVE row.

Afterwards: history is the old prefix plus the new migrations, in order, with no
ACTIVE row; the approved plan artifact is updated to this plan and re-approved.

### Failure or interruption

**Owner: the operator, escalating to the engineer who wrote the migration and to
the DBA for anything under exit 7 or 8.**

1. Read the exit code first; [When something fails](#when-something-fails) says
   what each one means. Collect the report, the run id and the event log lines
   for that run before anything else touches the database.
2. **Exit 3**, ordinary failure: an atomic migration rolled back and left no row;
   a restartable one stayed ACTIVE and resumes from its entry point. Fix the
   external cause and rerun the same artifact. No flag is needed for an unchanged
   rerun.
3. **Exit 4**, unknown outcome: do not rerun blindly and do not retry in the same
   run — the engine already refused to. Run `migr8 status` and read the durable
   state; the next ordinary run reconciles from it.
4. **Exit 5**, lock not acquired: another runner holds the namespace. Contention
   is not evidence that it has stopped. Find it before doing anything else.
5. **Exit 7 or 8**, damaged metadata or a contract violation: stop. Neither has an
   automatic repair, and durable effects may need manual remediation. Escalate.
6. **A killed or crashed runner** leaves either an ACTIVE restartable migration
   with its checkpoint, or nothing. On Oracle and PostgreSQL, `status` also
   reports whether the database session that row records is still present: while
   it is, that session still holds the namespace lock and the next run waits for
   it. Rerunning the same artifact is the recovery path; the checkpoint decides
   where it resumes, and the attempt count on the row increments.
7. **An amended ACTIVE migration** needs `migr8 migrate --recover ID`, which is a
   deliberate operator action on an unpublished migration. [Recovery](#recovery)
   has the rules.

Rehearse one interrupted restartable migration before you need this: apply a
batched migration against a disposable database, stop the runner between batches,
and rerun it. Restoring a backup restores the application data *and* the metadata,
which is the only consistent pair; installing an older version of the tool undoes
nothing in the database.

## Reference

### Commands

```text
migr8 migrate  [--config PATH] [--manifest PATH] [--recover ID] [--log-file PATH] [--json]
migr8 validate [--config PATH] [--manifest PATH] [--log-file PATH] [--json]
               [--offline] [--baseline PATH]
migr8 status   [--config PATH] [--manifest PATH] [--log-file PATH] [--json]
```

Defaults are `./migr8.toml` and `./manifest.toml`. Add `-v` before the command
for progress on stderr.

### Environment

| Variable | Purpose |
|---|---|
| `MIGR8_PASSWORD` | the database password; the only supported channel |
| `MIGR8_LOG_FILE` | default event-log path |

### What the engine stores

Three objects in the target namespace, reserved and not to be touched by
migrations:

| Object | Contents |
|---|---|
| `m8_history` | one row per migration: position, id, both fingerprints, language, mode, state, attempt count, timestamps, last-attempt diagnostics |
| `m8_progress` | checkpoints for the current ACTIVE migration, deleted on completion |
| `m8_meta` | one row: layout version, adapter, lock binding, namespace, initialization time |

Successful rows are never updated or deleted. There is at most one ACTIVE row,
immediately after the successful prefix.

### Adapter differences that affect how you write migrations

| | Oracle | PostgreSQL | sqlite |
|---|---|---|---|
| DDL in atomic mode | no | yes | yes |
| `require_valid` | supported | refused | refused |
| PL/SQL | yes | no, use `DO` | no |
| Namespace lock | `DBMS_LOCK` | session advisory lock | POSIX file lock |
| Separate connect user and schema | yes | no | n/a |
| Parameters | `:name` | `%s` / `%(name)s` | `:name` or `?` |

SQLite is a supported target within the profile below, and it is also the fast
suite for everything the three adapters share. It is not an Oracle emulator: a
migration that works on SQLite has been shown nothing about Oracle's DDL commits,
server locks or transport failures.

An adapter name is recorded in `m8_meta` on the first run and checked on every
later one, so it is part of the namespace binding. The name `sqlite-probe` was
retired: change `database.adapter` to `sqlite`. A namespace that was initialized
under the old name is refused by name, and this release has no metadata repair,
so migrate such a database from its own source of truth rather than editing
`m8_meta`.

### Not in this tool, on purpose

No undo or reverse migrations. No `clean`. No baseline for an existing schema. No
history repair. No forced unlock. No skipping or out-of-order execution. No
repeatable migrations, schema diffing or drift detection. No automatic
recompilation, reconnect or replay.

Each of these exists in other tools and each one trades away a guarantee this tool
makes. If you need one, it is a decision to make deliberately, not a gap to
paper over.
