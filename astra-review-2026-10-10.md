# migr8 design and implementation review — 2026-10-10

**Verdict: fix the transaction and admission defects before release.** A live PostgreSQL reproduction records SUCCESS after losing the entire batch. The existing suite passes but does not cover this case.

Reviewed revision: `06b3a36179fa7657528c1884e3d76ce6daa1c910`. Scope: protocol/design documents, core execution and validation, all three adapters, CLI/reporting, tests and test orchestration. This is a bounded source review with targeted reproductions, not exhaustive verification of every database behavior. No implementation files changed. The pre-existing staged deletion of `astra-4-opus.md` was left untouched.

## Findings

### F1 — P1: PostgreSQL can report a rolled-back batch as successful

**Evidence:** `src/migr8/adapters/postgres.py:337-338`, `src/migr8/context.py:276-289,315-325`.

`_do_commit()` discards the result of `COMMIT`. If migration code catches a statement error inside `ctx.transaction()`, PostgreSQL leaves the transaction aborted; the subsequent COMMIT rolls it back without raising. The context exits successfully and the engine completes the migration.

Live reproduction on PostgreSQL 17.5:

```python
with ctx.transaction():
    ctx.execute("INSERT INTO t VALUES (1)")
    ctx.progress.set("last", "1")
    try:
        ctx.execute("INSERT INTO t VALUES (1)")  # duplicate primary key
    except Exception:
        pass
```

Observed: exit **0**, history contains `batch/SUCCESS`, and `SELECT * FROM t` returns **no rows**. This loses previously successful statements in the batch, not just the statement whose error the author caught.

**Fix:** reject an aborted transaction before commit and verify that the command actually committed. Preserve the existing unknown-outcome handling for a lost reply. A failed batch must surface as a failure to its caller.

**Acceptance:** reproduce this exact case against PostgreSQL; expect a nonzero migration result and ACTIVE history when the batch failure propagates. Verify successful batch data/checkpoint coupling and unknown-commit behavior still pass.

### F2 — P1: restartable SQL blocks bypass reserved-metadata admission

**Evidence:** `src/migr8/checks.py:39-46`, `src/migr8/engine.py:534-540`; the missing protection is `src/migr8/adapters/base.py:1000-1020,1061-1075`.

Both preflight and execution skip `admit_statement()` for restartable PL/SQL entry files. A file containing `BEGIN DELETE FROM m8_history; COMMIT; END;` passes preflight. The identical statement through the Python facade raises `UsageError`. The SQL execution branch submits it directly, allowing an accidental metadata reference to bypass the guard and potentially erase history before completion fails.

**Evidence limit:** reproduced the admission difference without executing the destructive statement. The execution consequence follows from the direct submission path.

**Fix:** run restartable blocks through `admit_statement(..., mode=RESTARTABLE, in_batch=False)` in both preflight and execution. Share the SQL-entry admission decision so these branches cannot drift again.

**Acceptance:** every reserved metadata name is rejected before connection/admission for SQL blocks, just as for Python facade calls. Ordinary restartable blocks remain permitted. This is an author-error guard, not a sandbox against trusted code.

### F3 — P1: restartable batches lack the transaction-identity check

**Evidence:** `src/migr8/context.py:258-289`; compare atomic handling at `src/migr8/engine.py:351-365,402-417`. The batch contract is in `docs/SPEC.md:211`.

Batch entry begins a transaction but never establishes its identity; clean exit commits without checking it. On Oracle, an admitted PL/SQL call can commit inside a batch, separating data from later progress updates without triggering the atomic-mode tripwire.

Live Oracle 23ai reproduction: inside `ctx.transaction()`, execute a block containing an INSERT followed by COMMIT. Observed: **clean batch exit, no latched violation, durable row**. This probe exercised the real context and adapter; it did not run a complete migration or alter metadata.

**Fix:** share transaction-identity establishment/verification between atomic migrations and restartable batches. Detect a changed or missing identity before acknowledging a batch, latch exit 8, and preserve the warning that already committed effects need remediation. Keep each adapter's documented detection limits.

**Acceptance:** Oracle hidden commit and commit-then-new-transaction cases fail with exit 8 and cannot be swallowed into SUCCESS. Verify normal and empty batches, progress-first batches, and lost identity-probe replies.

### F4 — P2: Oracle validity checks swallow communication failures

**Evidence:** `src/migr8/adapters/oracle.py:1107-1118,1158-1175`; outer guard at `src/migr8/adapters/base.py:1121-1133`.

The validity query catches every `oracledb.Error` and returns an ordinary validity failure. Compiler-message lookup similarly turns every driver error into a privilege-related fallback. These catches prevent the outer operation guard from classifying connection loss.

A driver-shaped ORA-03113 injected into `check_required_objects()` returned one validity failure and left the latch unset. The engine can consequently report ordinary failure and attempt cleanup SQL after a communication failure; on the warning-text path it can proceed toward completion.

**Fix:** allow communication failures to reach the operation guard. Convert only definite server rejections into the documented inspection fallback.

**Acceptance:** inject transport faults separately into the main validity query and compiler-message lookup. Require exit 4, connection discard, and no later SQL. Keep privilege-denial diagnostics distinct. The current reproduction is fault injection, not a live network-failure test.

### F5 — P2: PostgreSQL connection construction corrupts passwords and ignores user

