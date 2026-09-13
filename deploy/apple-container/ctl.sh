#!/usr/bin/env bash
# Run the disposable test databases and the migration job under Apple's
# `container` runtime on macOS.
#
# Everything it touches is named with the migr8-ac- prefix. It never modifies
# unrelated containers, images, volumes or networks, and it never connects to
# anything but the containers it started.
#
# Addressing note: containers are reached by their address on the container
# network, read back from `container inspect`. Name-based addressing needs
# `sudo container system dns create <domain>`, which this script will not do for
# you. See README.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
GEN="$HERE/generated"
# The job container is removed when it exits, so its event log has to live
# outside it. This is the host side of that mount.
RUNS="$GEN/runs"

RUNNER_IMAGE="migr8-runner:local"
ORACLE_NAME="migr8-ac-oracle"
PG_NAME="migr8-ac-postgres"

ORACLE_IMAGE="gvenzl/oracle-free:23.9-slim"
PG_IMAGE="postgres:17.5"

: "${ORACLE_SYS_PASSWORD:=migr8_sys_test}"
: "${ORACLE_TEST_USER:=MIGR8_TEST}"
: "${ORACLE_TEST_PASSWORD:=migr8_ora_test}"
: "${PG_USER:=migr8_test}"
: "${PG_PASSWORD:=migr8_pg_test}"
: "${PG_DB:=migr8_test}"
: "${PG_SCHEMA:=migr8}"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "==> $*"; }

require_runtime() {
  command -v container >/dev/null || die "the 'container' CLI is not installed"
  if ! container system status >/dev/null 2>&1; then
    note "starting the container runtime"
    container system start >/dev/null
  fi
}

container_ip() {
  local name="$1"
  container inspect "$name" 2>/dev/null \
    | python3 -c '
import json, sys
data = json.load(sys.stdin)
data = data[0] if isinstance(data, list) else data
networks = (data.get("status") or {}).get("networks") or data.get("networks") or []
for net in networks:
    address = net.get("ipv4Address") or ""
    if address:
        print(address.split("/")[0])
        break
'
}

running() { container list 2>/dev/null | awk '{print $1}' | grep -qx "$1"; }

wait_for_log() {
  local name="$1" needle="$2" limit="${3:-900}" waited=0
  printf 'waiting for %s ' "$name"
  while (( waited < limit )); do
    if container logs "$name" 2>&1 | grep -q "$needle"; then echo " ready"; return 0; fi
    running "$name" || { echo " container stopped"; container logs "$name" 2>&1 | tail -20; return 1; }
    printf '.'; sleep 5; waited=$(( waited + 5 ))
  done
  echo " TIMEOUT after ${limit}s"; return 1
}

# --- commands ------------------------------------------------------------------

cmd_build() {
  require_runtime
  note "building $RUNNER_IMAGE"
  container build --tag "$RUNNER_IMAGE" --file "$HERE/Containerfile" "$ROOT"
}

cmd_up() {
  require_runtime
  if ! running "$PG_NAME"; then
    note "starting $PG_NAME"
    container rm "$PG_NAME" >/dev/null 2>&1 || true
    container run --detach --name "$PG_NAME" --cpus 2 --memory 1024MiB \
      --env "POSTGRES_USER=$PG_USER" \
      --env "POSTGRES_PASSWORD=$PG_PASSWORD" \
      --env "POSTGRES_DB=$PG_DB" \
      "$PG_IMAGE" postgres -c synchronous_commit=on -c fsync=on >/dev/null
    wait_for_log "$PG_NAME" "database system is ready to accept connections" 180
  fi
  if ! running "$ORACLE_NAME"; then
    note "starting $ORACLE_NAME (first start initialises the database)"
    container rm "$ORACLE_NAME" >/dev/null 2>&1 || true
    # Storage is the container's own writable layer: the gvenzl image cannot
    # initialise into an Apple container named volume, and a disposable test
    # database does not need to survive `down` anyway.
    container run --detach --name "$ORACLE_NAME" --cpus 4 --memory 4096MiB \
      --env "ORACLE_PASSWORD=$ORACLE_SYS_PASSWORD" \
      --env "ORACLE_CHARACTERSET=AL32UTF8" \
      "$ORACLE_IMAGE" >/dev/null
    wait_for_log "$ORACLE_NAME" "DATABASE IS READY TO USE" 900
  fi
  cmd_provision
  cmd_info
}

render() {
  mkdir -p "$GEN"
  local oracle_ip pg_ip
  oracle_ip="$(container_ip "$ORACLE_NAME")"
  pg_ip="$(container_ip "$PG_NAME")"
  [[ -n "$oracle_ip" ]] || die "cannot determine the $ORACLE_NAME address; is it running?"
  [[ -n "$pg_ip" ]] || die "cannot determine the $PG_NAME address; is it running?"

  sed -e "s|@ORACLE_DSN@|${oracle_ip}:1521/FREEPDB1|g" \
      -e "s|@ORACLE_USER@|${ORACLE_TEST_USER}|g" \
      "$HERE/templates/oracle.toml.in" > "$GEN/oracle.toml"
  sed -e "s|@PG_CONNINFO@|host=${pg_ip} port=5432 dbname=${PG_DB} user=${PG_USER}|g" \
      -e "s|@PG_USER@|${PG_USER}|g" \
      -e "s|@PG_SCHEMA@|${PG_SCHEMA}|g" \
      "$HERE/templates/postgres.toml.in" > "$GEN/postgres.toml"
  echo "$oracle_ip" > "$GEN/oracle.ip"
  echo "$pg_ip" > "$GEN/postgres.ip"
}

