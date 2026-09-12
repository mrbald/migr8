# Codex CLI handover — flyway.py MVP

> Kept in the repository so the project does not depend on continued access to
> the original attachment directory, which is no longer readable from this
> workspace. The text below is the handover as received.
>
> The tool was subsequently renamed from `flyway.py` / `flywaypy` to
> **migr8**. The handover text below is left at its original wording; read
> every `flyway.py` in it as `migr8`.

Implement the experimental `flyway.py` MVP described in the accompanying **flyway-spec-v6.md**. Develop the implementation, tests, examples, and specification side by side. This is authorization to begin implementation in the current CLI workspace; do not stop after producing another design review.

The specification is the complete baseline. Earlier v3–v5 drafts and the originating conversation are not required.

## Inputs

Original handover files on this machine:

- `/Users/bobah/Documents/Codex/2026-09-10/be/outputs/flyway-spec-v6.md`
- `/Users/bobah/Documents/Codex/2026-09-10/be/outputs/codex-handover.md`

If the files have been copied into this workspace, use the local copies. Keep the maintained specification in a conventional project location such as `docs/SPEC.md` and update it with implementation changes. Do not require continued access to the original attachment directories.

Use the current CLI working directory as the project location. Read applicable `AGENTS.md` instructions and inspect existing files and Git state first. Integrate with an existing repository if present, without overwriting unrelated work. If the directory is empty, scaffold a small Python project there. Ask only when the destination is genuinely ambiguous or an action requires additional authorization.

## Objective and scope

Build a small, inspectable migration engine with:

- An ordered TOML manifest, permanent identities, immutable successful history, and at most one active restartable migration.
- Atomic and restartable SQL/Python execution.
- Fingerprinted, staged units; static fingerprinted required-object declarations; read-only final validity checking.
- `migrate`, `validate`, and `status`, plus explicit `migrate --recover ID` for amended active source.
- A progress store whose writes commit with their batch.
- Oracle as the primary target, PostgreSQL as the second adapter/probe, and an explicitly limited SQLite local probe.

Preserve the spec's small scope. Do not add undo, history repair, abandonment, baselining, SQL*Plus interpretation, automatic recompilation, automatic replay, an ORM, or plugin infrastructure.

## Work sequence

1. Inspect the workspace and available Python/package/container runtimes. Explain the immediate implementation steps briefly, then proceed. Do not turn this into another open-ended planning round.
2. Establish a small package/CLI, deterministic manifest/fingerprint/staging tests, and a pure state validator. Use `uv` if it suits the workspace; pin dependencies in the project's lockfile.
3. Build a complete vertical slice through SQLite probe execution and CLI inspection. Keep its local filesystem-lock exception explicit; do not emulate Oracle DDL behavior or describe SQLite as production certification.
4. Bring up isolated, disposable Oracle and PostgreSQL test services if the current environment permits. Implement and exercise the Oracle adapter early so SQLite abstractions do not dictate Oracle semantics. Add the PostgreSQL adapter using its own transactional DDL and session advisory-lock behavior.
5. Exercise atomic DML, restartable DDL, interrupted batches, source recovery, required-object failures, metadata damage, and concurrent runners against real databases. Implement the directional commit-failure harness at actual engine boundaries; distinguish real transport evidence from wrapper simulations.
6. Keep the spec and examples synchronized with the code. Resolve small ambiguities using the simplest approach consistent with the invariants and tests. Document substantive changes. Escalate decisions that would weaken history integrity, recovery boundaries, or the explicit scope instead of silently redesigning them.
7. Produce a concise implementation and acceptance report with exact commands, tested versions, capabilities, failures, skipped gates, and remaining risks. Keep NOT RUN distinct from PASS. Do not label the MVP production-ready merely because fast tests pass.

## Database setup approach

Use an isolated Compose project or an equivalent disposable local setup. Bind ports to loopback, use dedicated test credentials, and scope all cleanup to this project's resources. Do not connect to an existing production or shared application database as a fallback.

- **Oracle:** use a currently available ARM64-compatible `gvenzl/oracle-free` image where applicable, pin the resolved tag/digest, and record the actual server release. Create a disposable user/schema with the needed grants and `DBMS_LOCK` access or a test wrapper. Test separate connect-user/target-schema behavior in a controlled fixture. Free-image results do not establish 19c compatibility; a real recorded 19.x run is required before claiming it.
- **PostgreSQL:** use the official image, pin a supported release and digest, and use a separate database/schema, credentials, port, and storage. Follow that image version's documented volume layout.
- **SQLite:** use the Python standard library with fresh local files. No service is needed. Keep probe locking and supported filesystem/platform assumptions explicit.

Provide practical commands or scripts to start, health-check, test, and stop these test services. Check Docker/runtime availability and resources before large downloads. Respect sandbox/network/filesystem permission requirements. If a service cannot run, state the exact blocker, continue independent work, and leave that acceptance gate visibly incomplete.

At handover preparation, the local machine reported ARM64, Python 3.14.7, `uv`, a Docker CLI, and SQLite CLI. The Docker daemon, available resources, image availability, and database connectivity were **not verified**. No database services were started, no images were pulled, and no implementation was created in the source conversation. Recheck the actual CLI environment; these observations are not prerequisites or proof of readiness.

## Critical contracts to carry into the code

- Acknowledge ACTIVE admission before importing or invoking restartable code.
- Hold the same physical Oracle/PostgreSQL session's migration lock through every commit. Never reconnect and continue an attempt.
- Establish atomic transaction identity before executing migration code. Report detected violations distinctly from a known clean rollback.
- Treat lost acknowledgements and uncertain commit-capable calls conservatively. Latch the run unusable so author code catching the exception cannot resume it. Do not issue cleanup SQL after an unknown outcome.
- Enforce same identity, position, and restartable mode during recovery. Update permitted language/fingerprint metadata together. Never admit a changed successful migration.
- Include `require_valid` in the canonical fingerprint. The static required set applies on every attempt; missing or wrong-type objects fail. Completion performs no automatic compilation.
- Distinguish incomplete initialization from later metadata damage. Never recreate missing successful history after the initialized marker exists.
- The documented Python backfill must run through the actual facade. Oracle output binds are not query result sets. Select a bounded key set, update those exact keys, then checkpoint them in one transaction.
- No line-prefix SQL*Plus detector or semicolon splitter. Preserve database-specific literals and procedural blocks. Keep one executable statement/block per SQL file initially.
- Keep history inspection read-only and useful during a running migration. Diagnostics must not pretend to know more about session liveness than their evidence supports.

## Delivery expectations

Deliver source, tests, a maintained full specification, runnable examples, dependency/configuration files, repeatable disposable database setup, and an honest acceptance report. A small module layout is preferable to forcing everything into one file.

Use focused tests that reach the actual failure boundary. Run the relevant checks once changes are stable; repeat or broaden them when failures or new changes justify it. Do not count skips as coverage of unavailable database behavior.

Keep changes local unless separately authorized to commit, push, publish, or deploy. Do not alter unrelated services, global configuration, credentials, or existing volumes. Avoid adding features to resolve a narrow discrepancy when a local correction suffices.

Give concise progress updates. When a self-contained milestone has landed and the next step does not require the current conversation history, flag `good time to /clear`. If ongoing work needs compaction, identify the few decisions, paths, and in-flight tasks that must survive; do not suggest a reset while work is still being verified.
