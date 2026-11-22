"""Command-line entry point for docker-janitor."""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from . import __version__
from .core import KEEP_LABEL, DockerCLI, Policy, human, human_age, plan


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docker-janitor",
        description="Reclaim Docker disk space safely: stopped containers, unused old images and (opt-in) "
        "orphaned volumes. Dry-run by default.",
    )
    p.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    p.add_argument("--older-than", type=float, default=7, metavar="DAYS", help="minimum age to delete (default 7)")
    p.add_argument(
        "--keep-last", type=int, default=2, metavar="N", help="always keep the newest N images per repo (default 2)"
    )
    p.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="GLOB",
        help="never touch images/volumes matching this glob, e.g. 'postgres:*' (repeatable)",
    )
    p.add_argument("--volumes", action="store_true", help="also remove unused anonymous volumes")
    p.add_argument("--named-volumes", action="store_true", help="with --volumes, include unused named volumes too")
    p.add_argument(
        "--only-if-above",
        type=float,
        metavar="PCT",
        help="do nothing unless the Docker data disk is more than PCT%% full (for cron)",
    )
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.add_argument("--docker", default="docker", help="docker binary to use")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def disk_used_pct(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total * 100


def main(argv: list[str] | None = None, docker: DockerCLI | None = None) -> int:
    args = build_parser().parse_args(argv)
    docker = docker or DockerCLI(args.docker)
    try:
        if args.only_if_above is not None:
            root = docker.root_dir()
            pct = disk_used_pct(root)
            if pct <= args.only_if_above:
                print(f"{root} is {pct:.1f}% full (threshold {args.only_if_above:g}%), nothing to do")
                return 0
            print(f"{root} is {pct:.1f}% full (threshold {args.only_if_above:g}%), cleaning up")
        policy = Policy(args.older_than, args.keep_last, tuple(args.keep), args.volumes, args.named_volumes)
        sizes = docker.volume_sizes() if args.volumes else {}
        items = plan(docker.containers(), docker.images(), docker.volumes(), policy, volume_sizes=sizes)
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([i.to_dict() for i in items], indent=2))
    elif not items:
        print("Nothing to clean up.")
    else:
        print(f"{'KIND':<10} {'NAME':<40} {'AGE':>5} {'SIZE':>9}  REASON")
        for i in items:
            print(f"{i.kind:<10} {i.name[:40]:<40} {human_age(i.age_days):>5} {human(i.size):>9}  {i.reason}")
        total = sum(i.size or 0 for i in items)
        print(f"\n{len(items)} item(s), up to {human(total)} reclaimable (shared layers may be counted twice)")

    if not args.apply:
        if items and not args.json:
            print(
                f"Dry run: nothing deleted. Re-run with --apply to delete. Protect items with label {KEEP_LABEL}=true."
            )
        return 0

    failed = 0
    for i in items:
        try:
            docker.remove(i)
            print(f"removed {i.kind} {i.name}")
        except RuntimeError as exc:
            failed += 1
            print(f"skipped {i.kind} {i.name}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
