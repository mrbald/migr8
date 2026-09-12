# User manual

`migr8` applies one ordered sequence of migrations to one database namespace.
It has three commands and no undo.

Status: **experimental**. See [`ACCEPTANCE.md`](ACCEPTANCE.md) for what has
actually been tested. The specification is [`SPEC.md`](SPEC.md); this manual is
how to use the thing.

**Contents** — [Install](#install) · [Configure](#configure) ·
[Write a migration](#write-a-migration) · [Run it](#run-it) ·
[Read the status](#read-the-status) · [When something fails](#when-something-fails) ·
[Recovery](#recovery) · [Diagnose](#diagnose-a-failure) ·
[Teams](#working-in-a-team) · [Deployment](#deployment) · [Reference](#reference)

---

## Install

Requires Python 3.14 or later.

```bash
uv sync --all-extras          # or: pip install '.[oracle,postgres]'
uv run migr8 --version
```

Drivers are optional extras. `oracle` pulls `python-oracledb`, `postgres` pulls
`psycopg`, and the SQLite probe needs nothing.

## Configure

Two files. A **configuration** says which database; a **manifest** says which
migrations, in what order. Neither ever contains a password.

```toml
# migr8.toml
[database]
adapter = "oracle"                     # oracle | postgres | sqlite-probe
dsn = "db.internal:1521/ORDERS"
user = "ORDERS_MIGRATOR"
target_schema = "ORDERS"               # defaults to the connect user

[oracle]
ddl_lock_timeout_seconds = 30

[lock]
provider = "dbms_lock"                 # advisory for PostgreSQL, file for the probe
package = "SYS.DBMS_LOCK"
id = 4711                              # every cooperating runner must use this id
timeout_seconds = 60
```

The password comes from `MIGR8_PASSWORD`, nowhere else:

```bash
export MIGR8_PASSWORD="$(read-from-your-secret-store)"
```

Three settings deserve thought:

**`lock.id`** is the namespace lock. Every runner that might touch this namespace
must use the same id, or they will not exclude each other. It is recorded on first
run and verified on every later run; changing it is a configuration error, not a
migration.

**`target_schema`** is where metadata and migration objects live. On Oracle it may
differ from the connect user; the engine sets `CURRENT_SCHEMA` and still qualifies
its own writes, so migration code cannot redirect them.

**`lock.timeout_seconds`** is how long to wait for another runner to finish. It is
an operator policy, unrelated to how long a statement may take.

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
            ctx.progress.set("last_id", str(last))        # commits with the batch
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
```

Each takes `--config`, `--manifest`, `--log-file` and `--json`; `migrate` also
takes `--recover ID`.

`migrate` does this, in order: load and validate both files; stage every unit into
a private directory and fingerprint the copies; connect, set up the session and
acquire the namespace lock; initialize or verify metadata; read history and
validate it; execute the pending suffix in order.

Staging means an edit to your working tree after `migrate` starts cannot change
what runs. Exactly one runner executes at a time, enforced by the database lock —
a second runner waits, and if it finds nothing left to do it exits 0, which is
correct and not an error.

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

Your amended code must converge from **every** state any earlier admitted version
could have left, including its checkpoint format. Existing progress rows are still
there; reading the old key and writing a new one is part of recovery logic:

```python
def migrate(ctx):
    cursor = ctx.progress.get("cursor")
    legacy = ctx.progress.get("last_id")          # written by the previous version
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
phase. Passwords, DSNs and bind values are never written.

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
* Back up the migration metadata with the application data. Total metadata loss is
  outside automatic recovery, by design.

## Reference

### Commands

```text
migr8 migrate  [--config PATH] [--manifest PATH] [--recover ID] [--log-file PATH] [--json]
migr8 validate [--config PATH] [--manifest PATH] [--log-file PATH] [--json]
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

| | Oracle | PostgreSQL | sqlite-probe |
|---|---|---|---|
| DDL in atomic mode | no | yes | yes |
| `require_valid` | supported | refused | refused |
| PL/SQL | yes | no, use `DO` | no |
| Namespace lock | `DBMS_LOCK` | session advisory lock | POSIX file lock |
| Separate connect user and schema | yes | no | n/a |
| Parameters | `:name` | `%s` / `%(name)s` | `:name` or `?` |

`sqlite-probe` is for development and tests. It is not an Oracle emulator and
proves nothing about production behaviour.

### Not in this tool, on purpose

No undo or reverse migrations. No `clean`. No baseline for an existing schema. No
history repair. No forced unlock. No skipping or out-of-order execution. No
repeatable migrations, schema diffing or drift detection. No automatic
recompilation, reconnect or replay.

Each of these exists in other tools and each one trades away a guarantee this tool
makes. If you need one, it is a decision to make deliberately, not a gap to
paper over.
