#!/usr/bin/env bash
# Build the TLS fixture for the disposable Oracle test container: wallets, a
# TCPS listener endpoint, a certificate identity mapped to a database user, and
# a proxy grant into a test schema.
#
# What this exercises is the shape a deployment uses when no password exists
# anywhere: the client proves who it is with a certificate, and the database
# says which schema that identity may become.
#
# Everything it touches belongs to this project's Compose container and to
# testenv/tls/ in the checkout. It never modifies an unrelated container, and it
# refuses to run against anything but the disposable test database.
#
# Rerun it after `dbctl.sh up` recreates the container: the fixture lives in the
# container's filesystem and goes with it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${ORACLE_CONTAINER:-migr8-oracle}"
SCHEMA="${MIGR8_ORACLE_TLS_SCHEMA:-MIGR8_TLS}"
CERT_DN="${MIGR8_ORACLE_CERT_DN:-CN=MIGR8_CERT,OU=platform,O=migr8}"
SERVER_DN="${MIGR8_ORACLE_SERVER_DN:-CN=migr8-oracle}"
TLS_PORT="${MIGR8_ORACLE_TLS_PORT:-2484}"
ALIAS="${MIGR8_ORACLE_TLS_ALIAS:-ORDERS_TLS}"
OUT="$HERE/tls"

# Wallet password. This is a disposable local fixture; the wallet that matters
# is the auto-login copy, which carries no password at all.
WALLET_PASSWORD="${MIGR8_WALLET_PASSWORD:-WalletPass123}"

# orapki is a Java program, and the slim database image ships neither a JRE nor
# the PKI jars. Both are added to the container here rather than to the image,
# and the jars are pinned by digest because they are downloaded.
JRE_PACKAGE="java-11-openjdk-headless"
PKI_JARS=(
  "com/oracle/database/security/oraclepki/23.9.0.25.07/oraclepki-23.9.0.25.07.jar d18e119751ef9bc5a7b3ec4b7158cbb467191e41dc873e7b477e8b55b0094bbe"
  "com/oracle/database/security/osdt_core/21.18.0.0/osdt_core-21.18.0.0.jar 4174ee8043b97371e8581e5b8cb8c9910229231ef9b8c1b99cd1e2e295071272"
  "com/oracle/database/security/osdt_cert/21.18.0.0/osdt_cert-21.18.0.0.jar 962a676f280fb9faafcecde69d937c724f58016dd6a8a40626d569f1b80a1b23"
)

die() { echo "error: $*" >&2; exit 1; }
note() { echo "==> $*"; }

