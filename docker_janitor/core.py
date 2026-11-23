"""Decide which Docker containers, images and volumes are safe to delete."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

KEEP_LABEL = "janitor.keep"
_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$")
_SIZE_RE = re.compile(r"^([\d.]+)\s*([kKMGT]?i?B)$")
_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
_ANON_VOLUME_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_ts(value: str | None) -> datetime | None:
    """Parse Docker timestamps (RFC3339 with up to nanosecond precision)."""
    if not value or value.startswith("0001-01-01"):
        return None
    m = _TS_RE.match(value.strip())
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7].ljust(7, "0")  # 3.10 fromisoformat needs exactly 3 or 6 digits
    tz = m.group(3) or "Z"
    tz = "+00:00" if tz == "Z" else tz
    return datetime.fromisoformat(f"{m.group(1)}{frac}{tz}")


def parse_size(value: str | None) -> int | None:
    """Parse Docker's human sizes such as '8.19kB' or '1.2GB' into bytes."""
    if not value or value == "N/A":
        return None
    m = _SIZE_RE.match(value.strip())
    if not m or m.group(2) not in _UNITS:
        return None
    return round(float(m.group(1)) * _UNITS[m.group(2)])


def human(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1000
    return f"{n:.1f}TB"


def human_age(days: float) -> str:
    return f"{days:.0f}d" if days >= 1 else f"{days * 24:.0f}h"


def _labels(obj: dict) -> dict:
    return (obj.get("Config") or {}).get("Labels") or obj.get("Labels") or {}


def _kept_by_label(obj: dict) -> bool:
    return str(_labels(obj).get(KEEP_LABEL, "")).lower() in ("1", "true", "yes")


@dataclass
class Item:
    kind: str  # container | image | volume
    id: str
    name: str
    size: int | None
    age_days: float
    reason: str
    targets: list[str] = field(default_factory=list)  # what to pass to `docker ... rm`

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Policy:
    older_than_days: float = 7
    keep_last: int = 2
    keep_patterns: tuple[str, ...] = ()
    volumes: bool = False
    named_volumes: bool = False


def plan(containers, images, volumes, policy: Policy, now: datetime | None = None, volume_sizes=None) -> list[Item]:
    now = now or datetime.now(timezone.utc)
    volume_sizes = volume_sizes or {}
    items: list[Item] = []

    def age(ts: datetime | None) -> float:
        return (now - ts).total_seconds() / 86400 if ts else 0.0

    # 1. Stopped containers that have been stopped long enough.
    remaining = []
    for c in containers:
        state = c.get("State") or {}
        status = state.get("Status", "")
        stopped_at = parse_ts(state.get("FinishedAt")) or parse_ts(c.get("Created"))
        a = age(stopped_at)
        name = c.get("Name", "").lstrip("/") or c["Id"][:12]
        if status in ("exited", "created", "dead") and a >= policy.older_than_days and not _kept_by_label(c):
            items.append(
                Item("container", c["Id"], name, c.get("SizeRw"), a, f"{status} for {human_age(a)}", [c["Id"]])
            )
        else:
            remaining.append(c)

    # 2. Images not used by any remaining container.
    used_images = {c.get("Image") for c in remaining}
    newest: dict[str, list[tuple[datetime, str]]] = {}
    for img in images:
        for tag in img.get("RepoTags") or []:
            repo = tag.rsplit(":", 1)[0]
            newest.setdefault(repo, []).append((parse_ts(img.get("Created")) or now, img["Id"]))
    protected = set()
    for entries in newest.values():
        protected.update(i for _, i in sorted(entries, reverse=True)[: policy.keep_last])

    for img in images:
        tags = img.get("RepoTags") or []
        a = age(parse_ts(img.get("Created")))
        name = ", ".join(tags) if tags else img["Id"].split(":")[-1][:12]
        if img["Id"] in used_images or _kept_by_label(img):
            continue
        if any(fnmatch.fnmatch(t, p) for t in tags for p in policy.keep_patterns):
            continue
        if not tags:
            reason = "dangling (untagged)"
        elif img["Id"] in protected:
            continue
        elif a >= policy.older_than_days:
            reason = f"unused, {human_age(a)} old, not in newest {policy.keep_last} of its repo"
        else:
            continue
        items.append(Item("image", img["Id"], name, img.get("Size"), a, reason, tags or [img["Id"]]))

    # 3. Volumes (opt-in) not mounted by any remaining container.
    if policy.volumes:
        mounted = {m.get("Name") for c in remaining for m in c.get("Mounts") or [] if m.get("Type") == "volume"}
        for v in volumes:
            vname = v["Name"]
            anonymous = bool(_ANON_VOLUME_RE.match(vname))
            a = age(parse_ts(v.get("CreatedAt")))
            if vname in mounted or _kept_by_label(v) or (not anonymous and not policy.named_volumes):
                continue
            if any(fnmatch.fnmatch(vname, p) for p in policy.keep_patterns) or a < policy.older_than_days:
                continue
            kind = "anonymous" if anonymous else "named"
            label = vname[:12] if anonymous else vname
            items.append(Item("volume", vname, label, volume_sizes.get(vname), a, f"unused {kind} volume", [vname]))
    return items


class DockerCLI:
    """Thin wrapper over the `docker` CLI so no SDK or socket library is needed."""

    def __init__(self, binary: str = "docker", runner=subprocess.run):
        self.binary = binary
        self.runner = runner

    def _run(self, *args: str) -> str:
        proc = self.runner([self.binary, *args], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _inspect(self, list_args: list[list[str]], inspect_cmd: list[str]) -> list[dict]:
        ids = sorted({i for args in list_args for i in self._run(*args).split()})
        return json.loads(self._run(*inspect_cmd, *ids)) if ids else []

    def containers(self) -> list[dict]:
        return self._inspect([["ps", "-aq", "--no-trunc"]], ["container", "inspect", "--size"])

    def images(self) -> list[dict]:
        # Newer Docker (containerd image store) hides untagged images from a plain `image ls`;
        # `-a` would also list intermediate build layers, so ask for dangling ones explicitly.
        base = ["image", "ls", "-q", "--no-trunc"]
        return self._inspect([base, [*base, "--filter", "dangling=true"]], ["image", "inspect"])

    def volumes(self) -> list[dict]:
        return self._inspect([["volume", "ls", "-q"]], ["volume", "inspect"])

    def volume_sizes(self) -> dict[str, int | None]:
        try:
            data = json.loads(self._run("system", "df", "-v", "--format", "json"))
        except (RuntimeError, json.JSONDecodeError):
            return {}
        return {v["Name"]: parse_size(v.get("Size")) for v in data.get("Volumes") or []}

    def root_dir(self) -> str:
        return self._run("info", "--format", "{{.DockerRootDir}}").strip()

    def remove(self, item: Item) -> None:
        cmd = {"container": ["container", "rm"], "image": ["image", "rm"], "volume": ["volume", "rm"]}[item.kind]
        self._run(*cmd, *item.targets)
