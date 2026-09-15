# Acceptance report

Specification: [`SPEC.md`](SPEC.md) (v6.2). Implementation: `src/migr8/`.
Architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md). Report date: 2026-09-13.

## Status

Every test listed below passes, and that is not the same thing as a
production-readiness claim. What is open is enumerated under
[Open gates](#open-gates).

What this report claims, for the exact versions recorded below:

- The state machine, fingerprint encoding, manifest rules, lexical scanner and
  pure state validator behave as specified, with deterministic fixtures.
- Oracle and PostgreSQL adapters execute real atomic and restartable migrations,
  hold a real database-backed namespace lock across every commit, and detect a
  broken atomic transaction before writing a SUCCESS row.
- Both directions of commit-acknowledgement failure were induced at transport
  level at all five engine-owned durable transitions, on both databases, with an
  independent observer establishing which branch occurred.
- A lost reply is classified the same way at every database boundary a run
  crosses, not only during migration execution: metadata reads and writes,
  namespace inspection, the snapshot read, `begin` and the transaction-state
  probe each latch the run and discard the connection. The assertion that no SQL
  follows an unknown outcome is made at the driver, so a rollback or a probe
  would be visible.
- An interruption during a commit is classified as an unknown outcome, and a
  supervised `SIGTERM` between commits rolls back and retains ACTIVE. Neither ends
  in a traceback.
- One adapter contract suite passes against all three adapters, so the shared
  rules behave identically on each.
- The SQLite adapter refuses a metadata layout that enforces less than the
  supported one -- a re-keyed, non-unique, unpredicated or differently predicated
  one-ACTIVE index, a missing, widened, weakened or added check constraint, a
  missing unique key, a primary key on another column, a foreign key that is
  absent or points elsewhere, an added index, a table stored with different
  options, and a schema it cannot parse -- in both supported journal modes.
- The SQLite adapter establishes its durability settings and reads each one back:
  journal mode, `synchronous`, foreign-key enforcement and check-constraint
  enforcement. A setting that does not take effect fails the run.
- A real runner killed with `SIGKILL` at each engine-owned durable boundary, in
  both journal modes, leaves a state the next run completes, with work committed
  before the kill not repeated and work after it not lost.
- A Python migration that does not compile, in its entry point or in a helper,
  fails `migrate`, `validate` and `validate --offline` before any connection and
  leaves no metadata behind.
- `validate --offline` completes the plan checks with no connection, no secret,
  no import, no execution and no bytecode, and an approved plan artifact refuses
  an edited, reordered or removed published entry.
- The wheel built from this tree installs on Linux into an environment holding
  nothing else and runs the examples from outside the source tree, and the
  service-free suite passes there against a second SQLite library version.
- A real filesystem filling during a batch commit is reported as an unknown
  outcome, and leaves the durable rows equal to the durable checkpoint.
- The three runbooks in `MANUAL.md` have been walked end to end on all three
  adapters, including an interrupted restartable migration and its rerun, and a
  PostgreSQL namespace dumped and restored as one consistent pair.
- The whole Oracle suite passes in python-oracledb **Thick** mode as well as
  Thin, with the client libraries loaded by the adapter from configuration, and
  a TNS alias from `tnsnames.ora` reaches the same namespace in both modes.
- A proxy connect string, `RUNNER[OWNER]`, authenticates as the runner and runs
  as the owner against the real server, with the objects owned by the owner.
- An initial install runs over TLS, authenticated by a client certificate and
  proxying into the schema, with no password in the configuration or the
  environment.

What it does not claim: nothing about Oracle 19c, thick-mode drivers, wallet or
external authentication, Windows, network filesystems, RAC, Data Guard,
performance, or scale. The SQLite results are results on this host's SQLite
library and Python, within the profile stated in `SPEC.md` §13.3; a killed
process is not a power failure, and no test here removes power or fills a
filesystem.

The Oracle adapter is verified against Oracle Database Free 23ai, which is the
only Oracle release distributed as a freely redistributable container image and
is therefore the release this project's CI and acceptance runs can use. A 19c
result would require a licensed installation; see [Open gates](#open-gates).

## Recorded environment

| Component | Value |
|---|---|
| Host | Darwin 25.6.0, arm64 |
| Python | 3.14.0 (CPython) |
| SQLite library | 3.50.4 |
| python-oracledb | 26.0.0; **Thin** on this host, and **Thick** in the run recorded below (`connection.thin` is read back either way) |
| Oracle Instant Client | 23.9.0.25.07, Linux ARM64, used for the Thick-mode run only |
| psycopg | 3.3.5 (binary) |
| Docker | 27.3.1, aarch64, 12 CPUs, 8 GiB available to the daemon |

A second environment, for the results labelled *Linux* below: `python:3.14-slim`
(Debian, kernel 6.10.14-linuxkit, **aarch64**), Python **3.14.7**, SQLite library
**3.46.1**, run as an unprivileged user. Both SQLite library versions are
therefore covered, 3.50.4 on the host and 3.46.1 there.

The supported floor is Python 3.12 (`requires-python` in `pyproject.toml`).
Every other measurement in this report was taken on 3.14 and is evidence for
3.14 only. CI runs lint, types and the service-free suite on both 3.12 and 3.14
on every push.

The whole suite was run at the floor on 2026-09-15: Python **3.12.12** on Darwin
25.6.0 arm64, against Oracle Free **23.9.0.25.7** and **PostgreSQL 17.5** from
the image digests `testenv/compose.yaml` pins. The services ran under Apple
`container` 1.4.1 and were reached on the container network, not through a
published port; `testenv/dbctl.sh` was not used, because it drives Docker
Compose. Result: **776 passed, 3 skipped** -- the two Oracle TLS tests and the
bounded-filesystem test, no service skip. The same run on 3.14.7 gave the same
counts.

| Service | Image | Digest | Recorded server banner |
|---|---|---|---|
| Oracle | `gvenzl/oracle-free:23.9-slim` | `sha256:945400df5e3fc9589db628223385f906e1024932dc3b72e118fc4fcd0f0e9bbb` | Oracle Database 23ai Free Release 23.0.0.0.0, Version **23.9.0.25.07** |
| PostgreSQL | `postgres:17.5` | `sha256:aadf2c0696f5ef357aa7a68da995137f0cf17bad0bf6e1f17de06ae5c769b302` | **PostgreSQL 17.5** (Debian 17.5-1.pgdg130+1), aarch64, `synchronous_commit=on`, `fsync=on` |

Both images are arm64 here; both digests are multi-architecture indexes, and CI
runs the same suite against the same digests on linux/amd64. PostgreSQL is
published on `127.0.0.1:15433`; `testenv/.env` overrides any port or credential.

## Reproducing this report

```bash
uv sync --all-extras

# Fast gates: pure plus the real SQLite adapter. No service required.
uv run pytest -m "not oracle and not postgres"

# Disposable services: start, health-check, provision test schemas.
testenv/dbctl.sh up
testenv/dbctl.sh versions          # reprints the table above from the live servers

# Everything, with the database environment set.
testenv/dbctl.sh test

# One gate at a time.
testenv/dbctl.sh test -m oracle
testenv/dbctl.sh test -m postgres

testenv/dbctl.sh down              # containers removed, volumes kept
testenv/dbctl.sh destroy           # also removes this project's volumes
```

Examples, each run end to end while preparing this report. Run
`testenv/dbctl.sh provision` first: `tests/test_adapter_contract.py` binds lock
id 4719 in the shared test schemas so it never contends with the other suites,
and the examples are configured for 4711, so a schema left behind by that suite
fails the lock-binding check by design.

```bash
cd examples/sqlite && uv run migr8 migrate && uv run migr8 status
cd examples/oracle   && MIGR8_PASSWORD=... uv run migr8 migrate
cd examples/postgres && MIGR8_PASSWORD=... uv run migr8 migrate
```

Two results below need an environment this host does not have. Both commands
mount the checkout read-only and build nothing into it:

```bash
# The service-free suite on Linux, as an unprivileged user.
docker run --rm -v "$PWD:/src:ro" python:3.14-slim bash -c '
  mkdir /work && tar --exclude=./.venv --exclude=./.git -C /src -cf - . | tar -C /work -xf -
  pip install -q pytest pytest-timeout oracledb "psycopg[binary]"
  useradd -m runner && chown -R runner /work
  su runner -c "cd /work && PYTHONPATH=/work/src python -m pytest -q -m \"not oracle and not postgres\""'

# The filesystem-fills case, on a real bounded filesystem.
docker run --rm --tmpfs /small:rw,size=8m -v "$PWD:/src:ro" python:3.14-slim bash -c '
  mkdir /work && tar --exclude=./.venv --exclude=./.git -C /src -cf - . | tar -C /work -xf -
  pip install -q pytest pytest-timeout
  useradd -m runner && chown -R runner /work /small
  su runner -c "cd /work && MIGR8_SMALL_FS=/small PYTHONPATH=/work/src python -m pytest -q -k fills"'
```

## Results by evidence level

Levels are partitioned by pytest marker: `oracle`, `postgres`, `sqlite`,
and everything else.

| Level | Result | Tests | Evidence |
|---|---|---|---|
| Pure / unit | **PASS** | 278 | Deterministic fixtures, including malformed, colliding and corrupt definitions, and dictionary rows recorded from the live servers. No database. |
| SQLite | **PASS** | 339 | Real SQLite files, real transactions, real cooperating OS processes, real signals and real `SIGKILL`s, in both supported journal modes. |
| PostgreSQL integration | **PASS** | 63 | Real PostgreSQL 17.5, session advisory lock, concurrent processes, transport failures, live metadata mutation. |
| Oracle integration | **PASS** | 99 | Real Oracle 23.9.0.25.07, `DBMS_LOCK`, PL/SQL, DDL, dictionary validity checks, transport failures, live metadata mutation. |
| Oracle 19c release gate | **NOT RUN** | 0 | No 19.x installation is available. See [Open gates](#open-gates). |

Total: **779 tests, 776 passed, 0 failed, 3 skipped** with both services up, run
on 2026-09-13 against the versions recorded above. Run time about 81 s. The three
skips each name the fixture they need: the filesystem-fills case wants a small
bounded filesystem in `MIGR8_SMALL_FS`, and the two certificate tests want the
TLS fixture and the Oracle Client libraries. Both fixtures and the results of
running with them are recorded below. Without the services, 617 run and the live
suites skip with an explicit message naming the missing environment variables; a
skip is never counted as coverage. The
`databases` job in CI runs the whole suite against both servers on every push
and pull request. It prints every skip and fails on one whose reason says a
service was unreachable or unconfigured, which is the failure that would
otherwise pass as a green run. The release build applies the same rule before
it builds anything.

The `checks` job requires 100% branch coverage for `errors`, `fingerprint`,
`latch`, `model` and `statevalidate`. `[tool.coverage.report]` in
`pyproject.toml` lists the modules and states the scope limit: no adapter,
engine or CLI coverage is measured, so that percentage says nothing about them.

The adapter contract suite contributes 88 of those tests, in one file. It is one
suite parametrised over every adapter, and `adapters.SUPPORTED` is asserted
against the parametrisation, so a new adapter cannot skip it.

| File | Tests |
|---|---|
| `tests/test_sqlite.py` | 89 |
| `tests/test_adapter_contract.py` | 88 |
| `tests/test_sqltext.py` | 66 |
| `tests/test_statevalidate.py` | 52 |
| `tests/test_manifest.py` | 49 |
| `tests/test_sqlite_layout.py` | 51 |
| `tests/test_sqlite_faults.py` | 45 |
| `tests/test_metadata_damage.py` | 29 |
| `tests/test_outcome_authority.py` | 22 |
| `tests/test_fingerprint.py` | 21 |
| `tests/test_failure_reporting.py` | 19 |
| `tests/test_offline.py` | 21 |
| `tests/test_entry_contract.py` | 18 |
| `tests/test_cli.py` | 17 |
| `tests/test_diagnostics.py` | 16 |
| `tests/test_unknown_outcome.py` | 12 |
| `tests/test_staging.py` | 11 |
| `tests/test_loader.py` | 10 |
| `tests/test_terminal_result.py` | 10 |
| `tests/test_concurrency_probe.py` | 8 |
| `tests/test_oracle_config.py` | 17 |
| `tests/test_checks.py` | 2 |
| `tests/integration/test_oracle_execution.py` | 27 |
| `tests/integration/test_commit_failure.py` | 20 |
| `tests/integration/test_oracle.py` | 20 |
| `tests/integration/test_postgres.py` | 18 |
| `tests/integration/test_metadata_damage.py` | 14 |
| `tests/integration/test_oracle_concurrency.py` | 5 |
| `tests/integration/test_oracle_tls.py` | 2 |

## Required scenario groups

Groups 1 to 11 are those of specification Section 14.2; 12 to 14 are this
report's own. **PARTIAL** means some listed item in the group is not covered,
and the gap is named.

| # | Group | Result | Notes |
|---|---|---|---|
| 1 | Manifest and fingerprint | **PARTIAL** | Golden canonical encoding is asserted against a hand-built byte string, not against the implementation's own loops. All covered inputs change the fingerprint; required-set reordering does not. Location and position independence, duplicate ids, duplicate and nested units, symlinked roots and in-unit symlinks, special files, bytecode and tool caches, invalid paths, missing entry, changed successful source, and staged source unaffected by later working-tree edits are all covered. Distinct ids `a-b` and `a_b` load distinct helpers. **Gap:** the case-folding collision rule is tested at the function level only; this host's filesystem is case-insensitive, so a colliding pair cannot be created to drive it end to end. |
| 2 | History validation | **PASS** | Valid empty, prefix and full states; position gaps and duplicates; more than one ACTIVE; ACTIVE not next; ACTIVE marked atomic; ACTIVE without an attempt count; SUCCESS without a completion time; atomic SUCCESS carrying an attempt count; incorrect stored language and mode; orphaned progress; progress on a SUCCESS row; modified SUCCESS source; unsupported fingerprint and layout formats; corrupt enumerated fields. |
| 3 | Initialization | **PARTIAL** | Interruption after each independently durable object creation (parametrised over all four objects) and before and after the final marker; permitted completion of an incomplete initialization; missing progress table, missing one-active index and incompatible column layout after completed initialization; populated history without a marker; lock and config binding mismatch. Live mutation in a disposable schema of a `DISABLED` unique key, a dropped foreign key, a replaced one-ACTIVE index expression, a check constraint rewritten as a tautology and a foreign key repointed at a same-named history table in a second disposable schema on Oracle; and of a re-keyed index, an unpredicated index, a `NOT VALID` foreign key, a tautological check constraint and a cross-schema foreign key on PostgreSQL. Each is reported as exit 7 and left unrepaired. At the dictionary-row level, a check constraint that is missing, altered or added is refused on both engines, as is one whose condition the comparison does not recognise. A namespace whose every check constraint was re-created from its own stored definition -- the round trip a logical restore performs -- is still accepted on both engines; on PostgreSQL the test asserts the rendering actually changed, so it cannot pass vacuously, and on Oracle the same round trip leaves the rendering unchanged. On SQLite, in both journal modes, live mutation of a completed namespace covers a one-ACTIVE index that is re-keyed, not unique, unpredicated, predicated on another literal, widened or extended; an index of that name on another table; an added index on a metadata table; a check constraint removed, widened, weakened with an added term or added; a missing unique key; a primary key on another column; a foreign key absent or pointing at another table; a shortened primary key; a table stored with different options that passes every structural check; and a hand-edited schema SQLite cannot parse. Each is exit 7 from both `validate` and `migrate`, and nothing is repaired. **Gap:** loss of dictionary inspection privilege mid-life is not simulated, though the adapter treats an inaccessible probe as an error and the equivalent "target schema absent" path is tested on PostgreSQL. |
| 4 | Atomic | **PASS** | Normal DML; query-only success; statement error rollback; an error inserting the SUCCESS row rolling the migration work back with it; forbidden DDL; the transaction-identity tripwire firing on a real PL/SQL `COMMIT` with the work left durable and no SUCCESS row; no further work after a detected violation; work and history committing together; a latched unknown outcome reported as exit 4 with the connection discarded even when the migration catches it and returns or raises its own exception; and lost completion acknowledgement in **both** proven branches on both databases. |
| 5 | Restartable | **PASS** | Failure after a batch boundary and after DDL plus a batch; completed work present before SUCCESS (induced at the completion boundary); no-op rerun of completed work; open transaction on return rolled back with ACTIVE retained; progress and data committing together and rolling back together; checkpoint derived from the exact processed keys; progress writes outside a batch rejected; nested batches rejected; no continuation after a caught unknown outcome, including when author code replaces it with an exception of its own or swallows every exception; a deferred entry point (`async def`, generator, async generator, or a synchronous wrapper returning one) refused before any history row is written; and a helper imported inside `migrate` reloaded from its edited source on a same-process recovery. |
| 6 | Required objects | **PARTIAL** | Missing, wrong-type and invalid declarations fail; the failure verdict is identical on a retry that skips recreation; a warning-only valid object passes; adding or removing a requirement changes the fingerprint; unsupported declaration types are refused before connecting. **Gaps:** an *ambiguous* `(owner, name, type)` row is not reproduced, because Oracle's dictionary does not permit two such rows in the supported namespace, so the branch exists without a live case; an object made inaccessible by privilege change is not simulated; and "unrelated invalid objects and compilation settings remain untouched" is argued from the read-only implementation rather than asserted by a test. |
| 7 | Recovery admission | **PASS** | Unchanged retry without the flag; changed ACTIVE refused without the flag, reporting both fingerprints and the exact recovery command; wrong and absent active id refused; `--recover` against an absent namespace and against a partly created one refused with exit 2, with no metadata object, no marker and no migration execution left behind, while plain `migrate` still completes the same incomplete namespace; mode change refused even with the flag; SQL-to-Python recovery recording consistent metadata while preserving identity, position, mode, first fingerprint and original start time; convergence from checkpoints written by two earlier source versions; and an edit to a successful migration never admitted. |
| 8 | Concurrency | **PARTIAL** | Deterministic contention at zero timeout on all three adapters; a waiter that later acquires the lock, finds no pending work and exits 0 (SQLite and Oracle); the lock held across DDL and across batch commits (all three); a dead client leaving a live server session that still holds the lock, with the lock released only once that session ends (Oracle, via the proxy); read-only `status` and `validate` during a long migration on all three. On SQLite, an application reader is never shown an uncommitted batch, an application writer holding the database fails the run within the busy timeout and leaves no history behind, and the next run applies the same migration; both cases run in both journal modes. **Gaps:** the waiter-succeeds and dead-client cases are not repeated on PostgreSQL. |
| 9 | Oracle and SQL details | **PARTIAL** | Correct `DBMS_TRANSACTION.LOCAL_TRANSACTION_ID` invocation with an output bind; multiline `UPDATE` with a line beginning `SET`; PL/SQL `EXIT WHEN`; literals and comments containing slashes and semicolons surviving normalisation and executing against the real server; stored PL/SQL keeping its terminator while `CREATE LIBRARY` does not; top-level SQL*Plus commands refused in preflight without rejecting valid internal tokens; direct DDL restrictions; synchronous commit session setup; native driver parameter handling; and the realistic bounded backfill example running against Oracle. **Gap:** `COMMIT_WAIT` cannot be read back at session level on Oracle, so its establishment is reported as NOT VERIFIED rather than asserted. |
| 10 | Inspection | **PASS** | `validate` and `status` perform no initialization, no code import (driven by a unit whose module body raises), no migration execution, no database-object recompilation and no SQLite journal-mode change; consistent history snapshots; uninitialized distinguished from damaged and from incomplete-but-compatible; session-liveness diagnostics reporting `present` where privileges allow and `unknown` where they do not, with the honest caveat attached. |
| 11 | Plan lint | **PASS** | A Python entry point and a helper that do not compile, and a source with a null byte, each fail `migrate`, `validate` and `validate --offline` with exit 2 before any connection, naming the file and line, and leave no database file behind; `validate --offline` creates no database and no lock file, imports nothing (driven by a unit whose module body writes a marker file), and writes no `__pycache__`; a statement the backend does not admit fails offline; an approved plan artifact refuses an edited published unit, a reordered plan and a removed migration, admits appended work, and guards the online command too; a baseline that is not a plan report and a missing one are usage errors. |
| 12 | Interruption and internal failure | **PASS** | Ctrl-C and `SIGTERM` between commits roll back, retain ACTIVE, exit 3 and print no traceback; an interruption during a commit latches an unknown outcome, discards the connection and exits 4; an injected internal defect produces a defined exit code naming the run id rather than a traceback. A real runner killed with `SIGKILL` at each of the six engine-owned boundaries, on either side of the commit, in both SQLite journal modes: the next run completes to the exact expected history and data, each row written once; the atomic commit's two sides are asserted separately; a kill between batches keeps the checkpoint and its data together with the migration ACTIVE; and the dead runner's lock file blocks nothing. A database path that is a directory, or a directory that cannot be created, is a usage error naming the path, and a read-only database file fails the run and changes no history. |
| 13 | Diagnostics | **PASS** | The event log records the phases in order with a single correlation id and monotonic timings; the terminal record names the failing migration and phase; author `ctx.log` lines interleave; a canary planted in a driver message reaches neither the report, the event log nor stderr, and the same holds for a canary in a metadata-read rejection surfacing through `status --json`, one raised at CLI adapter construction reaching the fallback handler, one in a close or rollback cleanup warning, and one in the run log's own write failure; the log appends across runs and is absent unless configured; `--json` carries outcome, failing identity, phase and recovery command. A log file that cannot be opened fails with exit 1 before the command connects; a log whose close fails leaves an acknowledged exit 0 and its durable history unchanged; a log that stops writing while the engine reports exit 4 changes neither the result nor the SQL issued; and a failure handled before the engine is reached carries the run id in both `--json` and stderr. |
| 14 | Adapter contract | **PASS** | Every behaviour below, per adapter: initialization to a verified layout, well-typed empty snapshot, physical-name resolution, engine-transaction state, uncommitted-work detection, transaction identity across a commit, the full admission/attempt/completion lifecycle including the permanence of `first_fingerprint` and `started_at`, a success row riding the caller's transaction, affected-row damage detection, bind narrowing and missing-bind detection, admission in every context, reserved-object refusal, the DDL allow-list, required-object consistency, error classification, and the identifier-level reserved-object rule with both its refusals and its admissions. |

## Linux, storage and the runbooks

These were run on 2026-09-13 with the wheel built from this tree
(`migr8-0.1.0-py3-none-any.whl`), installed into environments that contained
nothing else. The commands are above and in `MANUAL.md`; what follows is what
they reported.

**The service-free suite on Linux.** 607 collected, **606 passed, 1 skipped** in
`python:3.14-slim` on kernel 6.10.14-linuxkit, aarch64, Python 3.14.7, SQLite
3.46.1, as an unprivileged user. The skip is the filesystem-fills case, run
separately below. The same 606 pass on the macOS host against SQLite 3.50.4.

**The installed wheel on Linux.** A venv holding only `migr8` and `pip`; the
package imports from `site-packages`, and the SQLite example runs
`validate --offline`, `migrate`, `validate` and `status` from a copy outside the
source tree, all exit 0.

**A filesystem that fills.** On an 8 MiB tmpfs, a batched migration writing
900-byte rows filled the filesystem and SQLite returned `SQLITE_FULL` **at a
batch commit**. The run reported exit 4, unknown outcome, and latched: SQLite
settles a commit that fails on storage at the next open of the file, not in the
reply, so the tool does not claim which branch occurred. The branch that did
occur was "not committed" -- the durable rows (8000) equal the durable
checkpoint (8000), the migration is ACTIVE, and `status` reads the namespace
cleanly afterwards. This is one observation of one failure point, not a survey
of every place ENOSPC can land.

**Runbook rehearsal.** The three procedures in `MANUAL.md` were walked end to
end, from the installed wheel, on two engines:

| Step | SQLite (Linux, WAL, `synchronous=full`) | Oracle 23.9.0.25.07 (fresh schema, Thin) |
|---|---|---|
| Initial: starting state | `status` exit 6, not initialized | `status` exit 6, not initialized |
| Initial: lint, apply, confirm | exit 0, 0, 0 | exit 0, 0, 0 |
| Routine: published prefix against the approved plan | exit 0 | exit 0 |
| Routine: apply a seed of 200,000 / 100,000 rows | exit 0, **217 ms** | exit 0, **419 ms** |
| Interruption: `SIGKILL` mid-backfill | runner 137; ACTIVE with checkpoint 20,000 of 200,000 rows backfilled | runner 137; ACTIVE, recorded session reported `absent` by the liveness probe |
| Interruption: rerun the same artifact | exit 0, **409 ms**, all 200,000 backfilled | exit 0, **1.27 s**, all 100,000 backfilled, attempt count 2 |
| After each: `validate`, progress rows | exit 0, 0 rows | exit 0, 0 rows |

PostgreSQL 17.5 was walked the same way, from a venv holding only `migr8` and
`psycopg`: `status` exit 6 on the empty namespace, `migrate` exit 0, a seed of
100,000 rows, a backfill killed with `SIGKILL` mid-run leaving ACTIVE with its
checkpoint and the recorded backend reported `absent`, and a rerun that completed
in 882 ms with `validate` exit 0.

Volume and duration are from these runs and nothing else: a local container and
container-hosted servers on one laptop, with batches of 5,000 (seed) and 2,000
(backfill). They are a shape, not a capacity claim. No rehearsal was done on a
production-shaped target.

**A restore, and what it found.** The PostgreSQL namespace was dumped with
`pg_dump -n <schema>` -- application table and `m8_*` together -- the schema was
dropped, and the dump was restored with `psql`. The restored namespace was
**refused as damaged, exit 7**: `pg_dump` writes what `pg_get_constraintdef`
returns, and PostgreSQL re-parses that into a different rendering of the same
condition, with the cast inside the array elements rather than around it. The
three `IN (...)` conditions on `m8_history` were reported as unsupported and
missing at once.

That is a defect this rehearsal existed to find: the documented backup path left
a namespace no run would touch. The adapter now records those renderings as
accepted alternatives for the conditions they stand for, sourced to PostgreSQL
17.5 and dated. After the fix the restored namespace validates (exit 0), reports
its full history, and accepts the next migration. The regression test reproduces
the round trip without `pg_dump`, by re-creating each check constraint from its
own definition, and it fails when the recorded renderings are removed.

On Oracle the same round trip -- every check constraint dropped and re-added
from the dictionary's own `SEARCH_CONDITION` -- leaves the rendering unchanged
and the namespace accepted, so no alternative renderings are recorded there and a
test now says so. A real Oracle restore, through Data Pump or otherwise, has not
been run.

## Oracle driver modes

The adapter selects the mode and reads it back, so "which driver mode was this
tested in" has an answer per run rather than per machine.

**Thin.** 97 Oracle tests pass on the macOS host, python-oracledb 26.0.0, against
Oracle Free 23.9.0.25.07. This is the default and needs no Oracle client.

**Thick.** The same 97 tests pass with Oracle Instant Client 23.9.0.25.07
(Linux ARM64), on 2026-09-13, in `python:3.14-slim` as an unprivileged user,
against the same server. The suite runs in the configured mode end to end: the
generated configs carry `allow_thick_mode` and `client_lib_dir`, so the runners
the tests start in subprocesses load the client libraries too.

The certificate tests need the TLS fixture as well, which adds
`-v "$PWD/testenv/tls:/etc/oracle:ro" -e MIGR8_ORACLE_TLS_ADMIN=/etc/oracle` to
the same command after `testenv/provision_tls.sh` has run. With the fixture in
place the Oracle suite is **99 tests**, all passing.

```bash
# Instant Client, once: unzip instantclient-basic-linux.arm64-23.9.0.25.07.zip
docker run --rm --network migr8-testenv_default \
  -v "$PWD:/src:ro" -v /path/to/instantclient_23_9:/opt/ic:ro \
  -e MIGR8_ORACLE_DSN=migr8-oracle:1521/FREEPDB1 \
  -e MIGR8_ORACLE_USER=MIGR8_TEST -e MIGR8_ORACLE_PASSWORD=... \
  -e MIGR8_ORACLE_SYS_PASSWORD=... \
  -e MIGR8_ORACLE_CLIENT_LIB=/opt/ic -e LD_LIBRARY_PATH=/opt/ic \
  python:3.14-slim bash -c '...install pytest and drivers, then...
    python -m pytest -q -m oracle'
```

Two things that run cost rather than argument. The client resolves its own
libraries through the dynamic loader: this Instant Client build carries no
`$ORIGIN` rpath, so `client_lib_dir` alone is not enough and the directory has to
be on `LD_LIBRARY_PATH` or in `ldconfig`. And the mode is a process-wide decision
that must be made before the first connection, which is why the harness throws
the switch ahead of its own observer connections.

**TNS aliases.** With `oracle.config_dir` pointing at a directory holding
`tnsnames.ora`, `database.dsn` may be an alias. Rehearsed on 2026-09-13 in a
fresh schema: initial install through the alias in Thick mode (`status` exit 6,
`migrate` exit 0, `validate` exit 0), the same namespace validated through the
same alias in Thin mode, and validated again with a `host:port/service` DSN --
the binding records the schema and the lock id, not the spelling of the address.

**Certificate authentication over TLS.** `testenv/provision_tls.sh` builds the
fixture: a server wallet and a client wallet, a TCPS endpoint on the listener
demanding a client certificate, a database user whose identity is the client
certificate's DN, and a proxy grant into a test schema. With it,
`database.user = "[MIGR8_TLS]"`, no `MIGR8_PASSWORD` in the environment and no
secret in the configuration, an initial install runs end to end: `status` exit 6,
`migrate` exit 0, `validate` exit 0, and the session reports
`NETWORK_PROTOCOL = tcps`, `AUTHENTICATION_METHOD = SSL_PROXY`, the certificate
DN as `PROXY_USER` and the schema as `SESSION_USER`, with the application table
and the `m8_*` metadata owned by that schema. A password supplied alongside such
a connect string is refused, because the two say different things about who is
connecting.

Two self-signed certificates and one listener. A real estate's certificate
authority, revocation, expiry and rotation are not exercised, and neither is TLS
without client authentication.

**Proxy authentication.** `database.user = "RUNNER[OWNER]"` authenticates as one
user and runs as another. Covered against the real server: the migration applies
and validates, `USER` in the session is the owner, `PROXY_USER` is the runner,
and the application table and the `m8_*` metadata are owned by the owner schema,
with the runner holding no `ANY` privilege for that path. The database grant is
`ALTER USER OWNER GRANT CONNECT THROUGH RUNNER`, and `testenv/provision_oracle.py`
now makes it for the disposable fixture pair.

One property of Thick mode is worth recording, because the suite found it: the
client is loaded once per process, and the directories given to the first call
are the ones in force. When something else in the process loaded it already --
an embedding application, or this suite's own harness -- `client_lib_dir` and
`config_dir` cannot take effect, and the run logs that rather than reporting
directories it is not using.

## Commit-acknowledgement failure evidence

Specification Section 14.3 requires both directions, separately, at five
boundaries, with the branch established rather than assumed.

| Boundary | Oracle, request not delivered | Oracle, response withheld | PostgreSQL, request not delivered | PostgreSQL, response withheld |
|---|---|---|---|---|
| `initialization_complete` | PASS | PASS | PASS | PASS |
| `restartable_admission` | PASS | PASS | PASS | PASS |
| `atomic_completion` | PASS | PASS | PASS | PASS |
| `restartable_batch` | PASS | PASS | PASS | PASS |
| `restartable_completion` | PASS | PASS | PASS | PASS |

Mechanism, in `tests/proxy.py` and `tests/integration/test_commit_failure.py`:

- Synchronisation is at the engine's own boundary through
  `migr8.testing.hooks`, fired immediately before the engine issues its
  commit. A signal from migration code would not do, because further driver calls
  precede the COMMIT.
- **Request not delivered:** the client-to-server direction is dropped, so the
  commit request never reaches the server, and the transport is then terminated.
  After the original server session has ended, an independent session asserts the
  work and history are absent.
- **Response withheld:** the commit request is allowed through and only its
  response is held. An independent observer connection confirms the durable state
  *while the runner is still blocked*, then the transport is terminated so the
  waiting runner receives an error. On Oracle that error is `DPY-4011`; on
  PostgreSQL a SQLSTATE class `08` condition. Both classify as unknown.
- Each scenario asserts exit code 4, that the connection was discarded rather
  than closed, that no rollback or other SQL was issued after the latch, and that
  a fresh invocation reconciles to a fully successful history.

The dead-client test uses the same proxy to keep the upstream socket open after
the client process is killed, which leaves a live server session. A plain
`SIGKILL` closes the socket and the session ends at once, so that form would not
reach the case under test.

`tests/test_unknown_outcome.py` covers the same contract against the SQLite
probe by replacing the adapter's own `commit`. Those are labelled in the file as
**wrapper simulations, not transport evidence**; the SQLite probe runs in-process
and has no transport to lose, so it produces no unknown outcomes of its own.

## Capabilities: implemented versus tested

| Capability | Oracle | PostgreSQL | SQLite probe |
|---|---|---|---|
| Atomic DML and queries | implemented, tested | implemented, tested | implemented, tested |
| Transactional DDL in atomic mode | prohibited by design | implemented, tested | implemented, tested |
| Restartable DDL | implemented, tested | implemented, tested | implemented, tested |
| `CREATE INDEX CONCURRENTLY` outside a transaction | n/a | implemented, tested | n/a |
| Database-backed namespace lock | implemented, tested (`DBMS_LOCK`) | implemented, tested (session advisory) | not applicable: POSIX file lock, an explicit exception |
| Lock held across every commit | implemented, tested | implemented, tested | implemented, tested |
| Transaction-identity tripwire | implemented, tested on a real PL/SQL `COMMIT` | implemented, tested (`pg_current_xact_id`) | not available; enforcement boundary stated instead |
| Oracle-style `require_valid` checking | implemented, tested | refused by design | refused by design |
| Separate connect user and target schema | implemented, tested | not implemented | not applicable |
| Unknown-outcome classification | implemented, tested at transport level | implemented, tested at transport level | implemented; no unknown outcomes possible |
| Session-liveness diagnostic | implemented, tested in both the privileged and unprivileged paths | implemented, tested | implemented, reports `unknown` |
| Synchronous commit durability | set; **session read-back not available on Oracle** | set and verified by session read-back | n/a |
| Thick-mode driver | refused unless explicitly enabled; **NOT RUN** | n/a | n/a |
| Wallet or external authentication | not implemented; **NOT RUN** | not implemented | n/a |

## Open gates

| Gate | Status | What would close it |
|---|---|---|
| Client-side statement timeout | **NOT IMPLEMENTED, by decision** | Nothing bounds a single migration statement; a blocked statement holds the namespace lock indefinitely. The specification declines a client-side timeout for the initial runner, and adding one would create a new unknown-outcome surface, since a timeout firing during a commit is indistinguishable from a lost acknowledgement. Bound long statements with database policy instead: `DDL_LOCK_TIMEOUT` and resource manager on Oracle, `statement_timeout` on PostgreSQL. |
| Host-to-container TCP under Apple `container` | **BLOCKED on this machine** | Published ports reset and direct container IPs gave "no route to host" while ICMP succeeded, so the Apple `container` deployment runs the migration job as a sibling container. Both engines pass that way. See [`../deploy/apple-container/README.md`](../deploy/apple-container/README.md). The acceptance suite uses the Docker Compose services, which publish working host ports. |
| Oracle 19c release compatibility | **NOT RUN** | The full applicable suite against a real Oracle 19.x installation, with the exact update level recorded. Oracle publishes no freely redistributable 19c container image, so this gate needs a licensed installation. Results on Free 23ai are results on Free 23ai; 19c support is not published on the strength of them. |
| python-oracledb Thick mode | **PASS, one client** | All 97 Oracle tests pass in Thick mode with Instant Client 23.9.0.25.07 on Linux ARM64, recorded above. One client version, one architecture, one server release; and no Thick-mode-only feature is used, so this says the adapter works through that stack, not that it exercises it. |
| TNS aliases and a driver configuration directory | **PASS, aliases only** | `oracle.config_dir` with a `tnsnames.ora` alias resolves in both modes against a real listener. `sqlnet.ora` settings, a wallet directory and Easy Connect Plus parameters are not exercised. |
| Proxy-authenticated connect strings | **PASS** | `RUNNER[OWNER]` migrates and validates against the real server, running as the owner with the objects owned by the owner, and `[SCHEMA]` does the same authenticated by certificate. |
| Wallet and external authentication | **PASS, one fixture** | An initial install over TCPS authenticated by client certificate, proxying into the schema, with no password anywhere; `testenv/provision_tls.sh` builds the fixture and `tests/integration/test_oracle_tls.py` runs it. Self-signed certificates, one listener, one client: no certificate authority, revocation, expiry or rotation, and no TLS-without-client-authentication case. |
| Windows | **NOT SUPPORTED, NOT RUN** | The SQLite adapter refuses to start on Windows by design. Nothing has been run there. |
| Network filesystems, hard-link aliases, shared in-memory SQLite | **OUT OF PROFILE** | Out of the SQLite profile by declaration, not by test. |
| SQLite on Linux | **PASS, one distribution** | The service-free suite runs on Debian (`python:3.14-slim`, kernel 6.10.14-linuxkit, aarch64, SQLite 3.46.1) as an unprivileged user: 606 passed, 1 skipped. CI runs the same suite on ubuntu-latest, amd64, on every push. No other distribution, filesystem or kernel is recorded. |
| Power loss | **NOT RUN** | Killing a process is not removing power: the kernel still writes what the process handed it. Nothing here cuts power to a host or a disk. |
| A filesystem that fills | **PASS, at one failure point** | A real 8 MiB tmpfs filled during a batch commit: `SQLITE_FULL`, exit 4, and the durable rows equal the durable checkpoint. Recorded above. Where else ENOSPC can land -- a journal write, a WAL checkpoint, the metadata insert -- is not surveyed, and no failing physical disk was used. |
| Installation through the operator's index or proxy | **NOT RUN** | The wheel installs into clean environments and runs the examples from outside the source tree: on Linux aarch64 here, and on the CI runner in the release workflow, which also installs the oracle extra and runs it against the live server. Installing a *published* version through the user's own proxy, on the user's own Linux, has not been done, and no x86_64 host other than CI has been recorded. |
| Backup and restore of a SQLite namespace | **PARTIAL** | A backup taken through SQLite's own backup API, restored over the same canonical path, is rehearsed in both journal modes: `status` and `validate` pass on the restored file, history and application data match, and it accepts new work. A restore to another path is refused by the namespace binding. Not covered: a backup taken while a migration is running, and Oracle or PostgreSQL restore procedures. |
| Restoring a PostgreSQL namespace | **PASS, one path** | `pg_dump -n <schema>` and a `psql` restore of application data and `m8_*` together, rehearsed on 17.5; it found the rendering defect recorded above and validates after the fix. Physical backups, PITR and cross-version restores are not covered. |
| Restoring an Oracle namespace | **PARTIAL** | The rendering round trip is covered: re-creating every check constraint from its own `SEARCH_CONDITION` leaves the namespace accepted on 23.9.0.25.07. Data Pump, a physical restore and a cross-version restore have not been run. |
| The runbooks in `MANUAL.md` | **PARTIAL** | All three walked end to end from the installed wheel on 2026-09-13, on SQLite (Linux), Oracle 23.9.0.25.07 and PostgreSQL 17.5, each in a fresh namespace, including an interrupted restartable migration and its rerun; recorded above. Not walked on a production-shaped target, and not by an operator other than the implementation. |
| Oracle RAC, Data Guard, Transaction Guard, Application Continuity | **OUT OF SCOPE** | Explicitly out of this recovery path. |
| Performance and scale | **NOT RUN** | The runbook rehearsal above times one seed and one backfill per engine, at 100,000 and 200,000 rows on one laptop; the largest suite fixture is 2500 rows. No load test, no concurrent application traffic, no long-running migration, and no measurement on target-shaped hardware. |
| Security review | **NOT RUN** | No review of identifier handling, credential handling or the test-hook activation gate by anyone other than the implementation. |

## Remaining risks

1. **The Oracle adapter is verified on 23ai Free only.** The features it leans on
   most -- `DBMS_LOCK`, `DBMS_TRANSACTION.LOCAL_TRANSACTION_ID`, `ALL_ERRORS`,
   function-based unique indexes, `FETCH FIRST` with a bind -- all exist in 19c,
   but "exists" is not "tested".
2. **The facade's statement admission is an honest-mistake guard, not a
   sandbox.** A migration that calls a routine with autonomous transactions,
   external effects, or indirect DDL can defeat it, and the specification says
   so. The tripwire catches a changed or absent transaction identity; it cannot
   see an autonomous transaction.
3. **Required-object completeness is the author's.** The engine checks exactly
   what the manifest declares. A migration that creates an object and forgets to
   declare it will be recorded successful with that object invalid.
4. **Whole-manifest preflight can fail a sound database.** If a future change to
   the lexical scanner rejects a form an already-successful migration uses,
   `migrate` exits 2 even though the history is correct. This is a deliberate
   choice, recorded in Section 11.1 of the specification.
5. **Total metadata loss is outside automatic recovery**, by design. The scheme
   cannot distinguish a never-initialized database from one whose migration
   metadata was deleted wholesale, and it will not recreate successful history.
6. **The one ambiguity branch with no live case** is the ambiguous required-object
   row. Oracle's dictionary will not produce one in the supported namespace, so
   that code path is reasoned about rather than exercised.
7. **Test-hook activation** needs an exact token in `MIGR8_TEST_HOOKS_ENABLE`
   plus an importable module named in `MIGR8_TEST_HOOKS_MODULE`, and neither is
   read from any configuration file. That is deliberate, but it has not been
   reviewed by anyone else.
8. **A blocked statement blocks everything.** With no client-side timeout, one
   migration statement that never returns holds the namespace lock until an
   operator intervenes at the database. This is a known, accepted position rather
   than an oversight; see the gate above.

## Before claiming production readiness

1. Run the full suite against the actual target Oracle release and record its
   update level.
2. Close the thick-mode and authentication gates, or constrain the published
   support claim to what was tested.
3. Obtain an independent review of the implementation, with attention to
   identifier handling in generated SQL and to the unknown-outcome
   classification lists.
4. Decide and document an operational policy for metadata backup and restoration,
   since total metadata loss is explicitly outside this protocol. The SQLite
   procedure is written down; rehearse a restore and record it.
5. Walk the runbooks in `MANUAL.md` on the actual target, with the operator who
   will run them. They have been walked on disposable SQLite and Oracle targets
   here; that is a rehearsal by the implementation, not by the operator.
6. Install the release through the target environment's own index or proxy, on
   the target Linux, and record the architecture, the resolved dependency set and
   the installation self-check.

## Deployment under Apple `container`

Separate from the acceptance gates above, the containerised deployment in
[`../deploy/apple-container/`](../deploy/apple-container/) was run end to end on
this machine. **The run predates the current scripts and has not been repeated
against them**, so read the table as evidence about the runtime rather than about
the files as they stand:

| Component | Version | Result |
|---|---|---|
| `container` CLI | 1.2.2 on macOS 26.6.2, arm64 | runtime works |
| Oracle Database Free | 23.9.0.25.07, `gvenzl/oracle-free:23.9-slim` | **migrate and status pass**, 5 migrations |
| PostgreSQL | 17.5, `postgres:17.5` | **migrate and validate pass**, 4 migrations |
| Migration job image | `python:3.14-slim`, Python 3.14.7 | builds and runs unprivileged |

One change since that run: the job's event log was written inside a container
started with `--rm`, so it disappeared with the container. `ctl.sh` now mounts a
host directory over `/var/log/migr8` and `ctl.sh runlog` prints it. Rebuilt and
checked on 2026-09-13 with `container` CLI 1.4.1: the image builds, and a job run
with that mount leaves `run.jsonl` on the host with the run's events in it. The
database runs in the table above have **not** been repeated.

Two runtime limitations were found and are documented rather than worked around:
Oracle cannot initialise into an Apple `container` named volume, so the disposable
database uses the container's own writable layer; and host-to-container TCP did not
work on this machine, so the job runs as a sibling container, which is the
production shape anyway.
