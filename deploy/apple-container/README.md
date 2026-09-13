# Deploying under Apple's `container` runtime

This runs the migration job *as a container*, against databases in sibling
containers, using Apple's native `container` runtime on macOS. No Docker, no
Compose.

It is the realistic production shape: migrations are a job that runs once,
alongside the database, from an image whose contents are fixed.

Verified on this machine. **The database runs predate the current `ctl.sh` and
`Containerfile` and have not been repeated against them**, so the table is
evidence about the runtime rather than about the files as they stand. One part
has been rechecked: on 2026-09-13, with `container` CLI 1.4.1, the image builds
from the current `Containerfile` and a job run with the event-log mount leaves
`run.jsonl` on the host after the container is removed.

| Component | Version |
|---|---|
| macOS | 26.6.2 (arm64) |
| `container` CLI | 1.2.2 |
| Oracle Database Free | **23.9.0.25.07**, from `gvenzl/oracle-free:23.9-slim` |
| PostgreSQL | **17.5**, from `postgres:17.5` |
| Runner image | `python:3.14-slim`, Python 3.14.7 |

Both engines ran the full example migration sequence end to end. Oracle Free
**does** work under Apple `container`; the caveats below are about storage and
networking, not about the database.

## Quick start

```bash
deploy/apple-container/ctl.sh build     # build the migration-job image
deploy/apple-container/ctl.sh up        # start both databases, wait, provision
deploy/apple-container/ctl.sh migrate oracle
deploy/apple-container/ctl.sh status  oracle
deploy/apple-container/ctl.sh migrate postgres
deploy/apple-container/ctl.sh validate postgres
deploy/apple-container/ctl.sh runlog     # the job's event log, kept on the host
deploy/apple-container/ctl.sh destroy    # remove the containers and generated configs
```

`ctl.sh` only ever touches names beginning `migr8-ac-`, plus the
`migr8-runner:local` image it builds. It changes no unrelated container,
image, volume or network.

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
configuration, and a host directory for the event log. The only thing passed in
the environment is the password:

```bash
container run --rm \
  --volume "$GEN:/etc/migr8" \
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

## Three runtime behaviours worth knowing

**Addressing is by IP, not by name.** On the default network, containers do not
resolve each other's names. `ctl.sh` therefore reads each database container's
address from `container inspect` and renders it into a generated config under
`generated/` (gitignored). Name-based addressing is available, but it needs

```bash
sudo container system dns create migr8.test
```

which `ctl.sh` deliberately does not run for you, because it requires
administrator rights and changes host DNS configuration.

**Host-to-container TCP did not work on this machine.** ICMP reached the
containers and `--publish` bound a listener on the host, but TCP connections were
reset and the database never logged an inbound connection:

```text
127.0.0.1:15443   -> ConnectionResetError: Connection reset by peer
192.168.64.2:5432 -> OSError: [Errno 65] No route to host   (while ping succeeded)
```

Restarting the runtime did not change it. This is why the job runs as a
container rather than from the host: container-to-container TCP works fine. If
you need host access on your machine, check it with `ctl.sh up` followed by a
direct connection attempt before relying on it. The Docker Compose setup in
[`../../testenv/`](../../testenv/) publishes working host ports and is what the
test suite uses.

**Oracle cannot initialise into an Apple container named volume.** Mounting one
at `/opt/oracle/oradata` fails during database creation:

```text
ERROR: Cannot create folder : errno=2 : No such file or directory : /opt/oracle/oradata/FREE/FREEPDB1
```

The image creates the database as the unprivileged `oracle` user, and the volume
is not writable by it. `ctl.sh` therefore uses the container's own writable
layer: a disposable test database does not need to survive `destroy`. For a
database you want to keep, either fix the mount ownership before first start or
use a runtime whose volume semantics the image supports.

## Resources

Oracle Free gets 4 CPUs and 4 GiB; PostgreSQL gets 2 CPUs and 1 GiB. Oracle's
first start initialises the database and takes a few minutes; `ctl.sh up` waits
for `DATABASE IS READY TO USE` with a 15 minute ceiling and shows the log if the
container stops instead.

## Relationship to the test suite

This directory is a **deployment example**. The acceptance suite runs against the
Docker Compose services in [`../../testenv/`](../../testenv/), because it needs
host-side connections for the directional commit-failure proxy and for the
concurrent-runner tests. Results recorded in
[`../../docs/ACCEPTANCE.md`](../../docs/ACCEPTANCE.md) come from that setup.
