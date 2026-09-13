# HATS architecture and implementation review

**REWORK before merge. Retain the architecture; close the remaining enforcement
and reporting gaps. A rewrite is not justified.**

Reviewed on 2026-09-13. Base commit:
`bc5d2d500751c0d9cefb93883279303b2de1a40f`, **including the uncommitted fixes
present when this review began**. References below describe that worktree, not
the base commit alone. The previous F1-F8 handover described older behavior;
its disposition is recorded below.

This is a single-reviewer assessment using the HATS core from `~/src/hats`.
It applies spec-reframe, prior-art-check, the composition and ownership
principles, and wall-signal's prior mode when evaluating proposed remedies.
Story-refinement adds no separate task: the requested review and output are
already defined. Candidate and specified tools were not treated as validated.
There was no independent reviewer or controlled comparison measuring HATS'
benefit. No persistent HATS wiring was installed.

## Verdict

| Area | Decision | Reason |
|---|---|---|
| Product scope | RETAIN | Ordered, fingerprinted migrations with explicit recovery and batch checkpoints form a coherent scope. |
| Oracle-first design | RETAIN | Oracle's independently durable DDL is a real constraint. PostgreSQL provides a second implementation with different transaction behavior. |
| Pure state validation | RETAIN | Keep planning separate from I/O and mutation. |
| Shared adapter rules | RETAIN, tighten enforcement | Shared failure policy exists, but callers can still bypass it. |
| Metadata verification | REWORK | Some comparisons accept different constraints and foreign-key targets. |
| Diagnostics and terminal handling | REWORK | Safety and output rules stop short of several error and cleanup paths. |
| New framework, ORM, plugin system, or generic SQL parser | REJECT | None addresses the verified gaps more cheaply than changes at the existing boundaries. |
| Merge this implementation as reviewed | NO | R1-R5 below remain; live validation of the reviewed changes was not run here. |

## Ranked findings

### R1 - P1: The operation guard remains optional at database boundaries

Evidence: `src/migr8/context.py:258` (`_BatchContext.__enter__`),
`src/migr8/adapters/base.py:509` (`_exec`, `_fetch`),
`src/migr8/engine.py:173` (`_terminate`), and
`src/migr8/adapters/oracle.py:504` (`_metadata_execute`).

The new `Adapter.guarded()` handles the entry paths it wraps. Batch entry still
calls `has_open_transaction()` and `begin()` directly. Engine metadata writes
and reads also call raw adapter methods. A communication failure on these
paths does not latch the run. `_terminate()` treats an unlatched driver failure
as an ordinary internal failure and attempts rollback and normal close.

Two independent wrapper probes over real SQLite execution reproduced this:

| Injected failure | Observed result |
|---|---|
| Communication-class exception during the victim's SUCCESS insertion | Exit 3, latch OPEN, `rollback()` then `close()`; no discard. |
| Same class of exception in the transaction-state probe at batch entry; author catches it and returns after a query | Exit 0, latch OPEN, victim recorded SUCCESS. |

The second probe used an exception the adapter classifies as communication
failure. It establishes the missing enforcement, not a real SQLite transport
loss or a demonstrated production data-loss sequence. Oracle's transaction-state
probe does perform database I/O. The required behavior for a failed database
call must not depend on which public API reached it.

**Narrow remedy:** finish the operation boundary inside the adapter. Guard
metadata operations and transaction-state/begin operations, as well as migration
execution. Keep raw driver methods behind the guarded entry points. Preserve
setup, ordinary rejection, interruption, and unknown-outcome distinctions;
classify before cleanup. Keep metadata SQL separate from author SQL admission.
Do not add another orchestration layer or duplicate try/except blocks in every
caller.

