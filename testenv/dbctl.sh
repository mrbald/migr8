#!/usr/bin/env bash
# Start, health-check, inspect and stop the disposable test databases.
#
# Everything it touches belongs to the Compose project named in compose.yaml.
# It never modifies unrelated containers, volumes, global configuration or
# credentials.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
COMPOSE=(docker compose --project-directory "$HERE" -f "$HERE/compose.yaml")

if [[ -f "$HERE/.env" ]]; then
  set -a; . "$HERE/.env"; set +a
fi

: "${ORACLE_SYS_PASSWORD:=migr8_sys_test}"
: "${ORACLE_HOST_PORT:=15210}"
: "${ORACLE_TEST_USER:=MIGR8_TEST}"
: "${ORACLE_TEST_PASSWORD:=migr8_ora_test}"
: "${POSTGRES_USER:=migr8_test}"
: "${POSTGRES_PASSWORD:=migr8_pg_test}"
: "${POSTGRES_DB:=migr8_test}"
: "${POSTGRES_HOST_PORT:=15433}"
export ORACLE_SYS_PASSWORD ORACLE_HOST_PORT ORACLE_TEST_USER ORACLE_TEST_PASSWORD
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB POSTGRES_HOST_PORT

# One resolved endpoint pair for everything below. The provisioning scripts
# honour an inherited ORACLE_DSN or PG_CONNINFO, while the suite always connects
# to the ports Compose publishes, so a shell carrying settings from another rig
# could provision one database and test another and report the result as this
# project's. These are set, not defaulted, for that reason.
ORACLE_DSN="localhost:${ORACLE_HOST_PORT}/FREEPDB1"
PG_CONNINFO="host=127.0.0.1 port=${POSTGRES_HOST_PORT} dbname=${POSTGRES_DB} user=${POSTGRES_USER} password=${POSTGRES_PASSWORD}"
export ORACLE_DSN PG_CONNINFO

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

die() { echo "error: $*" >&2; exit 1; }

check_runtime() {
  command -v docker >/dev/null || die "docker CLI not found"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable; start it first"
  local mem
  mem="$(docker info --format '{{.MemTotal}}')"
  echo "runtime: $(docker info --format '{{.ServerVersion}}') arch=$(docker info --format '{{.Architecture}}') cpus=$(docker info --format '{{.NCPU}}') mem=${mem}"
  if (( mem < 3000000000 )); then
    echo "warning: less than 3 GB is available to Docker; the Oracle image may fail to start" >&2
  fi
}

wait_healthy() {
  local name="$1" limit="${2:-600}" elapsed=0 state
  printf 'waiting for %s ' "$name"
  while (( elapsed < limit )); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
    case "$state" in
      healthy) echo " healthy"; return 0 ;;
      missing) echo " (container missing)"; return 1 ;;
    esac
    printf '.'
    sleep 5
    elapsed=$(( elapsed + 5 ))
  done
  echo " TIMEOUT after ${limit}s (last state: $state)"
  return 1
}

