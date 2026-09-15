# The migration job as a container

This builds the migration job as an image and runs it as a container against
the disposable databases from [`../../testenv/`](../../testenv/), on any engine
that serves the Docker API. It is the production shape: migrations are a job
that runs once, alongside the database, from an image whose contents are fixed.

## Quick start

```bash
testenv/dbctl.sh up                       # the databases, on the Compose network
deploy/container-job/ctl.sh provision     # the job's own schemas in them
deploy/container-job/ctl.sh build         # the migration-job image
deploy/container-job/ctl.sh migrate oracle
deploy/container-job/ctl.sh status  oracle
deploy/container-job/ctl.sh migrate postgres
deploy/container-job/ctl.sh validate postgres
deploy/container-job/ctl.sh runlog        # the job's event log, kept on the host
```

`ctl.sh` writes to schemas of its own -- the Oracle users `MIGR8_JOB`,
`MIGR8_JOB_RUNNER` and `MIGR8_JOB_OWNER`, and the PostgreSQL schema
`migr8_job` -- to `generated/` here (gitignored), and to the image
`migr8-runner:local`. It changes no other container, image, volume or network,
and it starts no database: `testenv/dbctl.sh` owns those.

## How the job is shaped

The image bakes in **both the tool and the migrations**:

```dockerfile
COPY src ./src                 # the engine
RUN pip install '.[oracle,postgres]'
COPY examples ./examples       # the migrations
```

That is deliberate. Fingerprints exist to pin what was executed, so the
migration tree must not be able to change after the image is built. Mounting
migrations from the host would reintroduce exactly the drift the fingerprint is
there to catch. Two things are mounted at run time: the generated
configuration, read-only, and a host directory for the event log. The only
thing passed in the environment is the password:

```bash
docker run --rm --network migr8-testenv_default \
  --volume "$GEN:/etc/migr8:ro" \
  --volume "$GEN/runs:/var/log/migr8" \
  --env "MIGR8_PASSWORD=..." \
  migr8-runner:local \
  migrate --config /etc/migr8/oracle.toml --manifest /opt/migr8/examples/oracle/manifest.toml
```

The image runs as an unprivileged user and contains no credential. Its event log
goes to `/var/log/migr8/run.jsonl` via `MIGR8_LOG_FILE`, and the job container is
removed when it exits, so that path is mounted from the host: the log outlives
the container in `generated/runs/run.jsonl`, and `ctl.sh runlog` prints it. The
job runs as uid 10001, so `ctl.sh` opens that directory to it; a deployment
mounts a directory its own runner owns.

## Addressing and schemas

The job joins the Compose project's network, `migr8-testenv_default`, and
reaches each database by its service name: `oracle:1521/FREEPDB1` and
`host=postgres port=5432`. The generated configs carry those names; nothing is
discovered.

The suite binds `MIGR8_TEST` and the `migr8` schema to its lock id, and a job
run against them would fail the binding check by design, so `ctl.sh provision`
creates `MIGR8_JOB` (with the `_RUNNER` and `_OWNER` pair `provision_oracle.py`
makes for any name) and `migr8_job`, using the rig's own provisioning scripts.
`ORACLE_JOB_USER`, `ORACLE_JOB_PASSWORD` and `PG_JOB_SCHEMA` override the
names; the database credentials and ports come from `testenv/.env` as they do
for `dbctl.sh`.

## Verified

On 2026-09-15 on macOS arm64, podman 6.1.1 through the docker CLI 29.8.0, with
the databases from `testenv/dbctl.sh up`: the image built (208 MB); `migrate`,
`status` and `validate` exited 0 on Oracle Free 23.9.0.25.07 (5 migrations
applied) and on PostgreSQL 17.5 (4 applied); 54 events were on the host in
`generated/runs/run.jsonl` afterwards.

On podman the docker CLI builds in a BuildKit container it creates, listed by
`docker buildx ls` as `podman`; `docker buildx rm podman` removes it.

CI builds the image and runs `migrate` and `validate` on both engines in the
`databases` job on every push, after the suite.

## Apple `container`

This directory first ran under Apple's `container` runtime, with a database
start-up of its own. The image builds and runs there unchanged; the addressing
and storage differences, and what was verified when, are in
[`APPLE-CONTAINER.md`](APPLE-CONTAINER.md).
