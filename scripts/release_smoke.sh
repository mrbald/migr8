#!/usr/bin/env bash
# Smoke-test a release candidate the way an operator installs it.
#
#   scripts/release_smoke.sh <dist-dir> <exports-dir>
#
# <dist-dir> holds exactly one wheel and one sdist (`uv build --sdist`, then
# `uv build --wheel`). <exports-dir> holds runtime-oracle.txt and
# runtime-postgres.txt (`uv export`, hashes included). The base profile has no
# runtime dependencies, so it has no file.
#
# For each profile, base, oracle and postgres, the script makes a fresh venv,
# installs the profile's exported closure with --require-hashes, then the wheel
# with --no-deps, and drives the installed `migr8` from a scratch directory with
# PYTHONPATH unset. Inputs are copies of the checkout's examples/; code never
# comes from the checkout. Last, it rebuilds a wheel from the sdist and compares
# the two payloads.
#
# base runs the SQLite example end to end. oracle and postgres run their
# example's offline validation, and a full migrate against a live server when
# the profile's variables are set. A live run needs a freshly provisioned, empty
# schema; the script fails on a namespace that is already initialized.
#
# Environment:
#   PYTHON                       interpreter for the venvs, 3.12 or later
#                                (default: python3)
#   MIGR8_SMOKE_ORACLE_DSN       Oracle connect string, e.g. localhost:15210/FREEPDB1
#   MIGR8_SMOKE_ORACLE_USER      connect user, which is also the target schema
#   MIGR8_SMOKE_ORACLE_PASSWORD
#   MIGR8_SMOKE_PG_DSN           libpq conninfo without user or password,
#                                e.g. "host=127.0.0.1 port=15433 dbname=migr8_test"
#   MIGR8_SMOKE_PG_USER
#   MIGR8_SMOKE_PG_PASSWORD
#   MIGR8_SMOKE_PG_SCHEMA        target schema
#   MIGR8_SMOKE_REQUIRE_LIVE     set to 1 to fail where a live run would skip
#
# A live profile with none of its variables set prints one "SKIP:" line and the
# script goes on; with some but not all of them set, it fails. Every check
# prints one "ok:" line. The first failure prints "FAIL:" and exits non-zero; a
# failed setup step (mktemp, cd, cp) exits non-zero through `set -e` without
# that line. Needs network for pip, and `unzip`.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <dist-dir> <exports-dir>" >&2
    exit 2
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
dist="$(cd "$1" && pwd)"
exports="$(cd "$2" && pwd)"
python="${PYTHON:-python3}"
require_live="${MIGR8_SMOKE_REQUIRE_LIVE:-}"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}
ok() { echo "ok: $*"; }

# Bound a command in seconds. GNU timeout is on Linux runners; macOS has it
# only with coreutils, so perl's alarm is the fallback there.
bounded() {
    local secs="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$secs" "$@"
    else
        perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$secs" "$@"
    fi
}

"$python" - <<'EOF' || fail "$python is not Python 3.12 or later (set PYTHON)"
import sys

if sys.version_info < (3, 12):
    sys.exit(1)
EOF
ok "interpreter $python: $("$python" -c 'import sys; print(sys.version.split()[0])')"

