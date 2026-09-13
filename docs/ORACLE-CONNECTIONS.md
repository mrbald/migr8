# Connecting to Oracle

Four decisions sit between `migr8` and an Oracle database: which driver mode,
how the database is named, who the run authenticates as, and whether the
connection is encrypted. They are independent, they are usually made by
different people, and the words for them are Oracle's rather than this tool's.
This is the short version of each, and what `migr8` does with it.

The configuration reference is [`MANUAL.md`](MANUAL.md); what has actually been
tested is in [`ACCEPTANCE.md`](ACCEPTANCE.md). This page is the background.

## 1. Thin or thick: one package, two drivers

`python-oracledb` ships two implementations.

**Thin** speaks Oracle's network protocol in Python. Nothing is installed
beyond the package. It is the default here and it covers ordinary
password-authenticated work.

**Thick** loads Oracle's own C client libraries — Instant Client, or a full
database or client home — and calls them. It is what you need for features that
live in those libraries: external authentication through a wallet, `sqlnet.ora`
settings the thin driver does not read, Advanced Queuing, application
continuity, and some older or unusual server configurations.

The driver is thin unless a process calls `init_oracle_client()`, once, before
its first connection. That call is process-wide and cannot be undone. `migr8`
makes it when you ask for thick mode:

```toml
[oracle]
allow_thick_mode = true
client_lib_dir = "/opt/oracle/instantclient_23_9"   # optional
```

Then it reads the mode back from the connection. A run configured for thick that
comes back thin fails, because the two are different libraries, a different
network stack and different authentication paths — not a detail to discover
later.

`client_lib_dir` is optional: without it the client is found the way Oracle
documents (`ldconfig`, `LD_LIBRARY_PATH`, `ORACLE_HOME`). With it, the directory
still has to be on the loader's path, because the client resolves its *own*
libraries — `libnnz`, `libclntshcore` — through the loader rather than through
the path you gave. An Instant Client unpacked into a directory that is not on
`LD_LIBRARY_PATH` fails there and nowhere else, which is worth knowing before
you debug it.

## 2. Naming the database: Easy Connect and TNS

Two spellings reach the same database.

**Easy Connect** puts it in the string: `host:port/service_name`, optionally
with parameters (`host:port/service?connect_timeout=10`). Nothing on disk.

**A TNS alias** names an entry in `tnsnames.ora`:

```
ORDERS_PROD =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = db.internal)(PORT = 1521))
    (CONNECT_DATA = (SERVICE_NAME = ORDERS))
  )
```

with `database.dsn = "ORDERS_PROD"`. The file lives in a directory Oracle calls
`TNS_ADMIN`, alongside `sqlnet.ora` and often a wallet. `migr8` names it:

```toml
[oracle]
config_dir = "/etc/oracle"
```

This works in both driver modes. Aliases are how an estate keeps addresses out
of application configuration: the same alias points at different hosts in
different environments, and the failover or load-balancing description lives in
one file rather than in every service.

A namespace's binding records the schema and the lock id, not the spelling of
the address, so moving from a host and port to an alias — or back — does not
disturb an initialized namespace.

## 3. Who the run authenticates as

Three forms, and they compose.

**A password.** `database.user = "ORDERS_MIGRATOR"`, with the password in
`MIGR8_PASSWORD`. Nothing else to arrange.

**External authentication.** No password at all: the client proves who it is
with a certificate in a wallet, and the database maps that identity to a user
created `IDENTIFIED EXTERNALLY`. Nothing to rotate in the application, nothing
to leak in an environment variable. `python-oracledb` does this in thick mode
only.

**Proxy authentication.** One identity authenticates, another one runs. The
database has to permit it:

```sql
ALTER USER APP_DBA GRANT CONNECT THROUGH ORDERS_MIGRATOR;
```

and the connect string names both, target in brackets:

| `database.user` | Authenticates as | Session user | Password |
|---|---|---|---|
| `APP_DBA` | `APP_DBA` | `APP_DBA` | yes |
| `ORDERS_MIGRATOR[APP_DBA]` | `ORDERS_MIGRATOR` | `APP_DBA` | yes, the runner's |
| `[APP_DBA]` | a wallet certificate | `APP_DBA` | no |

Proxy authentication is what lets the identity that proves *who is running* be
separate from the schema that *owns the objects*. The runner needs no privilege
of its own on the target: after connecting it **is** the target, with the
target's privileges and the target's default schema. That is why `migr8` takes
the bracketed name as the default `target_schema` and leaves `CURRENT_SCHEMA`
alone — unlike the other way of separating them, where a runner holds `CREATE
ANY TABLE` and points `CURRENT_SCHEMA` at someone else's schema.

`ALL_USERS` shows nothing about who may proxy; `PROXY_USERS` does, and
`sys_context('USERENV','PROXY_USER')` shows it inside a session.

## 4. TLS, and what a wallet is

`TCPS` is Oracle's TLS. The listener gets a second endpoint, conventionally on
port 2484:

```
(ADDRESS = (PROTOCOL = TCPS)(HOST = db.internal)(PORT = 2484))
```