**Required evidence:** inject failure at each remaining boundary, with author
catch-and-return and catch-and-rethrow where reachable. Assert the exit code,
latch, no SUCCESS or later migration, and discard. Trace the actual driver
execute/commit/rollback calls to prove no SQL follows uncertainty. The existing
tracer at `tests/test_outcome_authority.py:68` wraps only `_run`; metadata,
transaction probes and rollback bypass it. An empty `sql_after` list there does
not establish the full claim. Extend the applicable live failure tests.

### R2 - P2: Metadata comparison still accepts constraints with different meaning

Evidence: `src/migr8/adapters/oracle.py:595` (`_constraint_problems`),
`src/migr8/adapters/postgres.py:390` (`_constraint_problems`), and
`src/migr8/adapters/metadata.py:39` (`ExpectedConstraints`).

The one-ACTIVE index checks have been tightened, but CHECK constraints still
use substring containment. Adding `OR 1=1` to each required history CHECK
returned **no problems** in both Oracle and PostgreSQL dictionary-row probes.
Those checks enforce nothing. Valid current rows do not establish that the
layout will enforce its declared constraints on later writes.

Foreign-key comparison retains table and column names but drops the referenced
schema. A PostgreSQL dictionary row for
`REFERENCES other_schema.m8_history(migration_id)` also returned **no problems**.
Oracle looks up the referenced constraint using its owner, then discards that
owner before comparison. A same-named history table in another schema can
therefore pass the target check while linking progress to the wrong namespace.

These are synthetic dictionary-row results. No live schema mutation was run in
this review. The problem concerns acceptance of an already damaged layout;
normal initialization was not shown to create these definitions.

**Narrow remedy:** compare the complete supported CHECK forms and fully
qualified referenced object identity, including ordered column correspondence.
Use narrow renderings observed on the supported database versions. The current
normalizers also uppercase literal contents and remove whitespace inside them;
do not copy that operation uncritically into an exact semantic comparison.
Reject unknown forms rather than building arbitrary SQL equivalence.

**Required evidence:** real mutations to tautological CHECKs and a foreign key
pointing to a same-named table in another disposable schema. Expect exit 7 and
no repair. Preserve healthy renderings and existing index/key-state tests.

### R3 - P2: Safe error reporting is not enforced on every output path

Evidence: `src/migr8/adapters/base.py:324` (`inspect_metadata`),
`src/migr8/cli.py:211` (unexpected-error handler),
`src/migr8/engine.py:258` (`_close_quietly`), and
`docs/ARCHITECTURE.md:154` (output policy).

The new safe exception description works on the tested execution path. Other
paths still interpolate arbitrary exception text:

- A synthetic canary in a metadata-read `sqlite3.OperationalError` reached the
  `status --json` report with exit 7 through `inspect_metadata()`.
- A synthetic unexpected exception at CLI adapter construction reached stderr
  and the JSONL log. The CLI uses raw exception text and `LOGGER.exception()`.
- Close and rollback warnings still format raw exceptions. This last point is
  source evidence; the first two were executed.

Only synthetic text was used. No real credential was exposed. The test named
`test_a_canary_during_setup_does_not_reach_the_report`
(`tests/test_failure_reporting.py:154`) injects at application `CREATE TABLE t`,
after setup, rather than connection, adapter construction or metadata inspection.
It does not cover the boundary named by the test.

**Narrow remedy:** make safe exception description available at the CLI and
inspection boundaries, including cleanup warnings. Keep safe engine-authored
messages distinct from driver text when constructing `Migr8Error`; the renderer
cannot assume that wrapping a raw exception makes its message safe. Author
`ctx.log()` content remains author-owned. Reconcile the conflicting universal
and setup-exception wording in the architecture document.

**Required evidence:** drive actual CLI failure handlers and metadata inspection,
not just `Engine.run()`. Put canaries in messages and causes; check stderr,
verbose output, JSON reports, logs and cleanup warnings. Keep phase, identity,
run id and safe error codes available.

### R4 - P2: Optional diagnostics can override an acknowledged migration result