shopt -s nullglob
wheels=("$dist"/*.whl)
sdists=("$dist"/*.tar.gz)
shopt -u nullglob
[ "${#wheels[@]}" -eq 1 ] || fail "expected one wheel in $dist, found ${#wheels[@]}"
[ "${#sdists[@]}" -eq 1 ] || fail "expected one sdist in $dist, found ${#sdists[@]}"
wheel="${wheels[0]}"
sdist="${sdists[0]}"
version="$(basename "$wheel" | cut -d- -f2)"
[ "$(basename "$sdist")" = "migr8-$version.tar.gz" ] \
    || fail "$(basename "$sdist") does not name the wheel's version $version"
ok "candidate: $(basename "$wheel") and $(basename "$sdist")"

work="$(mktemp -d "${TMPDIR:-/tmp}/migr8-smoke.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# The installed migr8 reads MIGR8_PASSWORD and MIGR8_LOG_FILE; the live runs
# below set the password per command, and nothing else may reach it.
unset PYTHONPATH VIRTUAL_ENV PYTHONHOME MIGR8_PASSWORD MIGR8_LOG_FILE
cd "$work"

venv=""

# Copy an example into the scratch directory, without anything a local run
# left in the checkout's copy.
copy_example() { # copy_example ENGINE DEST
    cp -R "$repo/examples/$1" "$2"
    rm -rf "$2/build"
    find "$2" -name __pycache__ -prune -exec rm -rf {} +
}

check_install() {
    local profile="$1" closure=""
    venv="$work/venv-$profile"
    "$python" -m venv "$venv" || fail "[$profile] $python -m venv"
    if [ "$profile" != base ]; then
        closure="$exports/runtime-$profile.txt"
        [ -f "$closure" ] || fail "[$profile] $closure is missing"
        "$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
            --require-hashes -r "$closure" \
            || fail "[$profile] pip install --require-hashes -r runtime-$profile.txt"
        ok "[$profile] runtime-$profile.txt installed with --require-hashes"
    fi
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-deps "$wheel" \
        || fail "[$profile] pip install --no-deps $(basename "$wheel")"
    ok "[$profile] wheel installed with --no-deps"
    "$venv/bin/python" -m pip check >/dev/null || fail "[$profile] pip check"
    ok "[$profile] pip check: no missing or conflicting requirements"
    if [ "$profile" != base ]; then
        # With no index, pip can satisfy the extra's requirements only from
        # what the closure installed.
        "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --dry-run \
            --no-index --find-links "$dist" "migr8[$profile]==$version" \
            || fail "[$profile] the installed closure does not satisfy migr8[$profile]"
        ok "[$profile] the installed closure satisfies migr8[$profile] without an index"
    fi

    "$venv/bin/python" - "$venv" "$profile" "$version" "$closure" <<'EOF' \
        || fail "[$profile] installed package layout"
import importlib.util
import json
import re
import sys
from importlib.metadata import distribution, distributions
from pathlib import Path

import migr8

venv = Path(sys.argv[1]).resolve()
profile, version, closure = sys.argv[2], sys.argv[3], sys.argv[4]
tag = f"[{profile}]"

where = Path(migr8.__file__).resolve()
if venv not in where.parents or "site-packages" not in where.parts:
    sys.exit(f"migr8 imported from {where}, not from {venv}'s site-packages")
print(f"ok: {tag} import migr8 resolves to the venv's site-packages ({where.parent})")

dist = distribution("migr8")
direct = dist.read_text("direct_url.json")
if direct and json.loads(direct).get("dir_info", {}).get("editable"):
    sys.exit("migr8 is an editable install")
if dist.version != version or migr8.TOOL_VERSION != version:
    sys.exit(f"installed {dist.version}, TOOL_VERSION {migr8.TOOL_VERSION}, wheel {version}")
print(f"ok: {tag} migr8 {version} is a regular install; TOOL_VERSION matches the wheel")

expected = {"LICENSE", "LICENSING.md", "COMMERCIAL.md"}
declared = set(dist.metadata.get_all("License-File") or ())
if declared != expected:
    sys.exit(f"License-File metadata {sorted(declared)} != {sorted(expected)}")
files = {str(f): f for f in dist.files or ()}
for name in sorted(expected):
    hits = [f for key, f in files.items() if key.endswith(f".dist-info/licenses/{name}")]
    if not hits or not Path(dist.locate_file(hits[0])).is_file():
        sys.exit(f"license file {name} missing from the dist-info")
print(f"ok: {tag} license files installed: {', '.join(sorted(expected))}")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


allowed = {"pip", "migr8"}
if closure:
    for line in Path(closure).read_text().splitlines():
        pinned = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if pinned:
            allowed.add(canonical(pinned.group(1)))
installed = {canonical(d.metadata["Name"]) for d in distributions()}
foreign = sorted(installed - allowed)
if foreign:
    sys.exit(f"installed beyond migr8, pip and the closure: {foreign}")
drivers = {"oracle": "oracledb", "postgres": "psycopg"}
for owner, module in drivers.items():
    present = importlib.util.find_spec(module) is not None
    if present != (owner == profile):
        sys.exit(f"{module} {'present' if present else 'absent'} in the {profile} profile")
for tool in ("pytest", "ruff", "mypy", "coverage", "hatchling"):
    if importlib.util.find_spec(tool) is not None or canonical(tool) in installed:
        sys.exit(f"{tool} is installed in the {profile} profile")
source = f"migr8, pip and runtime-{profile}.txt" if closure else "migr8 and pip"
driver = drivers.get(profile, "no driver")
print(
    f"ok: {tag} installed set is {source} ({len(installed)} distributions; "
    f"{driver}); no foreign driver or dev tool"
)
EOF

    local said
    said="$(bounded 60 "$venv/bin/migr8" --version)" || fail "[$profile] migr8 --version"
    [ "$said" = "migr8 $version" ] || fail "[$profile] migr8 --version printed: $said"
    ok "[$profile] migr8 --version prints: $said"
}

# Assert a `status --json` report: initialized, nothing pending, every manifest
# entry SUCCESS and recorded by this version.
check_status_json() { # check_status_json PROFILE REPORT MANIFEST
    "$venv/bin/python" - "$2" "$3" "$version" <<'EOF' || fail "[$1] status --json after migrate"
import json
import sys
import tomllib

report = json.load(open(sys.argv[1]))
with open(sys.argv[2], "rb") as handle:
    count = len(tomllib.load(handle)["migration"])
version = sys.argv[3]
if report["exit_code"] != 0 or not report["initialized"] or report["pending_count"] != 0:
    sys.exit(f"exit_code {report['exit_code']}, pending {report['pending_count']}")
rows = report["migrations"]
if len(rows) != count or report["success_count"] != count:
    sys.exit(f"{report['success_count']} successful of {len(rows)} rows; manifest has {count}")
stale = [r["id"] for r in rows if r["state"] != "SUCCESS" or r["tool_version"] != version]
if stale:
    sys.exit(f"not SUCCESS under {version}: {stale}")
EOF
}

# One example from empty namespace to validated history: status reports the
# namespace uninitialized (exit 6), then migrate, validate and status --json.
# PASSWORD is empty for SQLite.
run_example() { # run_example PROFILE LABEL DIR PASSWORD SECS
    local profile="$1" label="$2" dir="$3" password="$4" secs="$5" rc
    local -a env_cmd=(env)
    [ -z "$password" ] || env_cmd=(env "MIGR8_PASSWORD=$password")
    cd "$dir"
    rc=0
    bounded "$secs" "${env_cmd[@]}" "$venv/bin/migr8" status >"$work/status0.out" 2>&1 || rc=$?
    if [ "$rc" -ne 6 ]; then
        cat "$work/status0.out" >&2
        fail "[$profile] $label: status before migrate exited $rc, expected 6 (not" \
            "initialized); a live run needs a reachable server and a freshly provisioned schema"
    fi
    ok "[$profile] $label: status reports the namespace uninitialized (exit 6)"
    bounded "$secs" "${env_cmd[@]}" "$venv/bin/migr8" migrate >"$work/migrate.out" 2>&1 \
        || { cat "$work/migrate.out" >&2; fail "[$profile] $label: migrate"; }
    ok "[$profile] $label: migrate exits 0 ($(grep -c '^applied ' "$work/migrate.out") applied)"
    bounded "$secs" "${env_cmd[@]}" "$venv/bin/migr8" validate >"$work/validate.out" 2>&1 \
        || { cat "$work/validate.out" >&2; fail "[$profile] $label: validate"; }
    ok "[$profile] $label: validate exits 0"
    bounded "$secs" "${env_cmd[@]}" "$venv/bin/migr8" status --json >"$work/status.json" \
        || fail "[$profile] $label: status --json"
    check_status_json "$profile" "$work/status.json" "$dir/manifest.toml"
    ok "[$profile] $label: status --json: every migration SUCCESS, recorded by $version"
    cd "$work"
}

check_offline() { # check_offline PROFILE ENGINE
    local dir="$work/offline-$1-$2"
    copy_example "$2" "$dir"
    cd "$dir"
    bounded 60 "$venv/bin/migr8" validate --offline >"$work/offline.out" 2>&1 \
        || { cat "$work/offline.out" >&2; fail "[$1] validate --offline on examples/$2"; }
    cd "$work"
    ok "[$1] validate --offline on examples/$2 exits 0"
}

# Without the driver, the adapter refuses with exit 1 and names it.
check_missing_driver() { # check_missing_driver PROFILE ENGINE DRIVER
    local dir="$work/missing-$1-$2" rc=0
    copy_example "$2" "$dir"
    cd "$dir"
    bounded 60 "$venv/bin/migr8" validate --offline >"$work/missing.out" 2>&1 || rc=$?
    cd "$work"
    [ "$rc" -eq 1 ] || { cat "$work/missing.out" >&2; fail "[$1] examples/$2 exited $rc, expected 1"; }
    grep -q "the $2 adapter requires $3" "$work/missing.out" \
        || fail "[$1] examples/$2 refusal text: $(cat "$work/missing.out")"
    ok "[$1] examples/$2 is refused with exit 1: the $2 adapter requires $3"
}

# Point a copy of the example at the live server. Values are written as TOML
# basic strings; JSON's string encoding is valid TOML for them.
point_config() { # point_config CONFIG DSN USER SCHEMA
    "$venv/bin/python" - "$@" <<'EOF' || fail "rewrite $1"
import json
import re
import sys
import tomllib
from pathlib import Path

path, dsn, user, schema = Path(sys.argv[1]), *sys.argv[2:]
text = path.read_text()
for key, value in (("dsn", dsn), ("user", user), ("target_schema", schema)):
    text, n = re.subn(rf"(?m)^{key} = .*$", lambda _: f"{key} = {json.dumps(value)}", text)
    if n != 1:
        sys.exit(f"{path}: expected one '{key} =' line, found {n}")
path.write_text(text)
database = tomllib.loads(text)["database"]
if (database["dsn"], database["user"], database["target_schema"]) != (dsn, user, schema):
    sys.exit(f"{path}: rewritten [database] reads back as {database}")
EOF
}

# The live run, or a SKIP line when none of the profile's variables are set.
check_live() { # check_live PROFILE
    local profile="$1" dsn user password schema names
    case "$profile" in
        oracle)
            names="MIGR8_SMOKE_ORACLE_DSN MIGR8_SMOKE_ORACLE_USER MIGR8_SMOKE_ORACLE_PASSWORD"
            dsn="${MIGR8_SMOKE_ORACLE_DSN:-}"
            user="${MIGR8_SMOKE_ORACLE_USER:-}"
            password="${MIGR8_SMOKE_ORACLE_PASSWORD:-}"
            schema="$user"
            ;;
        postgres)
            names="MIGR8_SMOKE_PG_DSN MIGR8_SMOKE_PG_USER MIGR8_SMOKE_PG_PASSWORD MIGR8_SMOKE_PG_SCHEMA"
            dsn="${MIGR8_SMOKE_PG_DSN:-}"
            user="${MIGR8_SMOKE_PG_USER:-}"
            password="${MIGR8_SMOKE_PG_PASSWORD:-}"
            schema="${MIGR8_SMOKE_PG_SCHEMA:-}"
            ;;
    esac
    local name set=0 unset=0
    for name in $names; do
        if [ -n "${!name:-}" ]; then set=$((set + 1)); else unset=$((unset + 1)); fi
    done
    if [ "$set" -eq 0 ]; then
        [ "$require_live" != 1 ] \
            || fail "[$profile] MIGR8_SMOKE_REQUIRE_LIVE=1 and none of $names is set"
        echo "SKIP: [$profile] live run against a server: none of $names is set"
        return 0
    fi
    [ "$unset" -eq 0 ] || fail "[$profile] set all of $names, or none"
    local dir="$work/live-$profile"
    copy_example "$profile" "$dir"
    point_config "$dir/migr8.toml" "$dsn" "$user" "$schema"
    run_example "$profile" "examples/$profile on $dsn as $user, schema $schema" \
        "$dir" "$password" 300
}

for profile in base oracle postgres; do
    echo "== profile $profile"
    check_install "$profile"
    case "$profile" in
        base)
            copy_example sqlite "$work/sqlite"
            run_example base "examples/sqlite" "$work/sqlite" "" 120
            check_missing_driver base oracle python-oracledb
            check_missing_driver base postgres psycopg
            ;;
        oracle)
            check_offline oracle oracle
            check_missing_driver oracle postgres psycopg
            check_live oracle
            ;;
        postgres)
            check_offline postgres postgres
            check_missing_driver postgres oracle python-oracledb
            check_live postgres
            ;;
    esac
done

# The sdist must build the same wheel. RECORD is excluded: it lists the
# other files' hashes, so it adds nothing once they are compared, and it is
# the one file a builder may order or format differently.
echo "== sdist rebuild"
# The rebuild uses the hatchling that built the candidate (its WHEEL
# Generator line), so a hatchling release in between cannot change WHEEL.
generator="$(unzip -p "$wheel" '*.dist-info/WHEEL' | sed -n 's/^Generator: hatchling //p')" \
    || fail "read the Generator line of $(basename "$wheel")"
[ -n "$generator" ] || fail "$(basename "$wheel") names no hatchling Generator"
echo "hatchling==$generator" >"$work/build-constraints.txt"
ok "rebuild constrained to hatchling==$generator"
rebuilt="$work/rebuilt"
PIP_CONSTRAINT="$work/build-constraints.txt" bounded 300 "$venv/bin/python" -m pip wheel \
    --quiet --disable-pip-version-check --no-deps "$sdist" -w "$rebuilt" \
    || fail "pip wheel from $(basename "$sdist")"
"$venv/bin/python" - "$wheel" "$rebuilt"/*.whl <<'EOF' || fail "sdist-rebuilt wheel differs"
import hashlib
import sys
import zipfile


def payload(path: str) -> dict[str, str]:
    with zipfile.ZipFile(path) as zf:
        return {
            name: hashlib.sha256(zf.read(name)).hexdigest()
            for name in zf.namelist()
            if not name.endswith(".dist-info/RECORD")
        }


direct, rebuilt = payload(sys.argv[1]), payload(sys.argv[2])
if set(direct) != set(rebuilt):
    only_d = sorted(set(direct) - set(rebuilt))
    only_r = sorted(set(rebuilt) - set(direct))
    sys.exit(f"file lists differ: only direct {only_d}, only rebuilt {only_r}")
changed = sorted(name for name in direct if direct[name] != rebuilt[name])
if changed:
    sys.exit(f"payloads differ in: {changed}")
print(f"ok: sdist-rebuilt wheel matches the direct wheel ({len(direct)} files, RECORD excluded)")
EOF

echo "release smoke: all checks passed"
