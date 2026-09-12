# Disposable test databases

Everything here belongs to one isolated Compose project, `migr8-testenv`.
Ports are bound to loopback, credentials are test-only, and storage lives in
named volumes owned by this project. Nothing in these scripts touches unrelated
containers, volumes, global configuration or credentials.

**Never point this at a shared or production database.** There is no fallback
path that would let it do so: the adapter refuses to run without an explicit
target schema, and `dbctl.sh` only ever addresses this project.

## Commands

```bash
testenv/dbctl.sh up          # start, wait for health, provision test schemas
testenv/dbctl.sh ps          # this project's containers
testenv/dbctl.sh logs        # last 200 lines
testenv/dbctl.sh health      # re-check health
testenv/dbctl.sh provision   # re-create the disposable test schemas
testenv/dbctl.sh versions    # print exact versions and digests for the report
testenv/dbctl.sh env         # print the environment the integration tests read
testenv/dbctl.sh test [args] # run pytest with that environment set
testenv/dbctl.sh down        # remove containers, keep volumes
testenv/dbctl.sh destroy     # remove containers AND this project's volumes
```

`up` checks the Docker daemon, architecture and available memory before pulling
or starting anything, and warns if the daemon has less than 3 GB, which the
Oracle image needs.

## What gets created

| Service | Published on | Credentials | Storage |
|---|---|---|---|
| Oracle Database Free | `127.0.0.1:15210` → `FREEPDB1` | `MIGR8_TEST` / test-only password; `SYS` for provisioning only | volume `migr8-testenv-oracle-data` |
| PostgreSQL | `127.0.0.1:15433` | `migr8_test` / test-only password | volume `migr8-testenv-postgres-data` |

PostgreSQL uses 15433 rather than 15432 because 15432 was already taken on the
development host. Override any port or credential in `testenv/.env`, which is
gitignored; `testenv/.env.example` lists every variable.

Image digests are pinned in `compose.yaml`. Record the actual server banner from
`dbctl.sh versions`, not the tag: tags and product names change.

## Oracle schemas

`provision_oracle.py` runs as `SYS` against this disposable instance only and
creates three users, dropping any previous copies first:

- `MIGR8_TEST` — the main test schema, where the connect user and the target
  schema are the same. It is granted `EXECUTE` on `SYS.DBMS_LOCK`, which the
  engine requires, plus `SELECT` on `V$SESSION` and `V$INSTANCE` so the optional
  session-liveness diagnostic can be exercised in its privileged form.
- `MIGR8_TEST_RUNNER` and `MIGR8_TEST_OWNER` — the separate
  connect-user / target-schema fixture. The runner holds `ANY`-style object
  privileges and `DBMS_LOCK`, but deliberately *not* `V$SESSION`, so the
  unprivileged diagnostic path is exercised too.

Oracle Free exercises the implementation on its own release. It does not certify
Oracle 19c; see [`../docs/ACCEPTANCE.md`](../docs/ACCEPTANCE.md).

## PostgreSQL schema

`provision_postgres.py` drops and recreates a dedicated `migr8` schema inside
the test database, separate from `public`, so the namespace binding is explicit
and cleanup is scoped. It also prints `synchronous_commit` and `fsync`, both of
which the adapter requires to be on.

## If a service will not start

State the blocker and carry on: the live suites skip with a message naming the
missing environment variables, and the corresponding acceptance gate stays
visibly incomplete rather than being satisfied by a mock. Common causes:

- **Docker daemon not reachable.** `dbctl.sh` says so and stops.
- **Port already allocated.** Set `ORACLE_HOST_PORT` or `POSTGRES_HOST_PORT` in
  `testenv/.env`. Do not stop the container that holds the port unless it is
  yours.
- **Oracle never becomes healthy.** Check `dbctl.sh logs oracle`. The first start
  initialises the database and takes several minutes; the health wait allows 15.
  Low memory is the usual cause.
