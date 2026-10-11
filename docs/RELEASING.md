# Releasing migr8

Run the commands below from the repository root.

Releases are tag-driven. A push of a tag that matches `v*` starts
[`.github/workflows/release.yml`](../.github/workflows/release.yml). The workflow
runs the whole CI workflow as its first job, the live-database suite included.
It checks that the tag is annotated and names `TOOL_VERSION` from
`src/migr8/version.py`, builds the sdist and the wheel, and exports the runtime
dependency set of the `oracle` and `postgres` extras from `uv.lock` with hashes.
The base profile has no runtime dependencies. It then smoke-tests the installed
wheel with [`scripts/release_smoke.sh`](../scripts/release_smoke.sh): without an
extra against the SQLite example, and with each extra against a live Oracle or
PostgreSQL server in a schema of its own. It publishes only the files the smoke
installed. Publication uses trusted publishing (OIDC) in the `pypi`
environment; the repository holds no PyPI token. The environment has no
required reviewer, so a tag push publishes without a further click. The GitHub
release that follows carries the sdist, the wheel,
`runtime-oracle.txt`, `runtime-postgres.txt`, `versions.txt` (the servers the
smoke ran against) and `SHA256SUMS`. The workflow's header comment lists its
jobs and records the one-time trusted-publisher setup on pypi.org.

`src/migr8/version.py` is the only version source. `pyproject.toml` reads the
package version from `TOOL_VERSION`, and every durable history row records it.

A minor bump (0.1.0 to 0.2.0) carries behaviour: anything an operator or a
migration author can observe changing. A patch bump (0.1.0 to 0.1.1) carries
documentation or a correction with no behaviour change.

The annotated tag's message is the release note. Its first line is the
summary. Its body answers, in a fixed order, what an operator checks before
upgrading; the template is under "Make a release".

## Make a release

Start from a clean working tree with `main` pushed. Run the checks in
[`AGENTS.md`](../AGENTS.md#checks-before-a-change-is-done), the live-database
suite included.

Set the new `TOOL_VERSION` in `src/migr8/version.py` and run `uv lock`.
`uv.lock` records no version for migr8, because the version is dynamic, so
`uv lock` changes it only if the dependency resolution moved; such a change is
part of the release and the tag message names it. Build locally and compare the
wheel's file list with the previous release's (`uv build`, then `unzip -Z1` on
both): the difference must be what the tag message is about to say. Commit and
push:

```sh
git commit src/migr8/version.py uv.lock -m "Set the version to X.Y.Z"
git push origin main
```

Rehearse the release on that commit before you tag it. A manual run of the
workflow from a branch runs `gates`, `build` and `smoke` and publishes nothing,
whatever its `publish` input says. Publication needs a tag: a tag push, or a
manual run from a tag with `publish` set.

```sh
gh workflow run release.yml --ref main
gh run list --workflow release.yml --limit 1
```

The tag checks in `build` run whenever the ref is a tag, on a push or a manual
run.

The smoke test also runs locally, on macOS or Linux, against a local build.
`dist/` must hold only this build's two files:

```sh
rm -rf dist
uv build --sdist && uv build --wheel
exports="$(mktemp -d)"
uv export --frozen --no-dev --no-emit-project --extra oracle -o "$exports/runtime-oracle.txt"
uv export --frozen --no-dev --no-emit-project --extra postgres -o "$exports/runtime-postgres.txt"
PYTHON="$(uv python find 3.12)" bash scripts/release_smoke.sh dist "$exports"
```

Without its connection variables, each live run prints a `SKIP:` line and the
rest of the smoke runs. To include them, start the rig with
`testenv/dbctl.sh up`, create empty smoke schemas, and set the variables before
the last command above. Each live run needs a freshly created schema: the
script fails on a namespace that is already initialized, so provision again
before every run. Provisioning drops and recreates `MIGR8_SMOKE`,
`MIGR8_SMOKE_RUNNER`, `MIGR8_SMOKE_OWNER` and the `migr8_smoke` schema, and
honours an inherited `ORACLE_DSN` or `PG_CONNINFO`, so run it in a shell that
sets neither:

```sh
ORACLE_TEST_USER=MIGR8_SMOKE .venv/bin/python testenv/provision_oracle.py
MIGR8_PG_SCHEMA=migr8_smoke .venv/bin/python testenv/provision_postgres.py
export MIGR8_SMOKE_ORACLE_DSN=localhost:15210/FREEPDB1 \
  MIGR8_SMOKE_ORACLE_USER=MIGR8_SMOKE MIGR8_SMOKE_ORACLE_PASSWORD=migr8_ora_test
export MIGR8_SMOKE_PG_DSN="host=127.0.0.1 port=15433 dbname=migr8_test" \
  MIGR8_SMOKE_PG_USER=migr8_test MIGR8_SMOKE_PG_PASSWORD=migr8_pg_test \
  MIGR8_SMOKE_PG_SCHEMA=migr8_smoke
```

Then tag that commit and push the tag. The first `-m` is the summary. The
others are the release note's body, one paragraph per question, in this order:
the metadata layout version (`layout_version` in `m8_meta`), the version of
[`docs/SPEC.md`](SPEC.md), exit codes that changed meaning, inputs that
preflight now refuses and admitted before, what the adapters now detect or
refuse that they did not before, and Python modules that joined or left the
package. The last paragraph
describes the content since the previous release, A.B.C:

```sh
git tag -a vX.Y.Z -m "X.Y.Z: one-line summary" \
  -m "Metadata layout: unchanged at 1 (or from 1 to 2)." \
  -m "SPEC: unchanged at v6.2 (or from v6.2 to v6.3)." \
  -m "Exit codes: none changed meaning (or which code now means what)." \
  -m "Preflight: refuses nothing it admitted before (or what it now refuses)." \
  -m "Adapters: detect and refuse what they did before (or what changed)." \
  -m "Modules: none joined or left the package (or which)." \
  -m "Content since A.B.C: what changed, in prose."
git push origin vX.Y.Z
```

The tag must point at the commit that carries the same version in
`src/migr8/version.py`. The `build` job refuses a tag that disagrees, and a
lightweight one.

Last, check that the `release` workflow succeeded, and read the project page at
https://pypi.org/project/migr8/ and the GitHub release page for the tag.

A local `uv build` writes into the ignored `dist/` directory. It tests the
build only. The workflow is the one publication path.

PyPI refuses a second upload of a version that exists. Do not move a tag after
a successful publish. Release the next patch version instead.
