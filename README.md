# docker-janitor

[![CI](https://github.com/Kiranpjk/docker-janitor/actions/workflows/ci.yml/badge.svg)](https://github.com/Kiranpjk/docker-janitor/actions/workflows/ci.yml)

Safe, dry-run-first cleanup of Docker disk space. It shows exactly which stopped containers, old
unused images and orphaned volumes it would delete, why, and roughly how much space that frees.
Nothing is deleted until you add `--apply`.

## The problem

`/var/lib/docker` slowly fills the disk on build servers and home-lab boxes. `docker system prune -a`
is all-or-nothing: it can delete the image you need for tomorrow's rollback, and `--volumes` can
delete data. Most people end up writing a fragile cron one-liner instead.

Community threads describing it:

- Docker Forums, "Overlay2 folder takes up entire drive" (Jan 2025): the overlay2 folder used
  almost the whole disk and pruning only freed part of it.
  <https://forums.docker.com/t/overlay2-folder-takes-up-entire-drive/146051>
- Docker Forums, "How can I find out which container is causing my overlay2 folder to grow and
  crash my VM?": the user can't tell what is using the space.
  <https://forums.docker.com/t/how-can-i-find-out-which-container-is-causing-my-overlay2-folder-to-grow-and-crash-my-vm/150374>

> Research note: the brief asked for Reddit threads (r/selfhosted, r/homelab). Reddit was not
> reachable from the environment this was built in (blocked by the network proxy, and not
> returned by the search tool), so these are the closest verified public threads.

## What it deletes (and what it never touches)

| Resource | Deleted when | Always kept |
|----------|--------------|-------------|
| Containers | `exited`/`created`/`dead` for at least `--older-than` days | running/paused containers, label `janitor.keep=true` |
| Images | dangling (untagged), or unused, older than `--older-than` and not among the newest `--keep-last` tags of their repo | images used by any kept container, `--keep` globs, label `janitor.keep=true` |
| Volumes | only with `--volumes`: anonymous, unmounted, older than `--older-than` (named volumes need `--named-volumes` too) | mounted volumes, `--keep` globs, label `janitor.keep=true` |

Order matters: containers go first, so an image or volume used only by a deleted container becomes
eligible in the same run.

## Demo

Real run against a local Docker 29 daemon. Setup: four `demo/api` tags, a `postgres-demo:15` image,
one untagged image, a stopped `old-migration` container, a `keep-me` container labelled
`janitor.keep=true` (using `demo/api:2`), one anonymous volume and a named `pgdata` volume.
Everything was created seconds earlier, so the default 7-day threshold finds only the dangling image:

```console
$ docker-janitor
KIND       NAME                                       AGE      SIZE  REASON
image      6d0371c199ba                                0h    18.9MB  dangling (untagged)

1 item(s), up to 18.9MB reclaimable (shared layers may be counted twice)
Dry run: nothing deleted. Re-run with --apply to delete. Protect items with label janitor.keep=true.

$ docker-janitor --older-than 0 --keep-last 1 --keep 'postgres-demo:*' --volumes --apply
KIND       NAME                                       AGE      SIZE  REASON
container  old-migration                               0h     4.1kB  created for 0h
image      6d0371c199ba                                0h    18.9MB  dangling (untagged)
image      demo/api:3                                  0h    18.9MB  unused, 0h old, not in newest 1 of its repo
image      demo/api:1                                  0h     6.3MB  unused, 0h old, not in newest 1 of its repo
volume     56c76543d9ab                                0h        0B  unused anonymous volume

5 item(s), up to 44.1MB reclaimable (shared layers may be counted twice)
removed container old-migration
removed image 6d0371c199ba
removed image demo/api:3
removed image demo/api:1
removed volume 56c76543d9ab

$ docker images
IMAGE              ID             DISK USAGE   CONTENT SIZE   EXTRA
demo/api:2         8612115ef50a       12.6MB         6.29MB   U
demo/api:4         3002433d111f       25.2MB         12.6MB
postgres-demo:15   09dc5ada9f13       16.8MB         8.39MB

$ docker-janitor --only-if-above 95
/var/lib/docker is 6.1% full (threshold 95%), nothing to do
```

`demo/api:2` survived because the protected container uses it, `demo/api:4` is the newest tag,
`postgres-demo:15` matched `--keep`, and the named `pgdata` volume was left alone.

## Install

```bash
pip install git+https://github.com/Kiranpjk/docker-janitor.git
```

Requires Python 3.10+ and the `docker` CLI on `PATH`. No Python dependencies.

## Usage

```bash
docker-janitor                                   # dry run with defaults (7 days, keep newest 2 per repo)
docker-janitor --older-than 14 --keep-last 3     # tune the policy
docker-janitor --keep 'postgres:*' --keep 'myapp:prod-*'
docker-janitor --volumes                         # include orphaned anonymous volumes
docker-janitor --volumes --named-volumes         # include unused named volumes (careful: data)
docker-janitor --json                            # machine-readable plan
docker-janitor --apply                           # actually delete
```

Protect anything permanently with a label: `docker run --label janitor.keep=true ...` or `LABEL
janitor.keep=true` in a Dockerfile.

Exit codes: `0` success, `1` some deletions failed (for example an image still referenced by a
child image), `2` Docker could not be reached.

### Nightly cron, only when the disk is getting full

```cron
30 3 * * * /usr/local/bin/docker-janitor --only-if-above 80 --older-than 14 --apply >> /var/log/docker-janitor.log 2>&1
```

## Run with Docker

The image contains the static `docker` CLI and talks to the host daemon through the socket:

```bash
docker build -t docker-janitor .
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock docker-janitor --older-than 14
# --only-if-above also needs the data dir mounted at the same path:
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/docker:/var/lib/docker:ro \
  ghcr.io/kiranpjk/docker-janitor:main --only-if-above 80 --apply
```

Mounting the Docker socket gives the container root-equivalent control of the host; only run images
you trust.

## CI/CD

- `.github/workflows/ci.yml`: `ruff check`, `ruff format --check`, `pytest` on Python 3.10 to 3.13
  (the Docker CLI is faked in tests), then a Docker build plus `--version` smoke test.
- `.github/workflows/release.yml`: on pushes to `main` and `v*` tags, builds a multi-arch
  (amd64/arm64) image and pushes it to `ghcr.io/kiranpjk/docker-janitor` with the built-in
  `GITHUB_TOKEN`.
- `.github/dependabot.yml`: weekly updates for pip, GitHub Actions and Docker base images.

## Development

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest -v
```

## License

MIT, see [LICENSE](LICENSE).