A **wallet** is Oracle's keystore: a directory holding `ewallet.p12`, and
usually `cwallet.sso`, an "auto-login" copy the client can open without a
password. The server's wallet holds its certificate and private key. A client
wallet holds the certificates it trusts, and — for mutual TLS — the client's own
certificate and key.

Encryption alone needs only the client to trust the server's certificate.
**Certificate authentication** is mutual TLS plus a mapping: the listener is set
to demand a client certificate, and the database has a user whose identity *is*
that certificate's distinguished name.

```
# sqlnet.ora, in TNS_ADMIN
WALLET_LOCATION = (SOURCE = (METHOD = FILE)(METHOD_DATA = (DIRECTORY = /etc/oracle/wallet)))
SQLNET.AUTHENTICATION_SERVICES = (TCPS)
SSL_CLIENT_AUTHENTICATION = TRUE
SSL_SERVER_DN_MATCH = TRUE
```

```sql
CREATE USER "CN=orders-migrator,OU=platform,O=example" IDENTIFIED EXTERNALLY
  AS 'CN=orders-migrator,OU=platform,O=example';
GRANT CREATE SESSION TO "CN=orders-migrator,OU=platform,O=example";
ALTER USER APP_DBA GRANT CONNECT THROUGH "CN=orders-migrator,OU=platform,O=example";
```

With that in place, `database.user = "[APP_DBA]"` and no `MIGR8_PASSWORD`: the
certificate says who is connecting, the proxy grant says who it may become, and
no secret is configured anywhere in the runner.

`testenv/provision_tls.sh` builds exactly this against the disposable test
database, which is the shortest way to see the whole path working: two wallets,
a TCPS endpoint on the listener, a certificate identity mapped to a user, and a
proxy grant into a test schema. It writes a client `TNS_ADMIN` to `testenv/tls/`.
Two things about that script are worth knowing before you copy it. `orapki` is a
Java program, and the slim database image ships neither a JRE nor the PKI jars,
so the script adds both to the container; and a listener endpoint is added by a
restart, not by `lsnrctl reload`.

Wallets are read by the client libraries, so this whole path is thick-mode only.
`SSL_SERVER_DN_MATCH` is worth leaving on: without it a client will complete a
handshake with any certificate the chain accepts, whoever the host turns out to
be.

## 5. Putting it together

```toml
# A wallet-authenticated run through a TNS alias, proxying into the schema owner.
[database]
adapter = "oracle"
dsn = "ORDERS_PROD"                    # from tnsnames.ora, a TCPS address
user = "[APP_DBA]"                     # certificate identity, running as APP_DBA

[oracle]
allow_thick_mode = true                # wallets are read by the client libraries
client_lib_dir = "/opt/oracle/instantclient_23_9"
config_dir = "/etc/oracle"             # tnsnames.ora, sqlnet.ora, wallet

[lock]
provider = "dbms_lock"
package = "SYS.DBMS_LOCK"
id = 4711
timeout_seconds = 60
```

`migr8` refuses the combinations that contradict themselves: a password
alongside a connect string that names no connecting user, and an absent password
without thick mode.

## What has been tested

| Path | Status |
|---|---|
| Thin, Easy Connect, password | tested |
| Thick, Easy Connect, password | tested, Instant Client 23.9 on Linux ARM64 |
| TNS alias, both modes | tested |
| Proxy connect string with a password | tested against a real server |
| `[TARGET]` with a wallet, TCPS, certificate identity | tested against a real listener |

The last row was rehearsed end to end: an initial install over TLS with no
password in the environment or the configuration, the session authenticated as
the certificate's DN and running as the schema it may become
(`AUTHENTICATION_METHOD` is `SSL_PROXY`). One self-signed certificate pair and
one server; a real estate's CA, revocation, expiry and rotation are its own
subject. [`ACCEPTANCE.md`](ACCEPTANCE.md) records the run and what is still open.

One property of thick mode deserves repeating here, because it is the thing that
surprises people: `init_oracle_client()` happens once per process, and the
directories given to the *first* call are the ones in force. If something else in
the process loaded the client already — an application embedding this engine, or
a test harness — then `client_lib_dir` and `config_dir` cannot take effect, and
the run says so rather than pretending otherwise.

## Sources

- [python-oracledb: thin and thick modes](https://python-oracledb.readthedocs.io/en/latest/user_guide/initialization.html)
- [python-oracledb: connection handling and proxy authentication](https://python-oracledb.readthedocs.io/en/latest/user_guide/connection_handling.html)
- [Oracle Net Services: naming methods and `sqlnet.ora`](https://docs.oracle.com/en/database/oracle/oracle-database/23/netag/)
- [Oracle Database Security Guide: strong authentication and TLS](https://docs.oracle.com/en/database/oracle/oracle-database/23/dbseg/)
- [Proxy authentication and `CONNECT THROUGH`](https://docs.oracle.com/en/database/oracle/oracle-database/23/dbseg/configuring-privilege-and-role-authorization.html)
