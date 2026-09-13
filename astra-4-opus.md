# Pre-release review: Linux installation, plan linting and production SQLite

Reviewed 2026-09-13 against the current working tree at `bc5d2d500751c0d9cefb93883279303b2de1a40f`, including Opus's uncommitted changes. This replaces the previous review.

**Verdict: REWORK before the first production release. Keep the engine architecture and existing package extras. Finish production SQLite, minimal Linux installation, offline plan linting, and the initial-install/routine-migration runbooks.**

The user has selected Linux, installation into a venv through a PyPI proxy, and the minimum runtime dependencies for each database. Production SQLite is an explicit first-release requirement and should also be the primary fast integration backend for shared engine behavior. Initial setup here means creating application schema objects in an already provisioned database; database-server installation remains a platform prerequisite. Record the actual Linux architecture and Oracle release before qualifying that profile.

| Area | Current ruling | Required next step |
|---|---|---|
| Previous R1–R5 engine findings | Fixes and regression coverage have advanced | Retain the fixes; no broad architecture rewrite |
| Minimal per-database installation | Package structure already supports it | Consumer install guide and clean Linux artifact tests |
| Production SQLite | Not ready to relabel yet | Close the concrete gaps in R1 and qualify the supported profile |
| Migration-plan CI lint | Incomplete | Offline validation plus Python syntax preflight |
| Initial setup and routine migrations | Operational guidance exists; procedure incomplete | Document and rehearse the actual operator paths |
| Release workflow | Tests source, not installed runtime profiles | Smoke-test the exact wheel before publishing |

**R1 — P1: promote SQLite to a supported production adapter through a bounded hardening pass.**

The direction is appropriate. SQLite already provides real files, transactions, process contention and recovery fixtures, with no external driver dependency. It is well suited to exercising shared manifest, staging, state, progress, CLI and outcome-handling logic. Its production promotion does not require a new architecture.

However, the current adapter is explicitly a probe (`docs/SPEC.md:669–681`; `docs/MANUAL.md:480–481`). I found a concrete correctness gap and additional qualification work, so promotion should include more than a rename.

1. **Metadata inspection accepts a broken one-ACTIVE constraint.** After a successful migration, I replaced the one-ACTIVE index with:
   `CREATE UNIQUE INDEX <same-index-name> ON m8_history(migration_id) WHERE status = 'ACTIVE'`.
   Both `validate` and `migrate` returned **0**. This index permits multiple ACTIVE identities. The inspector checks substrings but not the indexed columns (`src/migr8/adapters/sqlite_probe.py:357–380`). It also checks table column names/types/nullability without verifying the full PK/UNIQUE/FK/CHECK contract. Bring SQLite metadata inspection up to the guarantees now checked on Oracle/PostgreSQL: inspect index keys and predicates, keys and FK targets, and the known CHECK definitions; fail closed on unsupported definitions. Add mutation regressions that reach each guard. This is a verified acceptance gap, not a claim that the engine spontaneously creates multiple ACTIVE rows.

2. **Make commit durability explicit and verify it.** Connection setup enables foreign keys; storage preparation requests a journal mode without reading its result. Neither establishes or verifies `synchronous` (`sqlite_probe.py:262–286`). The reviewed host defaults to FULL; I did not observe default unsafe settings. A controlled probe setting synchronous OFF before preparation confirmed that preparation leaves it OFF. Select and read back a supported durability policy, rather than relying on build defaults. SQLite documents FULL for durable WAL commits and EXTRA for stronger DELETE-journal durability across power loss; its [synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous) explains the distinction. Verify foreign-key enforcement and the effective journal mode too. Keep read-only commands free of persistent journal changes.

3. **Qualify the actual runtime modes.** The adapter defaults to WAL, but the common test fixture selects DELETE (`sqlite_probe.py:218–225`; `tests/support.py:62–68`). Run the meaningful transaction, checkpoint, metadata-damage and process-recovery cases in both supported modes, or deliberately narrow the first production profile. Reuse existing tests rather than duplicating the whole suite.

