# Acceptance report

Specification: [`SPEC.md`](SPEC.md) (v6.1, maintained). Implementation:
`src/migr8/`. Architecture review and the decisions taken:
[`ARCHITECTURE.md`](ARCHITECTURE.md). Report date: 2026-09-12, second pass after
the review.

The tool was renamed from `flyway.py` / `flywaypy` to `migr8` after that pass.
The rename also changed the metadata object prefix from `fw_` to `m8_`, the
fingerprint domain string, the `MIGR8_*` environment variables and the default
configuration name `migr8.toml`. The full suite, the three examples and the
recorded versions below were re-run after the rename against freshly created
services, with the same results. The Apple `container` deployment section at the
end was **not** re-run.

## Status

**Experimental. Not production-ready.** Every test listed below passes, and that
is not the same claim. The gates that would be needed for a production claim are
enumerated under [Open gates](#open-gates), and the largest of them, the Oracle
19c release gate, is **NOT RUN**.

What this report does claim, for the exact versions recorded below:

- The state machine, fingerprint encoding, manifest rules, lexical scanner and
  pure state validator behave as specified, with deterministic fixtures.
- Oracle and PostgreSQL adapters execute real atomic and restartable migrations,
  hold a real database-backed namespace lock across every commit, and detect a
  broken atomic transaction before writing a SUCCESS row.
- Both directions of commit-acknowledgement failure were induced at transport
  level at all five engine-owned durable transitions, on both databases, with an
  independent observer establishing which branch occurred.
- An interruption during a commit is classified as an unknown outcome, and a
  supervised `SIGTERM` between commits rolls back and retains ACTIVE. Neither ends
  in a traceback.
- One adapter contract suite passes against all three adapters, so the shared
  rules behave identically on each.

What it does not claim: nothing about Oracle 19c, thick-mode drivers, wallet or
external authentication, Windows, network filesystems, RAC, Data Guard,
performance, or scale.

## Recorded environment

| Component | Value |
|---|---|
| Host | Darwin 25.6.0, arm64 |
| Python | 3.14.0 (CPython) |
| SQLite library | 3.50.4 |
| python-oracledb | 26.0.0, **Thin mode** (`connection.thin` verified `True`) |
| psycopg | 3.3.5 (binary) |
| Docker | 27.3.1, aarch64, 12 CPUs, 8 GiB available to the daemon |

| Service | Image | Digest | Recorded server banner |
|---|---|---|---|
| Oracle | `gvenzl/oracle-free:23.9-slim` | `sha256:945400df5e3fc9589db628223385f906e1024932dc3b72e118fc4fcd0f0e9bbb` | Oracle Database 23ai Free Release 23.0.0.0.0, Version **23.9.0.25.07** |
| PostgreSQL | `postgres:17.5` | `sha256:aadf2c0696f5ef357aa7a68da995137f0cf17bad0bf6e1f17de06ae5c769b302` | **PostgreSQL 17.5** (Debian 17.5-1.pgdg130+1), aarch64, `synchronous_commit=on`, `fsync=on` |

Both images are arm64. PostgreSQL is published on `127.0.0.1:15433` rather than
the documented default, because an unrelated container on this host already held
`15432`; nothing belonging to that container was changed.

## Reproducing this report

```bash
uv sync --all-extras

# Fast gates: pure plus the SQLite probe. No service required.
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
`testenv/dbctl.sh provision` first: the integration suites bind lock id 4719 in
the shared test schemas, and the examples are configured for 4711, so an
unprovisioned schema fails the lock-binding check by design.

```bash
cd examples/sqlite-probe && uv run migr8 migrate && uv run migr8 status
cd examples/oracle   && MIGR8_PASSWORD=... uv run migr8 migrate
cd examples/postgres && MIGR8_PASSWORD=... uv run migr8 migrate
```

## Results by evidence level

| Level | Result | Tests | Evidence |
|---|---|---|---|
| Pure / unit | **PASS** | 209 | Deterministic fixtures, including malformed, colliding and corrupt definitions. No database. |
| SQLite probe | **PASS** | 92 | Real SQLite files, real transactions, real cooperating OS processes, real signals. |
| PostgreSQL integration | **PASS** | 48 | Real PostgreSQL 17.5, session advisory lock, concurrent processes, transport failures. |
| Oracle integration | **PASS** | 81 | Real Oracle 23.9.0.25.07, `DBMS_LOCK`, PL/SQL, DDL, dictionary validity checks, transport failures. |
| Oracle 19c release gate | **NOT RUN** | 0 | No 19.x installation is available in this environment. See [Open gates](#open-gates). |

Total: **430 tests, 430 passed, 0 failed, 0 skipped** when all services are up.
Run time about 51 s. Without the services, 301 run and the live suites skip with
an explicit message naming the missing environment variables; a skip is never
counted as coverage.

The adapter contract suite contributes 61 of those tests: 21 behaviours across
three adapters, in one file. It earned its place on first run by finding a real
defect, a nested `begin()` accepted on Oracle, described in
[`ARCHITECTURE.md`](ARCHITECTURE.md#5-oracle-accepted-a-nested-begin--fixed).

| File | Tests |
|---|---|
| `tests/test_sqltext.py` | 66 |
| `tests/test_adapter_contract.py` | 61 |
| `tests/test_manifest.py` | 48 |
| `tests/test_sqlite_probe.py` | 40 |
| `tests/test_statevalidate.py` | 38 |
| `tests/test_fingerprint.py` | 21 |
| `tests/test_cli.py` | 17 |
| `tests/test_diagnostics.py` | 16 |
| `tests/test_unknown_outcome.py` | 11 |
| `tests/test_loader.py` | 10 |
| `tests/test_staging.py` | 8 |
| `tests/test_concurrency_probe.py` | 5 |
| `tests/integration/test_oracle_execution.py` | 26 |
| `tests/integration/test_commit_failure.py` | 20 |
| `tests/integration/test_oracle.py` | 20 |
| `tests/integration/test_postgres.py` | 18 |
| `tests/integration/test_oracle_concurrency.py` | 5 |

## Required scenario groups

Groups are those of specification Section 14.2. **PARTIAL** means some listed
item in the group is not covered, and the gap is named.

| # | Group | Result | Notes |
|---|---|---|---|
| 1 | Manifest and fingerprint | **PARTIAL** | Golden canonical encoding is asserted against a hand-built byte string, not against the implementation's own loops. All covered inputs change the fingerprint; required-set reordering does not. Location and position independence, duplicate ids, duplicate and nested units, symlinked roots and in-unit symlinks, special files, bytecode and tool caches, invalid paths, missing entry, changed successful source, and staged source unaffected by later working-tree edits are all covered. Distinct ids `a-b` and `a_b` load distinct helpers. **Gap:** the case-folding collision rule is tested at the function level only; this host's filesystem is case-insensitive, so a colliding pair cannot be created to drive it end to end. |
| 2 | History validation | **PASS** | Valid empty, prefix and full states; position gaps and duplicates; more than one ACTIVE; ACTIVE not next; ACTIVE marked atomic; ACTIVE without an attempt count; SUCCESS without a completion time; atomic SUCCESS carrying an attempt count; incorrect stored language and mode; orphaned progress; progress on a SUCCESS row; modified SUCCESS source; unsupported fingerprint and layout formats; corrupt enumerated fields. |
| 3 | Initialization | **PARTIAL** | Interruption after each independently durable object creation (parametrised over all four objects) and before and after the final marker; permitted completion of an incomplete initialization; missing progress table, missing one-active index and incompatible column layout after completed initialization; populated history without a marker; lock and config binding mismatch. **Gaps:** an Oracle constraint left `DISABLED` or `NOVALIDATE` is not driven end to end, though the validation code checks both; and loss of dictionary inspection privilege mid-life is not simulated, though the adapter treats an inaccessible probe as an error and the equivalent "target schema absent" path is tested on PostgreSQL. |
| 4 | Atomic | **PASS** | Normal DML; query-only success; statement error rollback; an error inserting the SUCCESS row rolling the migration work back with it; forbidden DDL; the transaction-identity tripwire firing on a real PL/SQL `COMMIT` with the work left durable and no SUCCESS row; no further work after a detected violation; work and history committing together; and lost completion acknowledgement in **both** proven branches on both databases. |
| 5 | Restartable | **PASS** | Failure after a batch boundary and after DDL plus a batch; completed work present before SUCCESS (induced at the completion boundary); no-op rerun of completed work; open transaction on return rolled back with ACTIVE retained; progress and data committing together and rolling back together; checkpoint derived from the exact processed keys; progress writes outside a batch rejected; nested batches rejected; and no continuation after a caught unknown outcome, including when author code swallows every exception. |
| 6 | Required objects | **PARTIAL** | Missing, wrong-type and invalid declarations fail; the failure verdict is identical on a retry that skips recreation; a warning-only valid object passes; adding or removing a requirement changes the fingerprint; unsupported declaration types are refused before connecting. **Gaps:** an *ambiguous* `(owner, name, type)` row is not reproduced, because Oracle's dictionary does not permit two such rows in the supported namespace, so the branch exists without a live case; an object made inaccessible by privilege change is not simulated; and "unrelated invalid objects and compilation settings remain untouched" is argued from the read-only implementation rather than asserted by a test. |
| 7 | Recovery admission | **PASS** | Unchanged retry without the flag; changed ACTIVE refused without the flag, reporting both fingerprints and the exact recovery command; wrong and absent active id refused; mode change refused even with the flag; SQL-to-Python recovery recording consistent metadata while preserving identity, position, mode, first fingerprint and original start time; convergence from checkpoints written by two earlier source versions; and an edit to a successful migration never admitted. |
| 8 | Concurrency | **PARTIAL** | Deterministic contention at zero timeout on all three adapters; a waiter that later acquires the lock, finds no pending work and exits 0 (SQLite probe and Oracle); the lock held across DDL and across batch commits (all three); a dead client leaving a live server session that still holds the lock, with the lock released only once that session ends (Oracle, via the proxy); read-only `status` and `validate` during a long migration on all three. **Gaps:** the waiter-succeeds and dead-client cases are not repeated on PostgreSQL. |
| 9 | Oracle and SQL details | **PARTIAL** | Correct `DBMS_TRANSACTION.LOCAL_TRANSACTION_ID` invocation with an output bind; multiline `UPDATE` with a line beginning `SET`; PL/SQL `EXIT WHEN`; literals and comments containing slashes and semicolons surviving normalisation and executing against the real server; stored PL/SQL keeping its terminator while `CREATE LIBRARY` does not; top-level SQL*Plus commands refused in preflight without rejecting valid internal tokens; direct DDL restrictions; synchronous commit session setup; native driver parameter handling; and the realistic bounded backfill example running against Oracle. **Gap:** `COMMIT_WAIT` cannot be read back at session level on Oracle, so its establishment is reported as NOT VERIFIED rather than asserted. |
| 11 | Interruption and internal failure **[new]** | **PASS** | Ctrl-C and `SIGTERM` between commits roll back, retain ACTIVE, exit 3 and print no traceback; an interruption during a commit latches an unknown outcome, discards the connection and exits 4; an injected internal defect produces a defined exit code naming the run id rather than a traceback. |
| 12 | Diagnostics **[new]** | **PASS** | The event log records the phases in order with a single correlation id and monotonic timings; the terminal record names the failing migration and phase; author `ctx.log` lines interleave; credentials, DSNs and bind values are excluded; the log appends across runs and is absent unless configured; `--json` carries outcome, failing identity, phase and recovery command. |
| 13 | Adapter contract **[new]** | **PASS** | 21 behaviours per adapter: initialization to a verified layout, well-typed empty snapshot, physical-name resolution, engine-transaction state, uncommitted-work detection, transaction identity across a commit, the full admission/attempt/completion lifecycle including the permanence of `first_fingerprint` and `started_at`, a success row riding the caller's transaction, affected-row damage detection, bind narrowing and missing-bind detection, admission in every context, reserved-object refusal, the DDL allow-list, required-object consistency, and error classification. |
| 10 | Inspection | **PASS** | `validate` and `status` perform no initialization, no code import (driven by a unit whose module body raises), no migration execution, no compilation and no SQLite WAL change; consistent history snapshots; uninitialized distinguished from damaged and from incomplete-but-compatible; session-liveness diagnostics reporting `present` where privileges allow and `unknown` where they do not, with the honest caveat attached. |

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
the client process is killed, which is what produces a genuinely live server
session. A plain `SIGKILL` closes the socket and the session ends at once, so
that weaker form would have proved nothing.

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
| Oracle 19c release compatibility | **NOT RUN** | The full applicable suite against a real Oracle 19.x installation, with the exact update level recorded. Oracle Free 23.9.0.25.07 results say nothing about 19c. Until then no 19c support may be published. |
| python-oracledb Thick mode | **NOT RUN** | Run the Oracle suite with `oracle.allow_thick_mode = true` against a client installation. The adapter refuses thick mode by default precisely so this gate cannot be skipped silently. |
| Wallet and external authentication | **NOT RUN** | A fixture using a wallet, plus the supported-combination matrix. The password path is the only one tested. |
| Windows | **NOT SUPPORTED, NOT RUN** | The SQLite probe refuses to start on Windows by design. Nothing has been run there. |
| Network filesystems, hard-link aliases, shared in-memory SQLite | **OUT OF PROFILE** | Out of the first probe profile by declaration, not by test. |
| Oracle RAC, Data Guard, Transaction Guard, Application Continuity | **OUT OF SCOPE** | Explicitly out of this MVP's recovery path. |
| Performance and scale | **NOT RUN** | No load, volume or long-running migration measurements were taken. The largest fixture is 2500 rows. |
| Security review | **NOT RUN** | No review of identifier handling, credential handling or the test-hook activation gate by anyone other than the implementation. |

## Remaining risks

1. **The 19c gap is the dominant risk.** The Oracle features this engine leans on
   most, `DBMS_LOCK`, `DBMS_TRANSACTION.LOCAL_TRANSACTION_ID`, `ALL_ERRORS`,
   function-based unique indexes, `FETCH FIRST` with a bind, all exist in 19c, but
   "exists" is not "tested". Treat the Oracle adapter as verified on 23ai Free
   only.
2. **Trusted code is genuinely trusted.** The facade's statement admission is an
   honest-mistake guard. A migration that calls a routine with autonomous
   transactions, external effects, or indirect DDL can defeat it, and the
   specification says so. The tripwire catches a changed or absent transaction
   identity; it cannot see an autonomous transaction.
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
   since total metadata loss is explicitly outside this protocol.

Until all four are done, the package stays labelled experimental.

## Deployment verified under Apple `container`

**Recorded before the rename to `migr8` and not re-run since.** The container and
image names, the mount paths and the environment variables in
[`../deploy/apple-container/`](../deploy/apple-container/) were renamed with
everything else, so this table describes the previous run, not the current
scripts.

Separate from the acceptance gates above, the containerised deployment in
[`../deploy/apple-container/`](../deploy/apple-container/) was run end to end on
this machine:

| Component | Version | Result |
|---|---|---|
| `container` CLI | 1.2.2 on macOS 26.6.2, arm64 | runtime works |
| Oracle Database Free | 23.9.0.25.07, `gvenzl/oracle-free:23.9-slim` | **migrate and status pass**, 5 migrations |
| PostgreSQL | 17.5, `postgres:17.5` | **migrate and validate pass**, 4 migrations |
| Migration job image | `python:3.14-slim`, Python 3.14.7 | builds and runs unprivileged |

Two runtime limitations were found and are documented rather than worked around:
Oracle cannot initialise into an Apple `container` named volume, so the disposable
database uses the container's own writable layer; and host-to-container TCP did not
work on this machine, so the job runs as a sibling container, which is the
production shape anyway.