Evidence: `src/migr8/cli.py:196` and `:222`,
`src/migr8/diagnostics.py:49` and `:82`.

`RunLog` construction happens before the CLI's try block. Log close happens in
an unprotected finally block. A log-open error escapes the advertised error
handling; a close error can replace a completed return value.

A probe supplied a log handle whose writes succeeded and whose `close()` raised
`OSError`. The real `RunLog.close()` propagated it. Both migrations were durably
SUCCESS and stdout already contained an exit-0 JSON report, but `main()` raised
instead of returning 0. A process supervisor would receive a result inconsistent
with the emitted report.

A separate, unmodified CLI invocation with a missing configuration and `--json`
returned 1 with empty stdout and no run id in the error. Early failures do not
share the normal machine-readable result path.

**Narrow remedy:** give the command one terminal result owner. Validate explicit
log setup before database mutation, handling failure with a defined result.
After execution, diagnostic teardown must preserve the known database outcome.
Render handled command failures consistently, including `--json` and run id.
Do not claim that no migration was admitted merely because an exception reached
the CLI fallback; failures can also arise during post-execution handling.

**Required evidence:** log-open failure before work, log-close failure after
acknowledged success, and diagnostic failure while reporting exit 4. Verify that
reporting neither changes the result nor triggers database work or replay.

### R5 - P2: Invalid recovery requests initialize metadata before rejection

Evidence: `src/migr8/engine.py:160` (`run`), `:266`
(`_initialize_if_needed`), and `docs/SPEC.md:467`.

`run()` prepares storage and initializes metadata before building the plan and
checking recovery admission. On a new SQLite database, `migrate --recover absent`
returned exit 2 but created all four metadata objects and committed the singleton
marker. No migration body ran.

Section 10.1 requires a missing ACTIVE identity to fail before metadata mutation.
The current ordering violates that contract. On Oracle the same orchestration
reaches independently durable initialization DDL; that consequence is inferred
from source, not a live result from this review.

**Narrow remedy:** under the acquired namespace lock, inspect the namespace and
reject recovery when no completed metadata/ACTIVE state can support it before
storage preparation or initialization. Reuse the existing validation rules.
Ordinary `migrate` should retain recoverable initialization.

**Required evidence:** absent and incomplete namespaces with `--recover`; assert
exit 2, no new objects or marker, and no migration execution. Keep valid changed
ACTIVE recovery and wrong-id tests on initialized namespaces.

## Architecture decisions from the HATS perspectives

### Frame check: retain the bounded migration engine

The useful domain operation is applying an ordered change while retaining enough
source and durable-state information to resume safely after interruption.
SQL/Python are authoring forms; atomic/restartable describe execution guarantees.
These are separate axes. Sharing their safety rules is necessary even when their
admission and invocation mechanisms differ. R1 exposes an incomplete implementation
of that separation, not a need for a third execution mode.

The adapter boundary already serves two production database implementations.
Oracle-specific transaction identity and object-validity checks should remain
explicit. Do not generalize the project into a workflow engine, cross-database
migration language, schema-diff product or distributed deployment coordinator.

Keep the ordered stream, immutable successful prefix, one ACTIVE migration,
staged fingerprints and checkpoint coupling. These are durable protocol choices:
changing them later would require history-format and recovery compatibility work.
Internal method boundaries are reversible and can be improved without changing
stored history or migration source.

### Prior-art check: a custom engine remains defensible for this contract

Primary documentation checked on 2026-09-13. This is a bounded comparison, not
an exhaustive feature audit or proof that no extension can match migr8.