cmd_up() {
  check_runtime
  "${COMPOSE[@]}" up -d "$@"
  local ok=0
  if [[ $# -eq 0 || " $* " == *" postgres "* ]]; then
    wait_healthy migr8-postgres 300 || ok=1
  fi
  if [[ $# -eq 0 || " $* " == *" oracle "* ]]; then
    wait_healthy migr8-oracle 900 || ok=1
  fi
  (( ok == 0 )) || die "one or more services did not become healthy; see 'dbctl.sh logs'"
  cmd_provision
  cmd_info
}

cmd_provision() {
  echo "provisioning disposable Oracle test schema ${ORACLE_TEST_USER}"
  "$PY" "$HERE/provision_oracle.py"
  echo "provisioning disposable PostgreSQL test schema"
  "$PY" "$HERE/provision_postgres.py"
}

cmd_info() {
  echo
  echo "Oracle    : localhost:${ORACLE_HOST_PORT}/FREEPDB1 user=${ORACLE_TEST_USER}"
  echo "PostgreSQL: host=127.0.0.1 port=${POSTGRES_HOST_PORT} dbname=${POSTGRES_DB} user=${POSTGRES_USER}"
  echo
  echo "Run the integration suites with:"
  echo "  testenv/dbctl.sh test"
}

cmd_versions() {
  "$PY" "$HERE/record_versions.py"
}

cmd_test() {
  local log status skips
  log="$(mktemp -t migr8-suite.XXXXXX)"
  set +e
  ( cd "$ROOT" && MIGR8_ORACLE_DSN="${ORACLE_DSN}" \
      MIGR8_ORACLE_USER="${ORACLE_TEST_USER}" \
      MIGR8_ORACLE_PASSWORD="${ORACLE_TEST_PASSWORD}" \
      MIGR8_ORACLE_SYS_PASSWORD="${ORACLE_SYS_PASSWORD}" \
      MIGR8_PG_HOST=127.0.0.1 MIGR8_PG_PORT="${POSTGRES_HOST_PORT}" \
      MIGR8_PG_DB="${POSTGRES_DB}" MIGR8_PG_USER="${POSTGRES_USER}" \
      MIGR8_PG_PASSWORD="${POSTGRES_PASSWORD}" \
      "$ROOT/.venv/bin/pytest" ${1+"$@"} ) 2>&1 | tee "$log"
  status="${PIPESTATUS[0]}"
  set -e
  # A live fixture skips when its service is unreachable or unconfigured, which
  # is the failure this command exists to catch. Applying the rule here rather
  # than in the workflow means a developer running the command locally gets the
  # gate CI gets. The skips that survive name a fixture no plain runner has: a
  # bounded filesystem for the ENOSPC case, and an Oracle wallet plus Client
  # libraries for the two certificate tests. pytest colours the summary even
  # when stdout is a pipe, so the codes are stripped before matching.
  skips="$(sed $'s/\x1b\\[[0-9;]*m//g' "$log" | grep '^SKIPPED' || true)"
  rm -f "$log"
  echo "${skips:-nothing skipped}"
  if printf '%s\n' "$skips" | grep -qE 'not reachable|not configured|is unset|could not import'; then
    die "a live suite skipped; the service it needs is unreachable"
  fi
  return "$status"
}

cmd_env() {
  cat <<ENVEOF
export MIGR8_ORACLE_DSN="${ORACLE_DSN}"
export MIGR8_ORACLE_USER="${ORACLE_TEST_USER}"
export MIGR8_ORACLE_PASSWORD="${ORACLE_TEST_PASSWORD}"
export MIGR8_ORACLE_SYS_PASSWORD="${ORACLE_SYS_PASSWORD}"
export MIGR8_PG_HOST=127.0.0.1
export MIGR8_PG_PORT="${POSTGRES_HOST_PORT}"
export MIGR8_PG_DB="${POSTGRES_DB}"
export MIGR8_PG_USER="${POSTGRES_USER}"
export MIGR8_PG_PASSWORD="${POSTGRES_PASSWORD}"
ENVEOF
}

case "${1:-}" in
  up)        shift; cmd_up "$@" ;;
  down)      shift; "${COMPOSE[@]}" down "$@" ;;
  destroy)   "${COMPOSE[@]}" down -v ;;   # removes this project's volumes only
  stop)      "${COMPOSE[@]}" stop ;;
  start)     "${COMPOSE[@]}" start ;;
  ps)        "${COMPOSE[@]}" ps ;;
  logs)      shift; "${COMPOSE[@]}" logs --tail 200 "$@" ;;
  health)    wait_healthy migr8-postgres 60; wait_healthy migr8-oracle 60 ;;
  provision) cmd_provision ;;
  versions)  cmd_versions ;;
  info)      cmd_info ;;
  env)       cmd_env ;;
  test)      shift; cmd_test "$@" ;;
  *)
    cat <<USAGE
usage: dbctl.sh <command>

  up [service]   start, wait for health, provision test schemas
  down           stop and remove containers (volumes kept)
  destroy        stop and remove containers AND this project's volumes
  stop | start   stop or restart containers without removing them
  ps | logs      inspect this project's containers
  health         re-check health
  provision      re-create the disposable test schemas
  versions       record actual server versions for the acceptance report
  env            print the environment the integration tests read
  test [args]    run pytest with the database environment set
USAGE
    exit 1 ;;
esac
