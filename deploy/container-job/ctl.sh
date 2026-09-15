#!/usr/bin/env bash
# Build the migration-job image and run it as a container against the
# disposable test databases that testenv/dbctl.sh starts.
#
# The job joins the Compose project's network and reaches each database by its
# service name, so no address is discovered or rendered. It writes to schemas
# of its own, never the suite's, to generated/ here, and to one image.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
GEN="$HERE/generated"
# The job container is removed when it exits, so its event log has to live
# outside it. This is the host side of that mount.
RUNS="$GEN/runs"

RUNNER_IMAGE="migr8-runner:local"
# The network Compose creates for the project named in testenv/compose.yaml.
NETWORK="migr8-testenv_default"

if [[ -f "$ROOT/testenv/.env" ]]; then
  set -a; . "$ROOT/testenv/.env"; set +a
fi
# Credentials and ports are the rig's; testenv/dbctl.sh reads the same
# variables. The schemas are the job's own: the suite binds MIGR8_TEST and the
# migr8 schema to its lock id, and a job run against them would fail the
# binding check by design.
: "${ORACLE_SYS_PASSWORD:=migr8_sys_test}"
: "${ORACLE_HOST_PORT:=15210}"
: "${ORACLE_JOB_USER:=MIGR8_JOB}"
: "${ORACLE_JOB_PASSWORD:=migr8_ora_test}"
: "${POSTGRES_USER:=migr8_test}"
: "${POSTGRES_PASSWORD:=migr8_pg_test}"
: "${POSTGRES_DB:=migr8_test}"
: "${POSTGRES_HOST_PORT:=15433}"
: "${PG_JOB_SCHEMA:=migr8_job}"
export ORACLE_SYS_PASSWORD POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB
# Set, not defaulted, for the reason testenv/dbctl.sh gives: the provisioning
# scripts honour an inherited endpoint, and this script provisions the
# databases the job will run against.
ORACLE_DSN="localhost:${ORACLE_HOST_PORT}/FREEPDB1"
PG_CONNINFO="host=127.0.0.1 port=${POSTGRES_HOST_PORT} dbname=${POSTGRES_DB} user=${POSTGRES_USER} password=${POSTGRES_PASSWORD}"
export ORACLE_DSN PG_CONNINFO

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "==> $*"; }

require_engine() {
  command -v docker >/dev/null || die "docker CLI not found"
  docker info >/dev/null 2>&1 \
    || die "no engine answers on docker context '$(docker context show)'; see testenv/README.md"
  docker network inspect "$NETWORK" >/dev/null 2>&1 \
    || die "network $NETWORK does not exist; start the databases with testenv/dbctl.sh up"
}

# --load is a no-op with dockerd's own builder. Against podman the docker CLI
# builds in a BuildKit container it creates (`docker buildx ls` shows it), and
# without --load the result stays in that container's cache, so `docker run`
# finds no image and tries to pull one.
cmd_build() {
  require_engine
  note "building $RUNNER_IMAGE"
  docker build --load --tag "$RUNNER_IMAGE" --file "$HERE/Containerfile" "$ROOT"
}

# The job's schemas, created by the rig's provisioning scripts from the host
# through the published ports, the way dbctl.sh creates the suite's.
cmd_provision() {
  note "provisioning the job's Oracle schema $ORACLE_JOB_USER"
  ORACLE_TEST_USER="$ORACLE_JOB_USER" ORACLE_TEST_PASSWORD="$ORACLE_JOB_PASSWORD" \
    "$PY" "$ROOT/testenv/provision_oracle.py"
  note "provisioning the job's PostgreSQL schema $PG_JOB_SCHEMA"
  MIGR8_PG_SCHEMA="$PG_JOB_SCHEMA" "$PY" "$ROOT/testenv/provision_postgres.py"
}

render() {
  mkdir -p "$GEN"
  sed -e "s|@ORACLE_DSN@|oracle:1521/FREEPDB1|g" \
      -e "s|@ORACLE_USER@|${ORACLE_JOB_USER}|g" \
      "$HERE/templates/oracle.toml.in" > "$GEN/oracle.toml"
  sed -e "s|@PG_CONNINFO@|host=postgres port=5432 dbname=${POSTGRES_DB} user=${POSTGRES_USER}|g" \
      -e "s|@PG_USER@|${POSTGRES_USER}|g" \
      -e "s|@PG_SCHEMA@|${PG_JOB_SCHEMA}|g" \
      "$HERE/templates/postgres.toml.in" > "$GEN/postgres.toml"
}

# Run the migration job. Usage: run_job <engine> <migr8 args...>
run_job() {
  local engine="$1"; shift
  require_engine
  render
  local password manifest
  case "$engine" in
    oracle)   password="$ORACLE_JOB_PASSWORD"; manifest="/opt/migr8/examples/oracle/manifest.toml" ;;
    postgres) password="$POSTGRES_PASSWORD";   manifest="/opt/migr8/examples/postgres/manifest.toml" ;;
    *) die "unknown engine '$engine'; use oracle or postgres" ;;
  esac
  mkdir -p "$RUNS"
  # The job runs as uid 10001 and this directory belongs to whoever ran this
  # script, so the mount is opened to both. A deployment mounts a directory its
  # own runner owns instead; this is a disposable local rig.
  chmod 0777 "$RUNS"
  local rc=0
  # The job's exit code is the command's result, so it is kept rather than
  # replaced by the note that follows it.
  docker run --rm \
    --network "$NETWORK" \
    --volume "$GEN:/etc/migr8:ro" \
    --volume "$RUNS:/var/log/migr8" \
    --env "MIGR8_PASSWORD=$password" \
    "$RUNNER_IMAGE" \
    "$@" --config "/etc/migr8/$engine.toml" --manifest "$manifest" || rc=$?
  note "event log: $RUNS/run.jsonl"
  return $rc
}

cmd_info() {
  cat <<INFO

Oracle     : oracle:1521/FREEPDB1 on $NETWORK   user=$ORACLE_JOB_USER
PostgreSQL : postgres:5432 on $NETWORK          db=$POSTGRES_DB user=$POSTGRES_USER schema=$PG_JOB_SCHEMA
Generated configs in: $GEN

Run the migration job:
  $0 migrate oracle
  $0 status postgres
INFO
}

case "${1:-}" in
  build)     cmd_build ;;
  provision) cmd_provision ;;
  info)      cmd_info ;;
  migrate)   shift; run_job "${1:?engine required}" migrate "${@:2}" ;;
  status)    shift; run_job "${1:?engine required}" status "${@:2}" ;;
  validate)  shift; run_job "${1:?engine required}" validate "${@:2}" ;;
  runlog)    [[ -s "$RUNS/run.jsonl" ]] || die "no job event log at $RUNS/run.jsonl yet"
             cat "$RUNS/run.jsonl" ;;
  clean)     rm -rf "$GEN"; echo "removed the generated configs and the job's event log" ;;
  *)
    cat <<USAGE
usage: ctl.sh <command>

  build                  build the migration-job image
  provision              create the job's own schemas in the running test databases
  info                   show what the job connects to and where its configs are
  migrate  <engine> [..] run 'migr8 migrate' as a container job
  status   <engine> [..] run 'migr8 status'
  validate <engine> [..] run 'migr8 validate'
  runlog                 show the job's event log, kept on the host
  clean                  remove the generated configs and the event log

engines: oracle | postgres
The databases come from 'testenv/dbctl.sh up'; the image goes with
'docker image rm migr8-runner:local'.
USAGE
    exit 1 ;;
esac