| Alternative | Verified overlap | Fit and ownership judgment |
|---|---|---|
| Flyway | Ordered migrations; per-migration transactions; nontransactional execution. Its documentation describes manual cleanup and possible history repair after failures on databases with implicit DDL commits. | A strong alternative for conventional migration management. Matching migr8's explicit ACTIVE/amended-source/checkpoint recovery contract would require additional design and validation. |
| Liquibase | Changesets with transactional and nontransactional execution. Its `runInTransaction` documentation warns about an invalid tracking-table state after partial failure in a multi-statement nontransactional changeset. | Broader change management does not by itself discharge migr8's partial-progress recovery obligations. No product-wide disqualifier was established. |
| Sqitch | Ordered deployment with deploy/revert/verify scripts, including Oracle support in its documentation. | Relevant if deployment scripts and separate verification are the desired contract. This review did not establish an equivalent to migr8's changed-ACTIVE admission and transaction-coupled Python checkpoints. |
| Driver scripts plus a deployment job | Existing native drivers can execute the application's SQL. | The project would still own locking, source identity, durable progress and interrupted-run reconciliation. Removing the package would move those responsibilities into scripts. |

Sources: [Flyway transaction handling](https://documentation.red-gate.com/fd/migration-transaction-handling-273973399.html),
[Liquibase runInTransaction](https://docs.liquibase.com/secure/reference-guide-5-1/changelog-attributes/runintransaction),
[Sqitch deployment](https://sqitch.org/docs/manual/sqitch-deploy/),
[Sqitch Oracle tutorial](https://sqitch.org/docs/manual/sqitchtutorial-oracle/).

**Decision:** retain migr8 for the currently specified recovery contract. Order,
checksums and a CLI alone would not justify owning a new engine. Its reason to
exist is the explicit recovery behavior and inspectable implementation; both
must survive failure tests. Revisit adoption if that recovery contract ceases to
be needed. Do not build a parallel replacement as part of these fixes.

### Composition: repair the boundary before splitting the class

`Adapter` combines session lifecycle, dialect rendering, metadata transitions,
admission and error interpretation. Its size is a reason to inspect cohesion,
not sufficient evidence that it should be split.

Keep shared transition SQL and pure state validation. First make the existing
operation boundary complete and give terminal reporting one owner. Only extract
an internal component when those changes expose a concrete responsibility with
a smaller interface. Reject a blanket Adapter decomposition, descriptor redesign,
or new executor framework as a prerequisite. None is needed to reproduce or
close R1-R5.

The CHECK and index checks should describe the fixed supported layout. A general
SQL parser would enlarge ownership while leaving the original question unchanged:
does this database object enforce the exact required relationship?

### Evidence: strengthen observation where the claim is broader than the test

The five-module 100% branch gate is useful within its stated scope. Keep it.
Its success does not show that every caller reaches a latch or policy check.
The new probes pass through real orchestration and expose precisely that gap.

Use a small operation matrix for execution, metadata, transaction probes,
finalization and diagnostics. Test failure placement and resulting durable state.
Avoid adding many tests that only assert the helper itself works. Observe the
lowest practical driver boundary for no-SQL-after-uncertainty assertions.

Retain stated trust limits: arbitrary Python, autonomous transactions and
external effects are not sandboxed. No new security boundary is proposed here.
Keep the existing database-side timeout policy unless an operational requirement
justifies a separately tested client timeout. No performance or scale conclusion
was established in this review.

## Disposition of the previous F1-F8 handover

"Closed locally" means source inspection plus the passing snapshot tests; it is
not a claim that this review reran the live database suite.

| Old finding | Current disposition |
|---|---|
| F1: swallowed latch and terminal precedence | Closed locally for the original catch/return and catch/rethrow paths. R1 concerns failures that never reach the latch. |
| F2: SQL entries and interrupted calls bypass classification | Partially fixed. SQL/Python execution now share a guard and covered DDL interruptions are unknown. R1 remains at other database boundaries. |
| F3: deferred entry points produce false SUCCESS | Closed locally. Loader checks and invocation-result validation exist; both modes and wrapped results are tested. |
| F4: wrong one-ACTIVE expression and disabled keys | Original synthetic cases are closed locally. Wider definition validation remains incomplete under R2. Live tests exist but were not run here. |
| F5: lazy helper survives same-process recovery | Closed locally. Namespace cleanup enumerates at unload; full recovery and namespace isolation tests pass. |
| F6: driver text leaks into output | Partially fixed. Execution-path tests pass; R3 identifies remaining output paths. |
| F7: PostgreSQL SQLSTATE 40003 | Closed at classifier/unit level. No natural server trigger for 40003 was exercised here. |
| F8: admitted IDs exceed staging filename limit | Closed locally. Position-based directory names and maximum-length execution tests pass. |

Do not repeat the old broad prose cleanup. Root writing instructions and the PR
checklist now exist. Correct claims implicated by R1-R5, including the statement
that every driver path is guarded and conflicting diagnostics policies. The
`SPEC.md` implementation-boundary wording still assigns every rule to the shared
adapter; align it with the actual ownership described in `ARCHITECTURE.md`.

## Verification and limits

All execution tests ran against a temporary source snapshot with the workspace's
Python environment and bytecode/cache writes disabled where practical. No source
implementation or existing tests were edited by this review.

| Check | Result |
|---|---|
| Service-free suite | **400 passed, 153 deselected** in 6.06 seconds. |
| Same suite under the existing coverage gate | **400 passed, 153 deselected**; five included modules at **100%**, 367 statements and 104 branches. |
| CI Ruff lint/format scope: `src tests testenv` | PASS; 60 files formatted. |
| Mypy configured scope: `src/migr8`, `testenv` | PASS; 31 source files. |
| Root `ruff check .` from AGENTS.md | PASS. |
| Root `ruff format --check .` from AGENTS.md | FAIL: code blocks in `docs/ARCHITECTURE.md` and `docs/MANUAL.md`, and `examples/sqlite-probe/migrations/003-backfill-region/migration.py`. CI's narrower scope passes. |
| Added review probes | R1-R5 results above reproduced; metadata comparisons use synthetic dictionary rows. |
| Live Oracle/PostgreSQL suite | **NOT RUN**. Disposable Compose project inspection returned no running containers. |
| Oracle 19c, Thick mode, performance/scale | **NOT RUN**. |
| Independent review or measured HATS improvement | **NOT RUN**. |

The first snapshot test attempt omitted the checkout launcher and failed CLI
fixtures; it was stopped. After copying the launcher and examples, the complete
runs above passed. Those initial failures were review setup errors, not product
findings.

Snapshot directory:
`/var/folders/4f/8p2nm03s2hgcyqzd37mf02_m0000gn/T/migr8-hats-review-39ydx2kt`.
Its `review-hashes.json` records each captured file. SHA-256 of that inventory:
`82b2cfe32551633b4fd1b01627ea2c354e8d9e5b234a1ad6366f72195092fff2`.
Files were compared with the worktree before report delivery. Temporary probes
are review aids; the scenarios above are the persistent reproduction contract.

## Implementation handover

1. Reproduce R1 and finish the guarded adapter boundary. Strengthen the test
   tracer before relying on it for absence of SQL after failure.
2. Close R2 with fixed-form constraint and qualified-reference comparisons.
   Preserve healthy database renderings and reject uncertain forms.
3. Close R3/R4 together around safe diagnostics and a stable terminal result.
   Keep already acknowledged migration outcomes authoritative during teardown.
4. Close R5 by ordering recovery refusal ahead of initialization side effects.
5. Run the local gates and applicable real Oracle/PostgreSQL tests. Record
   actual results, unavailable gates and evidence scope. Fix the root format
   discrepancy without weakening checks or silently changing published migration
   bytes; examples are fingerprinted input when deployed.

Keep the existing commands, modes, fingerprints and durable layout. No new
backend, automatic replay, repair, undo, async executor, ORM or generic SQL
parser is required. Preserve unrelated work already in the worktree. Report
R1-R5 dispositions with evidence and refute any finding that no longer reproduces.
Do not commit, push or publish without authorization from the current session.
