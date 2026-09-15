# Running this under Apple `container`

Until 2026-09-15 this directory started the databases itself under Apple's
`container` runtime and ran the job there. The databases now come from
`testenv/compose.yaml`, which that runtime does not run (it has no Compose and
no `cp` subcommand, and Oracle cannot initialise into one of its named volumes),
and the job runs on whatever serves the Docker API. The image builds and runs
under Apple `container` unchanged; these notes are for anyone doing that.

## What was verified, and when

| Date | `container` CLI | Result |
|---|---|---|
| before 2026-09-13 | 1.2.2 on macOS 26.6.2, arm64 | Oracle Free 23.9.0.25.07 and PostgreSQL 17.5 ran as containers; `migrate` and `status` passed on Oracle (5 migrations), `migrate` and `validate` on PostgreSQL (4 migrations), from the job image (`python:3.14-slim`, Python 3.14.7) running unprivileged |
| 2026-09-13 | 1.4.1 | the image built from that day's `Containerfile`, and a job run with the event-log mount left `run.jsonl` on the host |
| 2026-09-15 | 1.4.1 | the whole suite, 776 passed and 3 skipped, ran from a host interpreter on Python 3.12.12 and on 3.14.7 against both databases at their container addresses; a PostgreSQL 17.5 container accepted host connections on a published port (`127.0.0.1:15499`) and on its own address 4 s after start |

## Addressing is by IP, not by name

On the default network, containers do not resolve each other's names. The
script of that time read each database container's address from
`container inspect` (`ipv4Address` under `networks`) and rendered it into the
generated config. Name-based addressing exists, but it needs

```bash
sudo container system dns create migr8.test
```

which requires administrator rights and changes host DNS configuration.

## Host-to-container TCP

At 1.2.2 it did not work on this machine: published ports reset the
connection and direct addresses gave "no route to host" while ICMP succeeded,
and the database never logged an inbound connection:

```text
127.0.0.1:15443   -> ConnectionResetError: Connection reset by peer
192.168.64.2:5432 -> OSError: [Errno 65] No route to host   (while ping succeeded)
```

At 1.4.1 both work (table above). The job runs as a sibling container in
either case, which is the deployment shape anyway.

## Oracle cannot initialise into a named volume

Mounting one at `/opt/oracle/oradata` fails during database creation:

```text
ERROR: Cannot create folder : errno=2 : No such file or directory : /opt/oracle/oradata/FREE/FREEPDB1
```

The image creates the database as the unprivileged `oracle` user, and the
volume is not writable by it. The workaround was the container's own writable
layer: a disposable test database does not need to survive removal. For a
database you want to keep, fix the mount ownership before first start or use a
runtime whose volume semantics the image supports.

## Resources and readiness

Oracle Free had 4 CPUs and 4 GiB, PostgreSQL 2 CPUs and 1 GiB. With no health
checks, readiness was `DATABASE IS READY TO USE` and `database system is ready
to accept connections` in the logs, with a 15 minute ceiling.