command -v docker >/dev/null || die "docker CLI not found"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "container $CONTAINER is not running; start it with dbctl.sh up"
[[ "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER")" == "migr8-testenv" ]] \
  || die "$CONTAINER is not this project's disposable test database"

# --- the tools orapki needs, inside the container ------------------------------------

if ! docker exec "$CONTAINER" bash -lc 'ls /usr/lib/jvm/jre-11 >/dev/null 2>&1'; then
  note "installing $JRE_PACKAGE in $CONTAINER (orapki is a Java program)"
  docker exec -u 0 "$CONTAINER" microdnf -y --nodocs install "$JRE_PACKAGE" >/dev/null
fi

note "staging the Oracle PKI jars"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
for entry in "${PKI_JARS[@]}"; do
  path="${entry%% *}"; want="${entry##* }"; file="$(basename "$path")"
  curl -sSL --max-time 120 -o "$staging/$file" "https://repo1.maven.org/maven2/$path"
  got="$(shasum -a 256 "$staging/$file" | cut -d' ' -f1)"
  [[ "$got" == "$want" ]] || die "$file has digest $got, expected $want"
done
# docker cp writes as root, so the staging directory is cleared as root too.
docker exec -u 0 "$CONTAINER" bash -lc 'rm -rf /tmp/pki && mkdir -p /tmp/pki'
docker cp "$staging/." "$CONTAINER:/tmp/pki/" >/dev/null

# --- wallets -------------------------------------------------------------------------

note "creating the server and client wallets"
docker exec "$CONTAINER" bash -lc "
set -e
CP=\$(ls /tmp/pki/*.jar | paste -sd: -)
orapki() { /usr/lib/jvm/jre-11/bin/java -cp \"\$CP\" oracle.security.pki.textui.OraclePKITextUI \"\$@\" -nologo >/dev/null; }
WD=/opt/oracle/tls; PW='$WALLET_PASSWORD'
rm -rf \$WD; mkdir -p \$WD
orapki wallet create -wallet \$WD/server -pwd \$PW -auto_login
orapki wallet add -wallet \$WD/server -pwd \$PW -dn '$SERVER_DN' -keysize 2048 -self_signed -validity 3650
orapki wallet export -wallet \$WD/server -pwd \$PW -dn '$SERVER_DN' -cert \$WD/server.crt
orapki wallet create -wallet \$WD/client -pwd \$PW -auto_login
orapki wallet add -wallet \$WD/client -pwd \$PW -dn '$CERT_DN' -keysize 2048 -self_signed -validity 3650
orapki wallet export -wallet \$WD/client -pwd \$PW -dn '$CERT_DN' -cert \$WD/client.crt
orapki wallet add -wallet \$WD/server -pwd \$PW -trusted_cert -cert \$WD/client.crt
orapki wallet add -wallet \$WD/client -pwd \$PW -trusted_cert -cert \$WD/server.crt
"

# --- the listener's TLS endpoint -----------------------------------------------------

note "adding a TCPS endpoint on $TLS_PORT and demanding a client certificate"
docker exec "$CONTAINER" bash -lc "
set -e
NA=\$ORACLE_HOME/network/admin
cp -n \$NA/listener.ora \$NA/listener.ora.bak 2>/dev/null || true
cp -n \$NA/sqlnet.ora \$NA/sqlnet.ora.bak 2>/dev/null || true
cat > \$NA/listener.ora <<EOF
LISTENER =
  (DESCRIPTION_LIST =
    (DESCRIPTION =
      (ADDRESS = (PROTOCOL = IPC)(KEY = EXTPROC_FOR_FREE))
      (ADDRESS = (PROTOCOL = TCP)(HOST=)(PORT = 1521))
      (ADDRESS = (PROTOCOL = TCPS)(HOST=)(PORT = $TLS_PORT))
    )
  )

DEFAULT_SERVICE_LISTENER = FREE

WALLET_LOCATION =
  (SOURCE = (METHOD = FILE)(METHOD_DATA = (DIRECTORY = /opt/oracle/tls/server)))
SSL_CLIENT_AUTHENTICATION = TRUE
EOF
cat > \$NA/sqlnet.ora <<EOF
NAMES.DIRECTORY_PATH = (EZCONNECT, TNSNAMES)
DISABLE_OOB=ON
BREAK_POLL_SKIP=1000

WALLET_LOCATION =
  (SOURCE = (METHOD = FILE)(METHOD_DATA = (DIRECTORY = /opt/oracle/tls/server)))
SSL_CLIENT_AUTHENTICATION = TRUE
SQLNET.AUTHENTICATION_SERVICES = (BEQ, TCPS)
SSL_VERSION = 1.2
EOF
# An endpoint is added by a restart, not by a reload; the instance re-registers
# its services with the listener afterwards.
lsnrctl stop >/dev/null 2>&1 || true
lsnrctl start >/dev/null
echo 'ALTER SYSTEM REGISTER;' | sqlplus -s / as sysdba >/dev/null
"
sleep 3
docker exec "$CONTAINER" bash -lc 'lsnrctl status' | grep -q "PROTOCOL=tcps" \
  || die "the listener has no TCPS endpoint; see: docker exec $CONTAINER lsnrctl status"

# --- the certificate identity and what it may become ---------------------------------

note "mapping $CERT_DN to a database user that may become $SCHEMA"
PY="$HERE/../.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
ORACLE_TEST_USER="$SCHEMA" "$PY" "$HERE/provision_oracle.py" >/dev/null
MIGR8_TLS_SCHEMA="$SCHEMA" MIGR8_TLS_CERT_DN="$CERT_DN" "$PY" - <<'PY'
import os

import oracledb

dn = os.environ["MIGR8_TLS_CERT_DN"]
schema = os.environ["MIGR8_TLS_SCHEMA"]
dsn = f"localhost:{os.environ.get('ORACLE_HOST_PORT', '15210')}/FREEPDB1"
with oracledb.connect(
    user="sys",
    password=os.environ.get("ORACLE_SYS_PASSWORD", "migr8_sys_test"),
    dsn=dsn,
    mode=oracledb.AUTH_MODE_SYSDBA,
) as connection:
    cursor = connection.cursor()
    for statement in (
        f'DROP USER "{dn}" CASCADE',
        f"""CREATE USER "{dn}" IDENTIFIED EXTERNALLY AS '{dn}'""",
        f'GRANT CREATE SESSION TO "{dn}"',
        f'ALTER USER {schema} GRANT CONNECT THROUGH "{dn}"',
    ):
        try:
            cursor.execute(statement)
        except oracledb.DatabaseError as exc:
            # ORA-01918: the user does not exist yet, which is the normal first run.
            if exc.args[0].code != 1918:
                raise
PY

# --- the client side, in the checkout ------------------------------------------------

note "writing the client TNS_ADMIN to $OUT"
rm -rf "$OUT"; mkdir -p "$OUT"
docker cp "$CONTAINER:/opt/oracle/tls/client/cwallet.sso" "$OUT/cwallet.sso" >/dev/null
docker cp "$CONTAINER:/opt/oracle/tls/client/ewallet.p12" "$OUT/ewallet.p12" >/dev/null
cat > "$OUT/sqlnet.ora" <<EOF
# The wallet is beside this file, which is what the client is pointed at.
WALLET_LOCATION =
  (SOURCE = (METHOD = FILE)(METHOD_DATA = (DIRECTORY = \$TNS_ADMIN)))
SQLNET.AUTHENTICATION_SERVICES = (TCPS)
SSL_SERVER_DN_MATCH = TRUE
SSL_VERSION = 1.2
EOF
cat > "$OUT/tnsnames.ora" <<EOF
$ALIAS =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCPS)(HOST = $CONTAINER)(PORT = $TLS_PORT))
    (CONNECT_DATA = (SERVICE_NAME = FREEPDB1))
    (SECURITY = (SSL_SERVER_CERT_DN = "$SERVER_DN"))
  )
EOF

cat <<EOF

The fixture is ready.

  schema            $SCHEMA
  certificate       $CERT_DN
  alias             $ALIAS  (TCPS on $TLS_PORT, host $CONTAINER)
  client TNS_ADMIN  $OUT

The address is the container's name on the Compose network, so the client runs
in a container on that network. The Oracle Client libraries have to be there
too, because a wallet is read by them and not by the thin driver:

  docker run --rm --network migr8-testenv_default \\
    -v "\$PWD:/src:ro" -v /path/to/instantclient:/opt/ic:ro -v $OUT:/etc/oracle \\
    -e LD_LIBRARY_PATH=/opt/ic \\
    -e MIGR8_ORACLE_TLS_ADMIN=/etc/oracle -e MIGR8_ORACLE_CLIENT_LIB=/opt/ic \\
    -e MIGR8_ORACLE_TLS_SCHEMA=$SCHEMA -e MIGR8_ORACLE_TLS_ALIAS=$ALIAS \\
    python:3.14-slim ...   # install pytest and oracledb, then: pytest -m oracle -k tls
EOF