4. **Exercise failure boundaries with real processes and controlled faults.** Cover process death around initialization, atomic work/SUCCESS, restartable admission, batch data/progress commits and final SUCCESS; rerun and assert exact durable state. Include application-reader/writer contention, busy timeouts, disk-full/write failures and unusable paths. Audit the blanket classification of every `sqlite3.Error` as definite (`sqlite_probe.py:473–484`) against actual commit/storage failures. SQLite distinguishes busy commits from errors that can roll back an entire transaction; [transaction error semantics](https://www.sqlite.org/lang_transaction.html) are the basis for those expectations. This review did not prove the existing classifier wrong in every such case. Do not equate killing a process with testing power failure.

5. **Define the small production support profile and its lifecycle.** Start with Linux, local file-backed storage, cooperating runners using the same canonical path, and the existing process-held lock. Preserve exclusions for network filesystems and unsupported aliasing/locking arrangements. Test lock release after death, concurrent application access, and file/directory permissions. Document backup and restore, including path bindings and WAL handling; rehearse a consistent backup through the [SQLite backup API](https://www.sqlite.org/backup.html) or a documented quiesced procedure.

Decide the public adapter name before the release. If introducing `sqlite`, handle existing `sqlite-probe` configuration and persisted adapter identity explicitly: that identity is part of namespace binding. Update the spec, capability report, examples and support table together.

**Acceptance:** installed base wheel on supported Linux; recorded Python and SQLite library versions; explicit verified durability settings; metadata mutations refused; process interruption/restart invariants pass in each supported journal mode; backup/restore and ordinary application contention rehearsed. Keep SQLite as the fast core integration gate. Oracle DDL commits, server session locks and transport failures still require their own database tests.

**R2 — P1: publish the minimal Linux installation procedure.**

`docs/MANUAL.md:18–28` currently offers `uv sync --all-extras` or installation from `.[oracle,postgres]`. That is checkout-oriented and installs more than the requested runtime profile.

The dependency separation already exists: `pyproject.toml:31–35` declares an empty base dependency list and separate Oracle/PostgreSQL extras. `src/migr8/adapters/__init__.py:17–38` imports the selected adapter lazily. SQLite uses the standard library.

Use one wheel:

- Oracle: `pip install 'migr8[oracle]==<approved-version>'`.
- SQLite: `pip install 'migr8==<approved-version>'`.

These describe the intended release installation; they do not assert that an approved version is already on the proxy. Extras select dependencies; the wheel still contains the small source modules for all adapters. Separate distributions would add version coordination without solving a demonstrated dependency problem.

The operator guide should specify Python 3.14, venv creation and ownership, proxy configuration, exact runtime requirements, configuration/secrets, writable temporary space, persistent logs, and an installation self-check. Require no checkout, uv, compiler, test framework, linter, or unused database driver. Prefer wheels; document failure if the proxy lacks a wheel for the supported architecture.

Publish a release-specific runtime dependency set with transitive versions/hashes. Pinning migr8 alone does not freeze the open-ended driver requirements. Oracle driver transitives are runtime dependencies, not removable development baggage. Keep any application-specific Python migration dependencies explicit and owned by the plan.

Include DBA prerequisites separately: identity, target schema, quota, metadata/application privileges, lock-package access and lock-id ownership. Do not promote disposable provisioning helpers into production installers; `testenv/README.md:8` expressly excludes shared and production targets.

**Verified:** the built base wheel installs into a clean host venv containing only migr8 and pip and runs the SQLite example through migration and validation. A separate Oracle-only venv installs and imports in Thin mode without psycopg or developer tools. This validates the package separation on the review host, not installation on Linux through the user's proxy.

**R3 — P1: provide a defined offline migration-plan lint contract.**

Current `validate` checks source against database history (`docs/MANUAL.md:220–227`). Its execution path performs preflight and then connects (`src/migr8/readonly.py:119–142`). It is useful for target validation but cannot currently serve as database-free plan CI.

It also omits Python syntax checking. `src/migr8/checks.py:18–37` applies lexical admission to SQL only.

**Reproduced:** apply a valid first migration, append a restartable Python migration with `def migrate(ctx)` missing its colon, then validate. Validation returns **0**, with one pending migration. Execution returns **3** and leaves the broken migration **ACTIVE**. The error is detectable before any migration state is written.

Add a proposed `migr8 validate --offline` mode using existing manifest loading, capture/fingerprinting and backend admission. Specify:

- No database connection or secret requirement; explicit backend selection.
- Manifest/reference/identity/order/path checks, supported mode/language combinations, required-object declarations and existing SQL lexical admission.
- Parse/compile every Python source in captured units without importing/executing it or writing bytecode. Reuse this in migration preflight so syntax errors fail before initialization/execution.
- Stable JSON diagnostics, nonzero failure status, and an explicit account of checks performed.

This extends the current read-only contract, which explicitly says no compilation (`docs/SPEC.md:710`). Update that wording deliberately to distinguish static source compilation from database-object compilation and migration execution; preserve no-import/no-execution guarantees.

A single manifest cannot reveal that a published migration was edited. Define a CI comparison against the previously approved plan artifact for published identity/order/fingerprint immutability. Keep explicitly amended ACTIVE recovery a separate operator action.

Document three gates: **offline lint**, **actual-plan rehearsal** on a disposable supported database from relevant starting states, then **online validation** against the target using the same approved artifact. Define first-install handling: an uninitialized namespace returns exit 6. Never blanket-ignore validation failures. The engine's check under its namespace lock remains authoritative.

Static lint does not prove database SQL validity, privileges, data-dependent correctness or restartable convergence. Avoid a SQL-parser project or an emulator to imply otherwise.

**R4 — P1 before production use: finish and rehearse the operator procedures.**

`docs/MANUAL.md:412–433` gives operational notes, not a complete first-install/day-to-day procedure. `docs/ACCEPTANCE.md:284–294` already names actual-target testing and metadata restoration policy as open production work.

Provide three paths with commands, expected reports/exits, postconditions and responsible operator:

- **Initial schema creation:** approved starting state, installed runtime, database prerequisites, configuration review, initialization, migration and application checks.
- **Routine migration:** immutable published prefix, reviewed artifact, target validation, one supervised job, retained logs/report/exit status, contention policy and postconditions. Specify any application compatibility/maintenance-window requirement.
- **Failure/interruption:** ordinary rerun versus explicit recovery, damaged metadata and unknown outcomes; evidence collection and escalation owner; representative interrupted restartable migration rehearsal.

Rehearse backup/restore keeping application state and migr8 metadata consistent. Tool-version rollback does not undo database changes. Existing-schema adoption also needs an explicit starting-state decision: the tool deliberately offers no baseline/history repair (`docs/MANUAL.md:483–488`).

Record the actual database version/update, Linux architecture, Python/driver/library versions and representative migration volume/duration. Oracle 19c, Thick mode and alternative authentication remain unqualified (`docs/ACCEPTANCE.md:242–244`); qualify the selected profile or constrain the release claim. There is no need to qualify every optional profile.

**R5 — P2 before the release tag: test the installed artifact in release automation.**

The release workflow tests source, builds distributions and runs Twine checks (`.github/workflows/release.yml:44–67`). It does not exercise the exact installed wheel outside the checkout.

Add clean Linux runtime smoke jobs for base/SQLite and Oracle-only installations using the release dependency set. Verify dependency inventories, CLI behavior and a minimal live adapter scenario without source-tree imports. Publish the same artifacts that passed. Manual wheel checks in this review passed; this is a missing automated gate, not a broken-package finding.

Align release lint/format scope too: release checks `src tests testenv`, while main CI now checks the repository root (`.github/workflows/ci.yml:27–30`).

**R6 — P2, conditional follow-up: the Apple-container reference loses its event log.**

The log defaults to `/var/log/migr8/run.jsonl` (`deploy/apple-container/Containerfile:35–36`), but the job uses `--rm` and mounts only configuration (`deploy/apple-container/ctl.sh:152–166`). The event log disappears with the container. Mount/export it if retaining this deployment reference. Its README also says the current scripts have not been rerun (`deploy/apple-container/README.md:10–12`).

This does not block the selected Linux venv route. Keep the example's evidence status accurate; do not spend the next iteration qualifying an unrelated deployment path.

**Verification and limits**

- Previous R1–R5 implementation changes and their regression coverage were inspected. Relevant coverage includes metadata CHECK/FK damage, metadata/batch-entry outcome authority, failure reporting, terminal-result handling and recovery admission. See `tests/test_metadata_damage.py:210–280`, `tests/test_metadata_damage.py:428–478`, `tests/test_outcome_authority.py:431–549`, `tests/test_failure_reporting.py`, `tests/test_terminal_result.py` and `src/migr8/engine.py:255–277`.
- Isolated source snapshot: **436 service-free tests passed**; full suite with the disposable Oracle and PostgreSQL services: **593 passed, no skips**. Ruff, formatting and configured mypy checks passed. The coverage threshold was not rerun in this review.
- Built sdist and wheel. Clean base installation completed SQLite migration/validation. Oracle-only installation/import succeeded, resolving oracledb 26.0.0, matching the full-suite environment. The installed Oracle-only venv was not itself exercised against a live database; the full database suite used the source snapshot.
- Reproduced the malformed-Python validation success, incorrectly keyed SQLite index acceptance, and lack of synchronous-setting enforcement. The synchronous-OFF probe was controlled injection, not the normal host setting.
- Reviewed files matched the tested snapshot before report preparation. This review changes only this report.
- Linux installation through the user's proxy, production SQLite qualification, the actual Oracle target, the real migration plan and restore rehearsal remain outstanding.

**Recommended sequence:** implement R1–R3 as bounded changes, add R5, then rehearse R4 using a release candidate. Refresh acceptance counts and resolved-review status when that work lands. First-release approval should name the supported Oracle and SQLite profiles and link their installation, migration and recovery evidence.