cmd_provision() {
  require_runtime
  render
  local oracle_ip pg_ip
  oracle_ip="$(cat "$GEN/oracle.ip")"
  pg_ip="$(cat "$GEN/postgres.ip")"
  note "provisioning the disposable Oracle schema"
  container run --rm \
    --env "ORACLE_DSN=${oracle_ip}:1521/FREEPDB1" \
    --env "ORACLE_SYS_PASSWORD=$ORACLE_SYS_PASSWORD" \
    --env "ORACLE_TEST_USER=$ORACLE_TEST_USER" \
    --env "ORACLE_TEST_PASSWORD=$ORACLE_TEST_PASSWORD" \
    --entrypoint python "$RUNNER_IMAGE" \
    /opt/migr8/testenv/provision_oracle.py
  note "provisioning the disposable PostgreSQL schema"
  container run --rm \
    --env "PG_CONNINFO=host=${pg_ip} port=5432 dbname=${PG_DB} user=${PG_USER} password=${PG_PASSWORD}" \
    --env "MIGR8_PG_SCHEMA=$PG_SCHEMA" \
    --entrypoint python "$RUNNER_IMAGE" \
    /opt/migr8/testenv/provision_postgres.py
}

# Run the migration job. Usage: ctl.sh <engine> <migr8 args...>
run_job() {
  local engine="$1"; shift
  require_runtime
  [[ -f "$GEN/$engine.toml" ]] || render
  local password manifest
  case "$engine" in
    oracle)   password="$ORACLE_TEST_PASSWORD"; manifest="/opt/migr8/examples/oracle/manifest.toml" ;;
    postgres) password="$PG_PASSWORD";          manifest="/opt/migr8/examples/postgres/manifest.toml" ;;
    *) die "unknown engine '$engine'; use oracle or postgres" ;;
  esac
  mkdir -p "$RUNS"
  # The job runs as uid 10001 and this directory belongs to whoever ran this
  # script, so the mount is opened to both. A deployment mounts a directory its
  # own runner owns instead; this is a disposable local rig.
  chmod 0777 "$RUNS"
  local status=0
  # The job's exit code is the command's result, so it is kept rather than
  # replaced by the note that follows it.
  container run --rm \
    --volume "$GEN:/etc/migr8" \
    --volume "$RUNS:/var/log/migr8" \
    --env "MIGR8_PASSWORD=$password" \
    "$RUNNER_IMAGE" \
    "$@" --config "/etc/migr8/$engine.toml" --manifest "$manifest" || status=$?
  note "event log: $RUNS/run.jsonl"
  return $status
}

cmd_info() {
  render
  cat <<EOF

Oracle     : $(cat "$GEN/oracle.ip"):1521/FREEPDB1   user=$ORACLE_TEST_USER
PostgreSQL : $(cat "$GEN/postgres.ip"):5432          db=$PG_DB user=$PG_USER schema=$PG_SCHEMA
Generated configs in: $GEN

Run the migration job:
  $0 migrate oracle
  $0 status postgres
EOF
}

case "${1:-}" in
  build)     cmd_build ;;
  up)        cmd_up ;;
  provision) cmd_provision ;;
  info)      cmd_info ;;
  ips)       render; echo "oracle   $(cat "$GEN/oracle.ip")"; echo "postgres $(cat "$GEN/postgres.ip")" ;;
  migrate)   shift; run_job "${1:?engine required}" migrate "${@:2}" ;;
  status)    shift; run_job "${1:?engine required}" status "${@:2}" ;;
  validate)  shift; run_job "${1:?engine required}" validate "${@:2}" ;;
  logs)      shift; container logs "${1:?container name required}" ;;
  runlog)    [[ -s "$RUNS/run.jsonl" ]] || die "no job event log at $RUNS/run.jsonl yet"
             cat "$RUNS/run.jsonl" ;;
  ps)        container list --all | grep -E 'migr8-ac-|^ID' || true ;;
  down)      for n in "$PG_NAME" "$ORACLE_NAME"; do container stop "$n" >/dev/null 2>&1 || true; done
             echo "stopped" ;;
  destroy)   for n in "$PG_NAME" "$ORACLE_NAME"; do
               container stop "$n" >/dev/null 2>&1 || true
               container rm "$n" >/dev/null 2>&1 || true
             done
             rm -rf "$GEN"
             echo "removed this project's containers and generated configs" ;;
  *)
    cat <<USAGE
usage: ctl.sh <command>

  build                  build the migration-job image
  up                     start both databases, wait for readiness, provision
  provision              re-create the disposable test schemas
  info | ips             show container addresses and generated config paths
  migrate  <engine> [..] run 'migr8 migrate' as a container job
  status   <engine> [..] run 'migr8 status'
  validate <engine> [..] run 'migr8 validate'
  logs <container>       show a database container's log
  runlog                 show the migration job's event log, kept on the host
  ps                     list this project's containers
  down                   stop the database containers
  destroy                stop and remove them, and the generated configs

engines: oracle | postgres
USAGE
    exit 1 ;;
esac