**Evidence:** `src/migr8/adapters/postgres.py:194-202`; duplicated user configuration at `examples/postgres/migr8.toml:5-6`.

The adapter appends `password=<raw value>` to the DSN and never passes `Config.user`. Using the installed psycopg parser, a password containing a space failed parsing, a URI DSN plus an environment password failed parsing, and the synthetic password `x host=other.invalid` changed the parsed host. `database.user` was absent from the connection arguments.

**Fix:** pass password and configured user as keyword arguments to `psycopg.connect`; define their precedence over DSN values. Psycopg explicitly supports keyword overrides: [connection API](https://www.psycopg.org/psycopg3/docs/api/connections.html). Remove the duplicated user from the example once the contract is fixed.

**Acceptance:** cover spaces, quotes, backslashes, URI and keyword DSNs, explicit user, and conflicting DSN values. Assert exact connection parameters without logging credentials. These reproductions used synthetic credentials and intercepted connection construction.

### F6 — P2: status becomes unavailable when a pending source has a syntax error

**Evidence:** `src/migr8/readonly.py:245-253`; reporting contract at `docs/SPEC.md:534`.

Full preflight runs before connecting or constructing a history report. After applying two SQLite migrations, adding a pending Python unit with invalid syntax made `status --json` return only the preflight error: no successful history, pending list, or namespace state. An unrelated pending edit therefore blocks the command operators need for diagnosis.

**Fix:** preserve safe database inspection when a captured plan has an admission/compilation error. Attach the source problem to the status report and retain its nonzero exit code. Do not execute or import migration code. Separately consider a database-only status mode for missing artifacts; that is a product decision, not necessary for the narrow fix.

**Acceptance:** the reproduced pending syntax error still exits 2 but includes both successful rows and the invalid pending entry. Test an ACTIVE migration followed by an invalid pending unit too.

## Architectural and technical simplification

1. **Share transaction-scope enforcement.** Extract the identity/latch/commit checks needed by both atomic execution and batch contexts. Keep atomic history insertion and restartable progress semantics explicit. F1 and F3 show the cost of the current split.
2. **Share SQL-entry admission.** Preflight and `_invoke_sql()` repeat the mode/block/DDL decision. One admission helper should return the permitted execution category; execution can then dispatch without reimplementing policy. F2 is a concrete divergence to eliminate.
3. **Consolidate result plumbing, without merging the report payloads.** `Engine.RunReport`, `reporting.Report`, and `cli._render_failure()` have different terminal envelopes. Share exit/outcome/run-id fields and recovery-command construction. Keep migration outcomes and history listings as distinct payloads. This also reduces duplicate failure formatting.

Keep the pure state validator, explicit adapter registry, mode-specific contexts, and shared metadata transitions. There is no demonstrated need to split packages, add plugin discovery, or replace dialect-specific catalog queries with a generic query framework. Collapsing the five adapter-description methods would offer less value than the fixes above.

## Operator perspective

- **Recovery commands lose the selected target.** `engine.py:178` and `readonly.py:260` emit only `migr8 migrate --recover ID`, dropping explicit config and manifest paths. Generate the command at the CLI boundary with shell-quoted resolved paths. Acceptance: a command copied from a run using nondefault paths selects that same configuration and manifest, including paths containing spaces.
- **PostgreSQL setup needs a complete manual example.** The manual centers Oracle and SQLite; PostgreSQL currently requires discovering the example directory and understanding the duplicated user fields. Add one working configuration and credential example after F5 is fixed.
- **Keep destructive convenience commands out.** Undo, forced unlock, automatic history repair, and adoption of an existing schema are separate protocols with substantial recovery consequences. This review found no requirement justifying their addition. Improve inspection and exact recovery guidance first.

## Verification and limits

All results below were obtained on 2026-10-10 from this checkout, using its `.venv` and Python 3.14.0.

| Gate | Result |
|---|---|
| `ruff check .` | PASS |
| `ruff format --check .` | PASS; 79 files |
| `mypy src` | PASS; 28 source files |
| Additional `mypy testenv` | PASS; 3 files |
| `pytest -m 'not oracle and not postgres'` | 616 passed, 1 skipped, 162 deselected |
| `testenv/dbctl.sh test -q` | 776 passed, 3 skipped; exit 0 |
| Oracle TLS/wallet cases | NOT RUN; 2 skipped, fixture unavailable |
| SQLite bounded-filesystem ENOSPC case | NOT RUN; 1 skipped, fixture unavailable |
| Fresh wheel installation, Linux/Python 3.12, container deployment, coverage percentage | NOT RUN in this review |

The first sandboxed live-suite attempt could not connect and correctly failed the gate. The subsequent approved run reached the existing local disposable services. Docker context was `podman`; inspected containers were `migr8-oracle` on loopback port 15210 and `migr8-postgres` on loopback port 15433. Targeted live probes identified Oracle 23.9.0.25.07 (Thin) and PostgreSQL 17.5. No containers were provisioned or restarted. The PostgreSQL probe used and removed its own schema; the Oracle probe used and removed one uniquely named application table.

**Implementation order:** F1 and F3 together with shared transaction checks; F2 with shared admission; F4; F5; F6 and recovery/reporting improvements. Add the targeted regressions before rerunning the existing gates. Passing those gates alone does not invalidate the independently reproduced defects.
